"""The quality section: the only one built on a biased sample.

We never run an engine, so accuracy and ACPL exist only for the games the player
asked Lichess to analyse. Below ``quality_min_analysed_games`` the section
returns no numbers at all rather than an average of a handful of self-selected
games -- see the coverage rule in docs/BRIEF.md.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Game
from app.report.builder import quality_reliable
from app.report.sections._common import iso, number, window


async def build(
    session: AsyncSession,
    player_id: int,
    *,
    since: datetime,
    until: datetime,
    games_analysed: int,
) -> dict[str, Any]:
    """Accuracy, ACPL and mistake counts over the analysed games, or a refusal."""
    base = {
        "games_analysed": games_analysed,
        "min_analysed_games": settings.quality_min_analysed_games,
    }
    if not quality_reliable(games_analysed):
        return {**base, "available": False, "totals": None, "by_month": [], "by_week": []}

    return {
        **base,
        "available": True,
        "totals": await _totals(session, player_id, since, until),
        "by_month": await _buckets(session, player_id, since, until, "month"),
        "by_week": await _buckets(session, player_id, since, until, "week"),
    }


def _aggregates() -> tuple[Any, ...]:
    return (
        func.count().label("games"),
        func.avg(Game.accuracy).label("accuracy"),
        func.avg(Game.acpl).label("acpl"),
        func.coalesce(func.sum(Game.inaccuracies), 0).label("inaccuracies"),
        func.coalesce(func.sum(Game.mistakes), 0).label("mistakes"),
        func.coalesce(func.sum(Game.blunders), 0).label("blunders"),
    )


def _shape(row: Any) -> dict[str, Any]:
    return {
        "games": row.games,
        "accuracy": number(row.accuracy),
        "acpl": number(row.acpl, 1),
        "inaccuracies": int(row.inaccuracies),
        "mistakes": int(row.mistakes),
        "blunders": int(row.blunders),
        "blunders_per_game": number(row.blunders / row.games, 2) if row.games else None,
        "mistakes_per_game": number(row.mistakes / row.games, 2) if row.games else None,
    }


async def _totals(
    session: AsyncSession, player_id: int, since: datetime, until: datetime
) -> dict[str, Any]:
    stmt = select(*_aggregates()).where(*window(player_id, since, until), Game.analysed.is_(True))
    return _shape((await session.execute(stmt)).one())


async def _buckets(
    session: AsyncSession, player_id: int, since: datetime, until: datetime, unit: str
) -> list[dict[str, Any]]:
    # `unit` is never user input -- it comes from the two call sites above -- so
    # interpolating it into date_trunc cannot be turned into an injection.
    bucket = func.date_trunc(unit, Game.played_at).label("bucket")
    stmt = (
        select(bucket, *_aggregates())
        .where(*window(player_id, since, until), Game.analysed.is_(True))
        .group_by(bucket)
        .order_by(bucket)
    )
    rows = (await session.execute(stmt)).all()
    return [{"start": iso(row.bucket), **_shape(row)} for row in rows]
