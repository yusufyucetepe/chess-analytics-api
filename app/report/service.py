"""The ``reports`` table as a job record.

There is no jobs table: a report row *is* the job, so its status column is the
single place the API and the worker agree on what is happening. Every state
transition lives here rather than being scattered across the route handlers and
the arq task, because "who is allowed to set ``completed_at``" is exactly the
kind of question that gets answered twice, differently.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Game, Player, Report, ReportStatus

#: Neither finished nor failed: a job someone is already meant to be running.
ACTIVE_STATUSES = (ReportStatus.PENDING, ReportStatus.RUNNING)


def report_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """The rolling window a new report covers, ending now."""
    end = now or datetime.now(UTC)
    return end - timedelta(days=settings.report_window_days), end


async def get_report(session: AsyncSession, report_id: uuid.UUID) -> Report | None:
    return await session.get(Report, report_id)


async def player_by_username(session: AsyncSession, username: str) -> Player | None:
    """Look a player up the way Lichess does: case-insensitively."""
    stmt = select(Player).where(Player.username_lower == username.lower())
    return (await session.execute(stmt)).scalar_one_or_none()


async def fresh_report(session: AsyncSession, player_id: int) -> Report | None:
    """The player's most recent completed report, if it is still young enough.

    This is the "don't re-scrape Lichess for a page refresh" rule. Step 7 puts a
    Redis cache in front of it; the database answer stays authoritative.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.report_fresh_ttl_s)
    stmt = (
        select(Report)
        .where(
            Report.player_id == player_id,
            Report.status == ReportStatus.DONE,
            Report.completed_at >= cutoff,
        )
        .order_by(Report.completed_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def latest_completed(session: AsyncSession, player_id: int) -> Report | None:
    """The newest finished report at any age -- what the read endpoints serve."""
    stmt = (
        select(Report)
        .where(Report.player_id == player_id, Report.status == ReportStatus.DONE)
        .order_by(Report.completed_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def active_report(session: AsyncSession, player_id: int) -> Report | None:
    """A job already queued or running for this player, if there is one.

    Bounded by ``ingest_lock_ttl_s``: a worker killed mid-ingest leaves a row
    stuck in ``running``, and without an age limit that one row would block the
    player from ever getting a report again. Step 7 replaces this with the real
    Redis lock, which expires on its own.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.ingest_lock_ttl_s)
    stmt = (
        select(Report)
        .where(
            Report.player_id == player_id,
            Report.status.in_(ACTIVE_STATUSES),
            Report.created_at >= cutoff,
        )
        .order_by(Report.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_report(
    session: AsyncSession, player_id: int, *, period_start: datetime, period_end: datetime
) -> Report:
    """Open a pending report. The caller enqueues the job afterwards."""
    report = Report(
        player_id=player_id,
        period_start=period_start,
        period_end=period_end,
        status=ReportStatus.PENDING,
    )
    session.add(report)
    await session.commit()
    await session.refresh(report)
    return report


async def mark_running(session: AsyncSession, report: Report) -> None:
    report.status = ReportStatus.RUNNING
    # Cleared in case this is a retry: the previous attempt's message is no
    # longer what is happening.
    report.error = None
    await session.commit()


async def mark_done(
    session: AsyncSession,
    report: Report,
    *,
    payload: dict[str, Any],
    games_total: int,
    games_analysed: int,
) -> None:
    report.status = ReportStatus.DONE
    report.payload = payload
    report.games_total = games_total
    report.games_analysed = games_analysed
    report.completed_at = datetime.now(UTC)
    report.error = None
    await session.commit()


async def mark_failed(session: AsyncSession, report: Report, message: str) -> None:
    """Record a user-safe message. Never a traceback -- this is served over HTTP."""
    report.status = ReportStatus.FAILED
    report.error = message
    report.completed_at = datetime.now(UTC)
    await session.commit()


async def count_games(
    session: AsyncSession, player_id: int, *, since: datetime, until: datetime
) -> tuple[int, int]:
    """Games stored in the window, and how many of them carry engine analysis.

    Counted from the database rather than from what this run happened to fetch:
    a re-ingest only sees the games it downloaded, but the report covers every
    game we hold for the window. Aggregated in SQL, not by loading the rows.
    """
    stmt = select(
        func.count(),
        func.count().filter(Game.analysed),
    ).where(
        Game.player_id == player_id,
        Game.played_at >= since,
        Game.played_at <= until,
    )
    total, analysed = (await session.execute(stmt)).one()
    return int(total), int(analysed)
