"""The puzzles section: the year handed back to be played again.

Unlike every other section this one does not aggregate. The work was done at
ingest, while the per-ply analysis was still in hand; all that is left is
choosing which of the stored positions a report shows, and joining each one
back to the game it came from.

No coverage gate here, and deliberately. The quality section refuses to average
a handful of self-selected games because the average would be a lie; a single
position is not an average, and one real blunder from one analysed game is a
perfectly honest puzzle.
"""

from datetime import datetime
from itertools import zip_longest
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Game, Puzzle
from app.puzzles import after_move


async def build(
    session: AsyncSession,
    player_id: int,
    *,
    since: datetime,
    until: datetime,
    games_analysed: int,
) -> dict[str, Any]:
    """Up to ``report_puzzles_shown`` positions, spread across the year."""
    rows = await _stored(session, player_id, since, until)
    chosen = _spread(rows, settings.report_puzzles_shown)
    return {
        "available": bool(chosen),
        "shown": len(chosen),
        # What ingest kept, which is more than a report shows. Worth stating:
        # it is the difference between "you had two bad moments" and "here are
        # the six worst of many".
        "pool": len(rows),
        "games_analysed": games_analysed,
        "reason": _reason(rows, games_analysed),
        "puzzles": [_shape(row) for row in sorted(chosen, key=lambda row: row.played_at)],
    }


async def _stored(
    session: AsyncSession, player_id: int, since: datetime, until: datetime
) -> list[Any]:
    """Every stored puzzle in the window, with its game's context attached."""
    stmt = (
        select(
            Puzzle,
            Game.played_at,
            Game.perf,
            Game.color,
            Game.result,
            Game.opponent_name,
            Game.opponent_rating,
        )
        .join(Game, (Game.player_id == Puzzle.player_id) & (Game.game_id == Puzzle.game_id))
        .where(
            Puzzle.player_id == player_id,
            Game.played_at >= since,
            Game.played_at <= until,
        )
        .order_by(Puzzle.swing.desc())
    )
    return list((await session.execute(stmt)).all())


def _spread(rows: list[Any], limit: int) -> list[Any]:
    """Pick ``limit`` puzzles, taking the worst moment of each month in turn.

    Ranking on the swing alone would answer "your six worst moves", which for
    most players is one terrible evening rendered six times. Dealing round the
    months instead answers "your worst moment in March, and in June, and in
    October" -- and once every month has given one up, the second round takes
    the next worst from each, so a heavier month still contributes more.
    """
    months: dict[tuple[int, int], list[Any]] = {}
    for row in rows:  # already sorted worst-first
        months.setdefault((row.played_at.year, row.played_at.month), []).append(row)

    # zip_longest over the months' lists is exactly the round-robin deal: one
    # tuple per round, in chronological month order, with exhausted months
    # padding out as None.
    rounds = zip_longest(*(months[key] for key in sorted(months)))
    dealt = [row for group in rounds for row in group if row is not None]
    return dealt[:limit]


def _reason(rows: list[Any], games_analysed: int) -> str | None:
    """Why there are no puzzles, in the player's terms. ``None`` when there are."""
    if rows:
        return None
    if not games_analysed:
        return (
            "None of your games have engine analysis, so there is nothing to replay. "
            "Ask Lichess to analyse a game and it will show up here next year."
        )
    return (
        "Your analysed games had no clear turning point to replay -- "
        "no single move that handed the game over."
    )


def _shape(row: Any) -> dict[str, Any]:
    puzzle: Puzzle = row.Puzzle
    return {
        "game_id": puzzle.game_id,
        # Opened from the player's own side, at the move before the mistake.
        "url": f"{settings.lichess_base_url}/{puzzle.game_id}/{row.color.value}#{puzzle.ply}",
        "played_at": row.played_at.isoformat(),
        "perf": row.perf,
        "color": row.color.value,
        "result": row.result.value,
        "opponent": {"name": row.opponent_name, "rating": row.opponent_rating},
        "ply": puzzle.ply,
        #: Full move number as a player counts them, not the ply index.
        "move_number": puzzle.ply // 2 + 1,
        "fen": puzzle.fen,
        "move_played": puzzle.move_played,
        "best_move": puzzle.best_move,
        "best_move_san": puzzle.best_move_san,
        "continuation": list(puzzle.continuation or []),
        "judgment": puzzle.judgment,
        "comment": puzzle.comment,
        "win_before": puzzle.win_before,
        "win_after": puzzle.win_after,
        "swing": puzzle.swing,
        "legal_moves": dict(puzzle.legal_moves or {}),
        # What the board looks like once the answer is played. See `after_move`
        # for why this is worked out here rather than kept in the table.
        "after_move": after_move(puzzle.fen, puzzle.best_move),
    }
