"""Collecting candidates across an export, and writing them down.

An export is a stream, so nothing here may grow with the number of games. The
pool keeps the worst few moments of each calendar month and drops the rest as
it goes, which bounds it at twelve times ``puzzle_pool_per_month`` however long
the year was.

Why per month rather than one global top-N: a report spreads its puzzles across
the year, and a global list would happily be six positions from the one week in
March everything went wrong. Bucketing at collection time means the spread is
still available at report time even after the surplus has been discarded.
"""

import logging
from bisect import insort
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Puzzle
from app.puzzles.candidates import Candidate

logger = logging.getLogger(__name__)

#: The bucket key: (year, month).
Month = tuple[int, int]


@dataclass(slots=True)
class CandidatePool:
    """The best candidates seen so far, a bounded number per month."""

    per_month: int = field(default_factory=lambda: settings.puzzle_pool_per_month)
    #: Each list is sorted worst-first, so the one to evict is always index 0.
    months: dict[Month, list[tuple[int, Candidate]]] = field(default_factory=dict)

    def add(self, candidate: Candidate, played_at: datetime) -> None:
        """Offer a candidate to the pool. Cheap to call for every game."""
        bucket = self.months.setdefault((played_at.year, played_at.month), [])
        # Sorting on the swing alone would compare Candidates when two moves
        # cost the same, and a dataclass with a dict field is not orderable.
        insort(bucket, (candidate.swing, candidate), key=lambda pair: pair[0])
        if len(bucket) > self.per_month:
            del bucket[0]

    def candidates(self) -> list[Candidate]:
        """Everything held, worst month first, best candidate first within it."""
        ordered: list[Candidate] = []
        for month in sorted(self.months):
            ordered.extend(candidate for _, candidate in reversed(self.months[month]))
        return ordered

    def __len__(self) -> int:
        return sum(len(bucket) for bucket in self.months.values())


async def store(session: AsyncSession, player_id: int, pool: CandidatePool) -> int:
    """Replace the player's puzzles with the pool, and return how many.

    Wholesale replacement rather than an upsert: the pool is a complete
    recomputation over the same window the games came from, so anything already
    stored that is not in it is a position that no longer makes the cut.

    Must run after the games are committed -- the rows point at ``games`` by
    foreign key, and half of one is not a puzzle.
    """
    await session.execute(delete(Puzzle).where(Puzzle.player_id == player_id))
    rows = [
        Puzzle(
            player_id=player_id,
            game_id=candidate.game_id,
            ply=candidate.ply,
            fen=candidate.fen,
            move_played=candidate.move_played,
            best_move=candidate.best_move,
            best_move_san=candidate.best_move_san,
            continuation=candidate.continuation,
            judgment=candidate.judgment,
            comment=candidate.comment,
            win_before=candidate.win_before,
            win_after=candidate.win_after,
            swing=candidate.swing,
            legal_moves=candidate.legal_moves,
        )
        for candidate in pool.candidates()
    ]
    session.add_all(rows)
    await session.commit()
    logger.info("stored %d puzzle(s) for player %d", len(rows), player_id)
    return len(rows)
