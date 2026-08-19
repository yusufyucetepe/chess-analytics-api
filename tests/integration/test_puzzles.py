"""Puzzles from the export to the section, against a real Postgres.

The unit suite covers which position gets picked and how the pool is bounded.
What is left needs the database: that a JSONB move map and a text array survive
the round trip, that the rows really are tied to the games they came from, and
that re-ingesting replaces a pool instead of stacking a second one on top.
"""

import copy
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Color, Game, Player, Puzzle, Result
from app.ingest import ingest_games
from app.report.sections import puzzles as section
from tests.conftest import load_fixture

USERNAME_LOWER = "zhigalko_sergei"
SINCE = datetime(2018, 1, 1, tzinfo=UTC)
UNTIL = datetime(2019, 1, 1, tzinfo=UTC)


@pytest.fixture
def raw_games() -> list[dict[str, Any]]:
    return [json.loads(line) for line in load_fixture("games_sample.ndjson").splitlines() if line]


async def stream(games: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    for game in games:
        yield game


async def ingest(session: AsyncSession, player: Player, games: list[dict[str, Any]]) -> Any:
    return await ingest_games(
        session, stream(games), player_id=player.id, username_lower=USERNAME_LOWER
    )


async def stored(session: AsyncSession, player: Player) -> list[Puzzle]:
    result = await session.execute(
        select(Puzzle).where(Puzzle.player_id == player.id).order_by(Puzzle.game_id)
    )
    return list(result.scalars())


# --- mined during ingest ----------------------------------------------------


async def test_the_export_leaves_puzzles_behind(
    session: AsyncSession, player: Player, raw_games: list[dict[str, Any]]
) -> None:
    stats = await ingest(session, player, raw_games)

    rows = await stored(session, player)
    assert stats.puzzles == len(rows) > 0
    assert {row.game_id for row in rows} <= {game["id"] for game in raw_games}


async def test_every_column_the_board_reads_survives_the_round_trip(
    session: AsyncSession, player: Player, raw_games: list[dict[str, Any]]
) -> None:
    """A JSONB map, a text array and three small integers, all bound at once.

    One puzzle out of six games, and that is the real yield: five of them are
    the opponent's mistakes or nobody's. The values are the export's own.
    """
    await ingest(session, player, raw_games)
    rows = await stored(session, player)

    assert [row.game_id for row in rows] == ["aJrkZsy8"]
    row = rows[0]
    assert row.ply == 38
    assert row.fen == "r1q1bk2/1p1n1prB/p3p3/3p3R/3P1Q2/2P5/PP3PPP/RN4K1 w - - 5 20"
    assert row.best_move == "f4d6"
    assert row.best_move_san == "Qd6#"
    assert row.judgment == "Blunder"
    assert "d6" in row.legal_moves["f4"]
    # Lichess gives no line past a move that was itself mate, so the array is
    # legitimately empty here -- which is the case worth pinning.
    assert row.continuation == []
    assert (row.win_before, row.win_after, row.swing) == (100, 82, 18)


async def test_a_second_export_replaces_the_pool_rather_than_adding_to_it(
    session: AsyncSession, player: Player, raw_games: list[dict[str, Any]]
) -> None:
    first = await ingest(session, player, raw_games)
    second = await ingest(session, player, raw_games)

    assert first.puzzles == second.puzzles
    assert len(await stored(session, player)) == second.puzzles


async def test_a_game_that_stops_qualifying_loses_its_puzzle(
    session: AsyncSession, player: Player, raw_games: list[dict[str, Any]]
) -> None:
    """Wholesale replacement is the point: an upsert would leave this behind."""
    await ingest(session, player, raw_games)
    before = {row.game_id for row in await stored(session, player)}

    stripped = copy.deepcopy(raw_games)
    for game in stripped:
        game.pop("analysis", None)
    await ingest(session, player, stripped)

    assert before and await stored(session, player) == []


async def test_deleting_a_player_takes_their_puzzles_with_them(
    session: AsyncSession, player: Player, raw_games: list[dict[str, Any]]
) -> None:
    """The rows point at `games` by foreign key; nothing else enforces this."""
    await ingest(session, player, raw_games)

    await session.execute(delete(Game).where(Game.player_id == player.id))
    await session.commit()

    count = await session.scalar(
        select(func.count()).select_from(Puzzle).where(Puzzle.player_id == player.id)
    )
    assert count == 0


# --- the section ------------------------------------------------------------


async def build(session: AsyncSession, player: Player, analysed: int = 5) -> dict[str, Any]:
    return await section.build(
        session, player.id, since=SINCE, until=UNTIL, games_analysed=analysed
    )


async def seed_puzzle(
    session: AsyncSession, player: Player, game_id: str, played_at: datetime, swing: int
) -> None:
    """One game and its puzzle. The FEN is a real position; the rest is filler."""
    session.add(
        Game(
            game_id=game_id,
            player_id=player.id,
            played_at=played_at,
            perf="blitz",
            rated=True,
            color=Color.WHITE,
            result=Result.LOSS,
            status="resign",
            moves_count=40,
            opponent_name="someone",
            opponent_rating=1500,
            opening_line=[],
            analysed=True,
        )
    )
    await session.flush()
    session.add(
        Puzzle(
            player_id=player.id,
            game_id=game_id,
            ply=20,
            fen="r1bq1rk1/ppp2pp1/3p3p/2bN4/2Ppn3/1P5P/PB1PBPP1/R2QK2R w KQ - 0 11",
            move_played="f3",
            best_move="e1g1",
            best_move_san="O-O",
            continuation=["c6"],
            judgment="Blunder",
            comment="Checkmate is now unavoidable. O-O was best.",
            win_before=swing,
            win_after=0,
            swing=swing,
            legal_moves={"e1": ["f1", "g1"]},
        )
    )
    await session.commit()


async def test_the_section_walks_the_year_rather_than_one_bad_week(
    session: AsyncSession, player: Player
) -> None:
    """Three big swings in one month and two small ones months later."""
    march = datetime(2018, 3, 4, tzinfo=UTC)
    for index, swing in enumerate([90, 85, 80]):
        await seed_puzzle(session, player, f"mar{index}", march + timedelta(days=index), swing)
    await seed_puzzle(session, player, "jul0", datetime(2018, 7, 1, tzinfo=UTC), 40)
    await seed_puzzle(session, player, "oct0", datetime(2018, 10, 1, tzinfo=UTC), 30)

    built = await build(session, player)

    assert built["pool"] == 5
    assert built["shown"] == 5
    months = [item["played_at"][5:7] for item in built["puzzles"]]
    assert months == sorted(months), "the section reads forwards through the year"
    assert set(months) == {"03", "07", "10"}


async def test_the_section_stops_at_the_number_a_report_shows(
    session: AsyncSession, player: Player
) -> None:
    pool = settings.report_puzzles_shown + 3
    for index in range(pool):
        # Two a month past the twelfth, so the surplus is real rather than a
        # thirteenth month the calendar does not have.
        when = datetime(2018, index % 12 + 1, index // 12 + 5, tzinfo=UTC)
        await seed_puzzle(session, player, f"g{index}", when, 50 + index)

    built = await build(session, player)

    assert built["pool"] == pool
    assert built["shown"] == settings.report_puzzles_shown
    assert len(built["puzzles"]) == settings.report_puzzles_shown


async def test_each_puzzle_carries_the_game_it_came_from(
    session: AsyncSession, player: Player
) -> None:
    await seed_puzzle(session, player, "abc123", datetime(2018, 5, 5, tzinfo=UTC), 50)

    item = (await build(session, player))["puzzles"][0]

    assert item["perf"] == "blitz"
    assert item["color"] == "white"
    assert item["result"] == "loss"
    assert item["opponent"] == {"name": "someone", "rating": 1500}
    assert item["move_number"] == 11, "ply 20 is White's eleventh move"
    assert item["url"].endswith("/abc123/white#20")
    assert item["continuation"] == ["c6"], "the text array comes back as a list"


async def test_a_year_with_nothing_analysed_says_so(session: AsyncSession, player: Player) -> None:
    built = await build(session, player, analysed=0)

    assert built["available"] is False
    assert built["puzzles"] == []
    assert "engine analysis" in built["reason"]


async def test_a_clean_year_is_told_apart_from_an_unanalysed_one(
    session: AsyncSession, player: Player
) -> None:
    built = await build(session, player, analysed=40)

    assert built["available"] is False
    assert "no clear turning point" in built["reason"]


async def test_a_puzzle_outside_the_window_is_not_shown(
    session: AsyncSession, player: Player
) -> None:
    await seed_puzzle(session, player, "old001", SINCE - timedelta(days=1), 70)

    built = await build(session, player)

    assert built["pool"] == 0 and built["available"] is False
