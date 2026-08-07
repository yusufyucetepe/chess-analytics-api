"""The headline section: the numbers a player would repeat out loud."""

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Game, Result
from app.report.sections._common import iso, record, result_counts, window


async def build(
    session: AsyncSession, player_id: int, *, since: datetime, until: datetime
) -> dict[str, Any]:
    """Volume, time at the board, record, best win, longest streak, busiest day."""
    totals = await _totals(session, player_id, since, until)
    if not totals["games"]:
        return {
            **totals,
            "by_perf": [],
            "best_win": None,
            "longest_win_streak": None,
            "busiest_day": None,
        }
    return {
        **totals,
        "by_perf": await _by_perf(session, player_id, since, until),
        "best_win": await _best_win(session, player_id, since, until),
        "longest_win_streak": await _longest_win_streak(session, player_id, since, until),
        "busiest_day": await _busiest_day(session, player_id, since, until),
    }


async def _totals(
    session: AsyncSession, player_id: int, since: datetime, until: datetime
) -> dict[str, Any]:
    # `coalesce` inside `least`, not around it: Postgres's LEAST ignores NULLs,
    # so `least(NULL, cap)` is the cap -- every game we have no duration for
    # would otherwise count as the maximum.
    played = func.least(func.coalesce(Game.duration_s, 0), settings.report_max_game_s)
    stmt = select(
        func.count().label("games"),
        *result_counts(),
        func.coalesce(func.sum(played), 0).label("seconds"),
        func.coalesce(func.sum(Game.moves_count), 0).label("plies"),
        func.count(func.distinct(func.date_trunc("day", Game.played_at))).label("days"),
    ).where(*window(player_id, since, until))
    row = (await session.execute(stmt)).one()
    return {
        **record(row),
        "seconds_played": int(row.seconds),
        "hours_played": round(row.seconds / 3600, 1),
        "moves_played": int(row.plies) // 2,
        "days_played": row.days,
    }


async def _by_perf(
    session: AsyncSession, player_id: int, since: datetime, until: datetime
) -> list[dict[str, Any]]:
    stmt = (
        select(Game.perf, func.count().label("games"), *result_counts())
        .where(*window(player_id, since, until))
        .group_by(Game.perf)
        .order_by(func.count().desc(), Game.perf)
    )
    rows = (await session.execute(stmt)).all()
    return [{"perf": row.perf, **record(row)} for row in rows]


async def _best_win(
    session: AsyncSession, player_id: int, since: datetime, until: datetime
) -> dict[str, Any] | None:
    stmt = (
        select(
            Game.game_id,
            Game.played_at,
            Game.perf,
            Game.color,
            Game.opponent_name,
            Game.opponent_rating,
            Game.player_rating,
        )
        .where(
            *window(player_id, since, until),
            Game.result == Result.WIN,
            Game.opponent_rating.is_not(None),
        )
        .order_by(Game.opponent_rating.desc(), Game.played_at.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    gap = None
    if row.player_rating is not None:
        gap = row.opponent_rating - row.player_rating
    return {
        "game_id": row.game_id,
        "url": f"{settings.lichess_base_url}/{row.game_id}",
        "played_at": iso(row.played_at),
        "perf": row.perf,
        "color": row.color.value,
        "opponent": row.opponent_name,
        "opponent_rating": row.opponent_rating,
        "player_rating": row.player_rating,
        "rating_gap": gap,
    }


async def _longest_win_streak(
    session: AsyncSession, player_id: int, since: datetime, until: datetime
) -> dict[str, Any] | None:
    # Gaps and islands: numbering all games and numbering only the wins gives a
    # difference that stays constant for as long as the wins stay consecutive,
    # so grouping on it turns "longest run" into a plain aggregate.
    order = (Game.played_at, Game.game_id)
    island = func.row_number().over(order_by=order) - func.row_number().over(
        partition_by=Game.result, order_by=order
    )
    runs = (
        select(Game.played_at, Game.result, island.label("island"))
        .where(*window(player_id, since, until))
        .subquery()
    )
    stmt = (
        select(
            func.count().label("length"),
            func.min(runs.c.played_at).label("start"),
            func.max(runs.c.played_at).label("end"),
        )
        .where(runs.c.result == Result.WIN)
        .group_by(runs.c.island)
        .order_by(func.count().desc(), func.min(runs.c.played_at))
        .limit(1)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    return {"games": row.length, "start": iso(row.start), "end": iso(row.end)}


async def _busiest_day(
    session: AsyncSession, player_id: int, since: datetime, until: datetime
) -> dict[str, Any]:
    day = func.date_trunc("day", Game.played_at).label("day")
    stmt = (
        select(day, func.count().label("games"), *result_counts())
        .where(*window(player_id, since, until))
        .group_by(day)
        .order_by(func.count().desc(), day)
        .limit(1)
    )
    row = (await session.execute(stmt)).one()
    return {"date": row.day.date().isoformat(), **record(row)}
