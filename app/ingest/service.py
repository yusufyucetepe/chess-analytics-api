"""Streaming ingest: Lichess NDJSON in, ``games`` rows out.

Written around two constraints. A year of bullet is tens of thousands of games,
so nothing is ever held in memory whole -- the stream is consumed in batches and
each batch is committed on its own. And an ingest can die halfway through, so
every write is an upsert keyed on the game's natural id: running it again is
cheap and safe rather than something to guard against.
"""

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Game, Player
from app.ingest.parser import parse_game
from app.lichess.client import LichessClient
from app.lichess.schemas import PlayerProfile

logger = logging.getLogger(__name__)

#: The composite primary key -- the only columns an upsert must not rewrite.
_GAME_KEY = ("game_id", "player_id")


@dataclass(slots=True)
class IngestStats:
    """What one ingest run did, for the report row and the logs."""

    #: Games the export handed us, including the ones we chose not to keep.
    fetched: int = 0
    #: Rows written or refreshed.
    stored: int = 0
    #: Variants, aborted games, anything `parse_game` declined.
    skipped: int = 0
    #: Of the stored rows, how many carried engine analysis.
    analysed: int = 0


async def upsert_player(session: AsyncSession, profile: PlayerProfile) -> Player:
    """Create or refresh the player row, and return it.

    Deliberately does not touch ``last_fetched_at``: that means "we have this
    player's games", and at this point we do not yet.
    """
    values = {
        "username_lower": profile.id.lower(),
        "display_name": profile.username,
        "title": profile.title,
        "country": profile.country,
        "ratings": profile.ratings(),
    }
    insert = pg_insert(Player).values(**values)
    stmt = insert.on_conflict_do_update(
        index_elements=[Player.username_lower],
        set_={key: insert.excluded[key] for key in values if key != "username_lower"},
    ).returning(Player)
    # `populate_existing` so a second upsert in the same session returns the
    # refreshed row rather than the copy already in the identity map.
    orm_stmt = select(Player).from_statement(stmt).execution_options(populate_existing=True)
    player: Player = (await session.execute(orm_stmt)).scalar_one()
    await session.commit()
    return player


async def ingest_games(
    session: AsyncSession,
    games: AsyncIterator[dict[str, Any]],
    *,
    player_id: int,
    username_lower: str,
    batch_size: int | None = None,
) -> IngestStats:
    """Drain a game stream into ``games``.

    Takes any async iterator of raw games rather than the client itself, so the
    batching and upsert logic can be tested without a stream to mock.
    """
    size = settings.ingest_batch_size if batch_size is None else batch_size
    stats = IngestStats()
    # Keyed by game id so a duplicate overwrites instead of reaching Postgres
    # twice in one statement, which ON CONFLICT rejects outright ("cannot
    # affect row a second time"). The client's resume logic already dedupes;
    # this is the cheap belt to its braces.
    batch: dict[str, dict[str, Any]] = {}

    async for raw in games:
        stats.fetched += 1
        row = parse_game(raw, player_id=player_id, username_lower=username_lower)
        if row is None:
            stats.skipped += 1
            continue
        # `raw` also carries the per-ply `analysis` array, which has nowhere to
        # live in `games` and is dropped here on purpose. When the puzzle
        # feature lands, this loop -- while the raw game is still in hand -- is
        # where candidate positions get pulled out. See docs/BRIEF.md.
        batch[row["game_id"]] = row
        if len(batch) >= size:
            await _flush(session, batch, stats)

    await _flush(session, batch, stats)
    logger.info(
        "ingested player %d: %d fetched, %d stored, %d skipped, %d analysed",
        player_id,
        stats.fetched,
        stats.stored,
        stats.skipped,
        stats.analysed,
    )
    return stats


async def ingest_player(
    session: AsyncSession,
    client: LichessClient,
    username: str,
    *,
    since: datetime,
    until: datetime,
) -> tuple[Player, IngestStats]:
    """Fetch a player's profile and window of games, and store both.

    Raises ``UserNotFoundError`` before writing anything if the username does
    not exist -- the profile call doubles as the existence check.
    """
    profile = await client.get_profile(username)
    player = await upsert_player(session, profile)
    stats = await ingest_games(
        session,
        client.stream_games(profile.username, since=since, until=until),
        player_id=player.id,
        username_lower=player.username_lower,
    )
    # Only now, once the export ran to completion, is the player's history
    # actually ours. A half-finished ingest must not look fresh to the caching
    # rules in step 7.
    #
    # The count is written here for the same reason, and deliberately not in
    # `upsert_player`: that runs before the export, so storing it there would
    # record games as fetched at the moment we learned they existed, and the
    # next request would see nothing new however the export went.
    player.last_fetched_at = datetime.now(UTC)
    player.rated_games_count = profile.counts.rated
    session.add(player)
    await session.commit()
    return player, stats


async def _flush(
    session: AsyncSession, batch: dict[str, dict[str, Any]], stats: IngestStats
) -> None:
    if not batch:
        return
    rows = list(batch.values())
    await session.execute(_upsert_games(rows))
    # One transaction per batch, not one for the whole export: a year of games
    # in a single transaction would hold locks for minutes and lose everything
    # to one dropped connection. Because the writes are idempotent, a partial
    # ingest is a resumable state rather than a corrupt one.
    await session.commit()
    stats.stored += len(rows)
    stats.analysed += sum(1 for row in rows if row["analysed"])
    batch.clear()


def _upsert_games(rows: list[dict[str, Any]]) -> Any:
    """Insert a batch, refreshing any game we already had.

    Do-update rather than do-nothing: a player can request analysis on an old
    game, and a re-ingest is the only chance we get to pick that up.
    """
    stmt = pg_insert(Game).values(rows)
    return stmt.on_conflict_do_update(
        index_elements=list(_GAME_KEY),
        set_={
            column.name: stmt.excluded[column.name]
            for column in Game.__table__.columns
            if column.name not in _GAME_KEY
        },
    )
