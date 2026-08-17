"""Query fragments and JSON coercions shared by the sections.

Every section answers questions about the same slice of ``games`` -- one player,
one window -- so the filter and the win/draw/loss aggregates live here rather
than being retyped five times.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, func

from app.db.models import Game, Result


def window(player_id: int, since: datetime, until: datetime) -> tuple[ColumnElement[bool], ...]:
    """The rows a report is allowed to see."""
    return (
        Game.player_id == player_id,
        Game.played_at >= since,
        Game.played_at <= until,
    )


def result_counts() -> tuple[Any, Any, Any]:
    """Win, draw and loss counts as ``FILTER (WHERE ...)`` aggregates.

    Built fresh on each call: the same labels are used in several queries, and
    sharing one expression object across them would be reusing labels too.
    """
    return (
        func.count().filter(Game.result == Result.WIN).label("wins"),
        func.count().filter(Game.result == Result.DRAW).label("draws"),
        func.count().filter(Game.result == Result.LOSS).label("losses"),
    )


def record(row: Any) -> dict[str, Any]:
    """A ``games/wins/draws/losses`` row plus the two ratios worth precomputing."""
    return {
        "games": row.games,
        "wins": row.wins,
        "draws": row.draws,
        "losses": row.losses,
        "win_rate": rate(row.wins, row.games),
        "score": score(row.wins, row.draws, row.games),
    }


def points_lost(draws: int, losses: int) -> float:
    """Points dropped against the maximum available -- a loss costs one, a draw half.

    The number the repertoire drill-down ranks on. Frequency finds the lines a
    player plays; this finds the ones costing them the year.
    """
    return round(losses + draws / 2, 1)


def rate(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def score(wins: int, draws: int, games: int) -> float:
    """Chess scoring: a win is a point, a draw is half of one."""
    return round((wins + draws / 2) / games, 4) if games else 0.0


def number(value: Any, digits: int = 2) -> float | None:
    """``AVG`` over a NUMERIC column comes back as ``Decimal``, which JSONB won't take."""
    return None if value is None else round(float(value), digits)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
