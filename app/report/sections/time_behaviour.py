"""The time section: when the player plays, and how well at those times.

Everything here is in UTC. We do not know the player's timezone -- Lichess does
not publish it -- so the payload says so and the frontend can offer a shift.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import Integer, Text, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Game, Result
from app.report.sections._common import iso, rate, record, result_counts, window

TIMEZONE = "UTC"

#: 1 = Monday, matching Postgres's ISO day-of-week.
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


async def build(
    session: AsyncSession, player_id: int, *, since: datetime, until: datetime
) -> dict[str, Any]:
    """Hour and weekday distributions, the longest session, and a tilt signal."""
    by_hour = await _by_hour(session, player_id, since, until)
    by_weekday = await _by_weekday(session, player_id, since, until)
    played = [slot for slot in by_hour if slot["games"]]
    return {
        "timezone": TIMEZONE,
        "by_hour": by_hour,
        "by_weekday": by_weekday,
        "peak_hour": max(played, key=lambda slot: slot["games"])["hour"] if played else None,
        "longest_session": await _longest_session(session, player_id, since, until),
        "tilt": await _tilt(session, player_id, since, until),
    }


async def _by_hour(
    session: AsyncSession, player_id: int, since: datetime, until: datetime
) -> list[dict[str, Any]]:
    hour = cast(func.extract("hour", Game.played_at), Integer).label("bucket")
    rows = await _distribution(session, player_id, since, until, hour)
    found = {row.bucket: row for row in rows}
    # Every hour is present even when empty: a chart with holes in it is worse
    # than one with zeroes, and the frontend should not have to fill them.
    return [_slot({"hour": h}, found.get(h)) for h in range(24)]


async def _by_weekday(
    session: AsyncSession, player_id: int, since: datetime, until: datetime
) -> list[dict[str, Any]]:
    day = cast(func.extract("isodow", Game.played_at), Integer).label("bucket")
    rows = await _distribution(session, player_id, since, until, day)
    found = {row.bucket: row for row in rows}
    return [_slot({"weekday": d, "name": WEEKDAYS[d - 1]}, found.get(d)) for d in range(1, 8)]


async def _distribution(
    session: AsyncSession, player_id: int, since: datetime, until: datetime, bucket: Any
) -> list[Any]:
    stmt = (
        select(bucket, func.count().label("games"), *result_counts())
        .where(*window(player_id, since, until))
        .group_by(bucket)
    )
    return list((await session.execute(stmt)).all())


def _slot(label: dict[str, Any], row: Any) -> dict[str, Any]:
    if row is None:
        return {**label, "games": 0, "wins": 0, "draws": 0, "losses": 0, "win_rate": 0.0}
    return {**label, **{k: v for k, v in record(row).items() if k != "score"}}


async def _longest_session(
    session: AsyncSession, player_id: int, since: datetime, until: datetime
) -> dict[str, Any] | None:
    # A session breaks wherever the gap to the previous game exceeds the
    # threshold. The running sum of those breaks numbers the sessions, which
    # turns "longest sitting" into a group-by like everything else here.
    order = (Game.played_at, Game.game_id)
    gap = func.extract("epoch", Game.played_at - func.lag(Game.played_at).over(order_by=order))
    marked = (
        select(
            Game.played_at,
            Game.game_id,
            case((gap > settings.report_session_gap_s, 1), else_=0).label("boundary"),
        )
        .where(*window(player_id, since, until))
        .subquery()
    )
    numbered = select(
        marked.c.played_at,
        func.sum(marked.c.boundary)
        .over(order_by=(marked.c.played_at, marked.c.game_id))
        .label("session"),
    ).subquery()
    stmt = (
        select(
            func.count().label("games"),
            func.min(numbered.c.played_at).label("start"),
            func.max(numbered.c.played_at).label("end"),
        )
        .group_by(numbered.c.session)
        .order_by(func.count().desc(), func.min(numbered.c.played_at))
        .limit(1)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    return {
        "games": row.games,
        "start": iso(row.start),
        "end": iso(row.end),
        "minutes": round((row.end - row.start).total_seconds() / 60, 1),
        "gap_minutes": settings.report_session_gap_s // 60,
    }


async def _tilt(
    session: AsyncSession, player_id: int, since: datetime, until: datetime
) -> dict[str, Any] | None:
    """Win rate in games played straight after two losses, against the baseline."""
    order = (Game.played_at, Game.game_id)
    # Cast to text inside the lag: a window function over an enum column loses
    # its type on the way out of the subquery, and Postgres has no operator for
    # comparing that to a bare string.
    previous = cast(Game.result, Text)
    sub = (
        select(
            Game.result,
            func.lag(previous, 1).over(order_by=order).label("prev1"),
            func.lag(previous, 2).over(order_by=order).label("prev2"),
        )
        .where(*window(player_id, since, until))
        .subquery()
    )
    stmt = select(
        func.count().label("games"),
        func.count().filter(sub.c.result == Result.WIN).label("wins"),
    ).where(sub.c.prev1 == Result.LOSS.value, sub.c.prev2 == Result.LOSS.value)
    row = (await session.execute(stmt)).one()
    if not row.games:
        return None

    overall = select(
        func.count().label("games"),
        func.count().filter(Game.result == Result.WIN).label("wins"),
    ).where(*window(player_id, since, until))
    base = (await session.execute(overall)).one()

    after = rate(row.wins, row.games)
    baseline = rate(base.wins, base.games)
    return {
        "games_after_two_losses": row.games,
        "win_rate": after,
        "baseline_win_rate": baseline,
        "delta": round(after - baseline, 4),
    }
