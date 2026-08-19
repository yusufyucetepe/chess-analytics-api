"""Batching behaviour, checked without a database.

What matters here is *when* rows are handed to Postgres, not what Postgres does
with them -- the upsert semantics are covered by the integration suite. A stub
session records the statements, which makes the easy and invisible bug (a final
partial batch that never gets flushed) impossible to miss.
"""

import copy
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.dialects.postgresql import Insert

from app.ingest import IngestStats, ingest_games
from tests.conftest import load_fixture

USERNAME = "zhigalko_sergei"
PLAYER_ID = 7


class RecordingSession:
    """Just enough of AsyncSession for `ingest_games` to run."""

    def __init__(self) -> None:
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement: Any) -> None:
        self.statements.append(statement)

    def add_all(self, rows: Any) -> None:
        self.added.extend(rows)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    @property
    def upserts(self) -> list[Any]:
        """The game batches, without the puzzle pool's own statements.

        Every ingest ends by replacing the player's puzzles, which is a DELETE
        and a commit whether or not it found any. These tests are about when
        *games* reach Postgres, so they count the inserts.
        """
        return [statement for statement in self.statements if isinstance(statement, Insert)]


@pytest.fixture
def template() -> dict[str, Any]:
    """One real analysed game, cloned to make as many as a test needs."""
    first = load_fixture("games_sample.ndjson").splitlines()[0]
    game: dict[str, Any] = json.loads(first)
    return game


def clone(template: dict[str, Any], index: int, **overrides: Any) -> dict[str, Any]:
    game = copy.deepcopy(template)
    game["id"] = f"game{index:04d}"
    game["createdAt"] += index * 1000
    game.update(overrides)
    return game


async def stream(games: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    for game in games:
        yield game


async def run(session: RecordingSession, games: list[dict[str, Any]], size: int) -> IngestStats:
    return await ingest_games(
        session,  # type: ignore[arg-type]
        stream(games),
        player_id=PLAYER_ID,
        username_lower=USERNAME,
        batch_size=size,
    )


async def test_games_are_written_one_batch_at_a_time(template: dict[str, Any]) -> None:
    session = RecordingSession()

    stats = await run(session, [clone(template, i) for i in range(12)], size=5)

    assert len(session.upserts) == 3, "5 + 5 + a partial 2"
    assert session.commits == 4, "each batch its own transaction, plus the puzzle pool's"
    assert stats.stored == 12


async def test_the_final_partial_batch_is_still_written(template: dict[str, Any]) -> None:
    session = RecordingSession()

    stats = await run(session, [clone(template, i) for i in range(3)], size=5)

    assert len(session.upserts) == 1
    assert stats.stored == 3


async def test_an_exact_multiple_does_not_write_an_empty_batch(template: dict[str, Any]) -> None:
    session = RecordingSession()

    await run(session, [clone(template, i) for i in range(10)], size=5)

    assert len(session.upserts) == 2


async def test_a_stream_of_only_skippable_games_writes_nothing(template: dict[str, Any]) -> None:
    session = RecordingSession()

    stats = await run(session, [clone(template, i, variant="chess960") for i in range(4)], size=2)

    assert session.upserts == []
    assert session.added == [], "and no puzzles mined from games we declined"
    assert stats == IngestStats(fetched=4, stored=0, skipped=4, analysed=0, puzzles=0)


async def test_skipped_games_do_not_fill_the_batch(template: dict[str, Any]) -> None:
    """Otherwise a run of variants would trigger a flush of two real games."""
    session = RecordingSession()
    games = [clone(template, i, variant="chess960") for i in range(8)]
    games += [clone(template, 100), clone(template, 101)]

    stats = await run(session, games, size=5)

    assert len(session.upserts) == 1, "the two kept games flush at the end, not mid-stream"
    assert stats.fetched == 10
    assert stats.skipped == 8
    assert stats.stored == 2


async def test_a_repeated_game_id_collapses_within_a_batch(template: dict[str, Any]) -> None:
    """Postgres rejects an ON CONFLICT insert that hits one row twice."""
    session = RecordingSession()
    duplicated = [clone(template, 1), clone(template, 1), clone(template, 2)]

    stats = await run(session, duplicated, size=10)

    assert stats.fetched == 3
    assert stats.stored == 2, "the repeat overwrote rather than being sent twice"
    assert stats.skipped == 0


async def test_analysed_games_are_counted_for_the_coverage_caveat(
    template: dict[str, Any],
) -> None:
    session = RecordingSession()
    bare = copy.deepcopy(template)
    del bare["players"]["white"]["analysis"]
    games = [clone(template, 1), clone(bare, 2), clone(bare, 3)]

    stats = await run(session, games, size=10)

    assert stats.stored == 3
    assert stats.analysed == 1
