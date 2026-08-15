"""The progression section: rating over the window, per perf.

Built from the games rather than from Lichess's rating-history endpoint. The
games table already carries the rating before each game and the change it
caused, which is the same curve sampled at every game instead of once a day,
and it is scoped to the report's window without any further filtering.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import ARRAY, Integer, func, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Game
from app.report.sections._common import iso, number, window


async def build(
    session: AsyncSession, player_id: int, *, since: datetime, until: datetime
) -> dict[str, Any]:
    """A monthly rating series per perf, with start, end, peak and net change."""
    rows = await _monthly(session, player_id, since, until)
    by_perf = {}
    for perf in dict.fromkeys(row.perf for row in rows):
        by_perf[perf] = _summarise([row for row in rows if row.perf == perf])

    ranked = sorted(by_perf.items(), key=lambda item: -item[1]["games"])
    return {
        "by_perf": by_perf,
        "main_perf": ranked[0][0] if ranked else None,
        "best_gain": max(by_perf.items(), key=lambda i: i[1]["net"])[0] if by_perf else None,
    }


def _summarise(rows: list[Any]) -> dict[str, Any]:
    series = [
        {
            "month": iso(row.month),
            "games": row.games,
            "average": number(row.average, 0),
            "peak": row.peak,
            "low": row.low,
            "end": row.last_rating,
        }
        for row in rows
    ]
    start = rows[0].first_rating
    end = rows[-1].last_rating
    return {
        "games": sum(row.games for row in rows),
        "start": start,
        "end": end,
        "net": end - start,
        "peak": max(row.peak for row in rows),
        "low": min(row.low for row in rows),
        "series": series,
    }


def _first(value: Any, *order: Any) -> Any:
    """The first ``value`` in ``order``, as an aggregate over the group.

    Postgres has no ``FIRST()``: sorting inside ``array_agg`` and taking element
    one is the standard way, and it keeps the month series to a single query.
    """
    return func.array_agg(aggregate_order_by(value, *order), type_=ARRAY(Integer))[1]


async def _monthly(
    session: AsyncSession, player_id: int, since: datetime, until: datetime
) -> list[Any]:
    order = (Game.played_at, Game.game_id)
    # The rating a game left the player on. `player_rating` is what they went
    # into it with, so the last game of the window is the only place the closing
    # rating can come from.
    after = Game.player_rating + func.coalesce(Game.rating_diff, 0)
    month = func.date_trunc("month", Game.played_at).label("month")
    # Peak and low span every rating the player *held*, which is the rating they
    # carried into each game as well as the one they left it on. Taken over
    # `after` alone they would miss the opening rating, and a player who only
    # ever went down would be reported as peaking at their lowest point -- the
    # series starts at the rating before the first game, so the extremes have to
    # start there too. Neither column is NULL here: the WHERE excludes a missing
    # `player_rating` and `after` coalesces the diff, so GREATEST and LEAST
    # ignoring NULLs cannot bite.
    stmt = (
        select(
            Game.perf,
            month,
            func.count().label("games"),
            func.avg(after).label("average"),
            func.greatest(func.max(after), func.max(Game.player_rating)).label("peak"),
            func.least(func.min(after), func.min(Game.player_rating)).label("low"),
            _first(Game.player_rating, *order).label("first_rating"),
            _first(after, Game.played_at.desc(), Game.game_id.desc()).label("last_rating"),
        )
        .where(*window(player_id, since, until), Game.player_rating.is_not(None))
        .group_by(Game.perf, month)
        .order_by(Game.perf, month)
    )
    return list((await session.execute(stmt)).all())
