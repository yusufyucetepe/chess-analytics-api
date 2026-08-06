"""The ``build_report`` job: everything between a queued row and a finished one.

The job takes a report id and nothing else. Every other input is read from the
row, so a job that is retried, replayed from the queue, or enqueued by hand
behaves identically.
"""

import logging
import uuid
from typing import Any

from arq import Retry
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.db.base import SessionLocal
from app.db.models import Player, Report
from app.ingest import ingest_player
from app.lichess.client import LichessClient
from app.report import service
from app.report.builder import build_payload
from app.worker.failures import DEFAULT_MESSAGE, is_retryable, user_message

logger = logging.getLogger(__name__)

BUILD_REPORT = "build_report"


async def build_report(ctx: dict[str, Any], report_id: str) -> dict[str, Any]:
    """Build one report. Returns a small summary, which arq stores as the result."""
    session_factory: async_sessionmaker[AsyncSession] = ctx.get("session_factory") or SessionLocal
    attempt: int = ctx.get("job_try", 1)

    async with session_factory() as session:
        report = await service.get_report(session, uuid.UUID(report_id))
        if report is None:
            # The row was deleted between enqueue and pickup. Nothing to fail --
            # raising here would only have arq retry a job with no subject.
            logger.warning("report %s no longer exists; dropping the job", report_id)
            return {"report_id": report_id, "status": "missing"}

        player = await session.get(Player, report.player_id)
        if player is None:
            # Only reachable if the player was deleted under us; the FK rules it
            # out otherwise.
            await service.mark_failed(session, report, DEFAULT_MESSAGE)
            logger.error("report %s points at missing player %s", report_id, report.player_id)
            return {"report_id": report_id, "status": "failed"}

        await service.mark_running(session, report)
        logger.info(
            "building report %s for %s (attempt %d/%d)",
            report_id,
            player.username_lower,
            attempt,
            settings.worker_max_tries,
        )

        try:
            return await _run(session, report, player)
        except Exception as exc:
            return await _fail(session, report, player, exc, attempt)


async def _run(session: AsyncSession, report: Report, player: Player) -> dict[str, Any]:
    # A client per job rather than a shared one: an export holds a connection
    # open for minutes, and that timeout has no business being reused by the
    # short profile calls the API makes.
    async with LichessClient() as client:
        _, stats = await ingest_player(
            session,
            client,
            player.display_name,
            since=report.period_start,
            until=report.period_end,
        )

    # Counted from the table, not from `stats`: this run only saw what it
    # downloaded, while the report covers every game we hold for the window.
    games_total, games_analysed = await service.count_games(
        session, player.id, since=report.period_start, until=report.period_end
    )
    payload = build_payload(
        player=player,
        period_start=report.period_start,
        period_end=report.period_end,
        games_total=games_total,
        games_analysed=games_analysed,
    )
    await service.mark_done(
        session,
        report,
        payload=payload,
        games_total=games_total,
        games_analysed=games_analysed,
    )
    logger.info(
        "report %s done: %d games (%d analysed), %d fetched this run",
        report.id,
        games_total,
        games_analysed,
        stats.fetched,
    )
    return {
        "report_id": str(report.id),
        "status": "done",
        "games_total": games_total,
        "games_analysed": games_analysed,
    }


async def _fail(
    session: AsyncSession, report: Report, player: Player, exc: Exception, attempt: int
) -> dict[str, Any]:
    # `arq.Retry` is the only exception arq treats as "run this again" -- every
    # other one marks the job finished and failed. The retry budget is ours
    # rather than arq's: letting arq run out first would drop the job without
    # our code waking up, leaving the report stuck in `running` with nothing to
    # explain why. Only the last attempt writes `failed`, so a poller never sees
    # it fail and then quietly un-fail.
    #
    # The session may be mid-transaction from the failed ingest.
    await session.rollback()

    retryable = is_retryable(exc)
    if retryable and attempt < settings.worker_max_tries:
        delay = settings.worker_retry_delay_s * attempt
        logger.warning(
            "report %s failed on attempt %d/%d (%s); retrying in %ds",
            report.id,
            attempt,
            settings.worker_max_tries,
            exc,
            delay,
        )
        raise Retry(defer=delay) from exc

    logger.exception(
        "report %s for %s failed permanently after %d attempt(s)",
        report.id,
        player.username_lower,
        attempt,
        exc_info=exc,
    )
    await service.mark_failed(session, report, user_message(exc))
    return {"report_id": str(report.id), "status": "failed", "retryable": retryable}
