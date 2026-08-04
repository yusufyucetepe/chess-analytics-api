"""Ingest against a real Postgres.

The unit suite covers what the parser decides and when batches flush. What is
left can only be checked by writing to the database: that every column actually
binds (a NUMERIC, two enums, a text array, a timestamptz), and that re-running
an ingest converges instead of duplicating or failing.
"""

import copy
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Color, Game, Player, Result
from app.ingest import ingest_games, ingest_player, upsert_player
from app.lichess import LichessClient
from app.lichess.schemas import PlayerProfile
from tests.conftest import load_fixture

USERNAME = "Zhigalko_Sergei"
USERNAME_LOWER = USERNAME.lower()
UNANALYSED = "JR9Xk3lP"

SINCE = datetime(2018, 1, 1, tzinfo=UTC)
UNTIL = datetime(2019, 1, 1, tzinfo=UTC)


@pytest.fixture
def raw_games() -> list[dict[str, Any]]:
    return [json.loads(line) for line in load_fixture("games_sample.ndjson").splitlines() if line]


@pytest.fixture
def profile() -> PlayerProfile:
    return PlayerProfile.model_validate(json.loads(load_fixture("user_profile.json")))


async def stream(games: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    for game in games:
        yield game


async def count_games(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(Game))).scalar_one()


async def store(
    session: AsyncSession, games: list[dict[str, Any]], player_id: int, **kwargs: Any
) -> Any:
    return await ingest_games(
        session,
        stream(games),
        player_id=player_id,
        username_lower=USERNAME_LOWER,
        **kwargs,
    )


# ------------------------------------------------------------------- players


async def test_upsert_player_creates_the_row(session: AsyncSession, profile: PlayerProfile) -> None:
    player = await upsert_player(session, profile)

    assert player.id is not None
    assert player.username_lower == USERNAME_LOWER
    assert player.display_name == USERNAME
    assert player.title == "GM"
    assert player.ratings["bullet"] > 0
    assert player.last_fetched_at is None, "we have not fetched any games yet"


async def test_upsert_player_refreshes_instead_of_duplicating(
    session: AsyncSession, profile: PlayerProfile
) -> None:
    first = await upsert_player(session, profile)

    moved_on = profile.model_copy(deep=True)
    moved_on.perfs["bullet"].rating = 3000
    second = await upsert_player(session, moved_on)

    assert second.id == first.id
    assert second.ratings["bullet"] == 3000
    assert (await session.execute(select(func.count()).select_from(Player))).scalar_one() == 1


# --------------------------------------------------------------------- games


async def test_the_export_lands_in_postgres_intact(
    session: AsyncSession, profile: PlayerProfile, raw_games: list[dict[str, Any]]
) -> None:
    player = await upsert_player(session, profile)

    stats = await store(session, raw_games, player.id)

    assert stats.fetched == len(raw_games)
    assert stats.stored == len(raw_games)
    assert stats.skipped == 0
    assert stats.analysed == len(raw_games) - 1, "the fixture ends on an unanalysed game"

    game = (
        await session.execute(select(Game).where(Game.game_id == raw_games[0]["id"]))
    ).scalar_one()
    # Read back rather than trusted from the parser: these are the column types
    # that can bind wrongly and only fail against a real driver.
    assert game.color is Color.WHITE
    assert game.result is Result.WIN
    assert game.accuracy == Decimal("96.00")
    assert game.opening_line[:4] == ["e4", "c5", "c3", "d6"]
    assert game.played_at == datetime(2018, 9, 20, 13, 59, 41, 883000, tzinfo=UTC)


async def test_re_ingesting_the_same_export_changes_nothing(
    session: AsyncSession, profile: PlayerProfile, raw_games: list[dict[str, Any]]
) -> None:
    """A retried job must converge, not accumulate."""
    player = await upsert_player(session, profile)

    await store(session, raw_games, player.id)
    await store(session, raw_games, player.id)

    assert await count_games(session) == len(raw_games)


async def test_a_game_analysed_later_is_picked_up_on_re_ingest(
    session: AsyncSession, profile: PlayerProfile, raw_games: list[dict[str, Any]]
) -> None:
    """Players request analysis after the fact; the upsert has to notice."""
    player = await upsert_player(session, profile)
    unanalysed = next(game for game in raw_games if game["id"] == UNANALYSED)
    await store(session, [unanalysed], player.id)

    now_analysed = copy.deepcopy(unanalysed)
    now_analysed["players"]["white"]["analysis"] = {
        "inaccuracy": 2,
        "mistake": 1,
        "blunder": 0,
        "acpl": 31,
        "accuracy": 88,
    }
    await store(session, [now_analysed], player.id)

    game = (await session.execute(select(Game).where(Game.game_id == UNANALYSED))).scalar_one()
    assert game.analysed is True
    assert game.accuracy == Decimal("88.00")
    assert game.blunders == 0
    assert await count_games(session) == 1


async def test_a_batch_size_below_the_stream_stores_everything(
    session: AsyncSession, profile: PlayerProfile, raw_games: list[dict[str, Any]]
) -> None:
    player = await upsert_player(session, profile)

    await store(session, raw_games, player.id, batch_size=2)

    assert await count_games(session) == len(raw_games)


async def test_two_players_can_share_one_game(
    session: AsyncSession, profile: PlayerProfile, raw_games: list[dict[str, Any]]
) -> None:
    """The reason the primary key is (game_id, player_id) and not the game id.

    Colour, ratings and the analysis block are all per player, so one game
    between two users we track is two rows.
    """
    player = await upsert_player(session, profile)
    opponent_profile = profile.model_copy(update={"id": "axmerk", "username": "axmerk"})
    opponent = await upsert_player(session, opponent_profile)

    game = raw_games[0]
    await store(session, [game], player.id)
    await ingest_games(
        session,
        stream([game]),
        player_id=opponent.id,
        username_lower="axmerk",
    )

    rows = (await session.execute(select(Game).where(Game.game_id == game["id"]))).scalars().all()
    assert len(rows) == 2
    by_player = {row.player_id: row for row in rows}
    assert by_player[player.id].color is Color.WHITE
    assert by_player[opponent.id].color is Color.BLACK
    assert by_player[opponent.id].result is Result.LOSS


# ---------------------------------------------------------------- end to end


@respx.mock
async def test_ingest_player_walks_profile_then_games(session: AsyncSession) -> None:
    respx.get(path=f"/api/user/{USERNAME}").mock(
        return_value=httpx.Response(200, json=json.loads(load_fixture("user_profile.json")))
    )
    respx.get(path=f"/api/games/user/{USERNAME}").mock(
        return_value=httpx.Response(
            200,
            text=load_fixture("games_sample.ndjson"),
            headers={"content-type": "application/x-ndjson"},
        )
    )

    async with LichessClient() as client:
        player, stats = await ingest_player(session, client, USERNAME, since=SINCE, until=UNTIL)

    assert player.username_lower == USERNAME_LOWER
    assert stats.stored == 6
    assert await count_games(session) == 6
    # Set only now that the export ran to completion -- step 7's caching rules
    # will read this as "we have this player's year".
    assert player.last_fetched_at is not None
