"""The ``build_report`` job: everything between a queued row and a finished one.

The job takes a report id and nothing else. Every other input is read from the
row, so a job that is retried, replayed from the queue, or enqueued by hand
behaves identically.
"""

import logging
import uuid
from typing import Any

from arq import Retry
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import locks
from app.core.config import settings
from app.core.redis import get_redis
from app.db.base import SessionLocal
from app.db.models import Player, Report
from app.ingest import ingest_player
from app.lichess.client import LichessClient
from app.report import cache, service, style_reference
from app.report.builder import build_payload
from app.report.sections import build_sections
from app.worker.failures import DEFAULT_MESSAGE, is_retryable, user_message

logger = logging.getLogger(__name__)

BUILD_REPORT = "build_report"
REFRESH_STYLE_REFERENCE = "refresh_style_reference"


async def refresh_style_reference(ctx: dict[str, Any]) -> dict[str, int]:
    """Recompute where the middle of each rating band sits. Runs on a cron.

    Cheap and idempotent: it reads finished payloads and writes one row per
    (time control, band). Scheduled rather than done at report time because the
    answer barely moves between two reports and every report would otherwise
    pay for the scan.
    """
    session_factory: async_sessionmaker[AsyncSession] = ctx.get("session_factory") or SessionLocal
    async with session_factory() as session:
        bands = await style_reference.refresh(session)
    logger.info(
        "style reference refreshed: %d band(s), %d player(s)",
        len(bands),
        sum(band.players for band in bands),
    )
    return {"bands": len(bands), "players": sum(band.players for band in bands)}


async def build_report(ctx: dict[str, Any], report_id: str) -> dict[str, Any]:
    """Build one report. Returns a small summary, which arq stores as the result."""
    session_factory: async_sessionmaker[AsyncSession] = ctx.get("session_factory") or SessionLocal
    # Deliberately not `ctx["redis"]`: that is arq's own pool, which speaks bytes
    # and whose keyspace belongs to the queue.
    redis: Redis = get_redis()
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
            result = await _run(session, redis, report, player)
        except Exception as exc:
            # When another attempt is due this re-raises as `arq.Retry`, which
            # skips the release below on purpose: the lock has to outlive the
            # attempt, or a request arriving during the backoff would start a
            # second export of the same year.
            result = await _fail(session, report, player, exc, attempt)

        await locks.release(redis, locks.report_lock_key(player.username_lower), report_id)
        return result


async def _run(
    session: AsyncSession, redis: Redis, report: Report, player: Player
) -> dict[str, Any]:
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
    sections = await build_sections(
        session,
        player.id,
        since=report.period_start,
        until=report.period_end,
        games_analysed=games_analysed,
    )
    payload = build_payload(
        player=player,
        period_start=report.period_start,
        period_end=report.period_end,
        games_total=games_total,
        games_analysed=games_analysed,
        sections=sections,
    )
    await service.mark_done(
        session,
        report,
        payload=payload,
        games_total=games_total,
        games_analysed=games_analysed,
    )
    # Evicted rather than overwritten: the next reader repopulates from the row,
    # so the cache can never hold a rendering the table disagrees with.
    await cache.forget(redis, player.username_lower)
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
    # Read before the rollback below, not after. `rollback` expires every object
    # the transaction touched, and the next attribute read reloads it -- from a
    # synchronous __get__, which on an async session raises MissingGreenlet.
    # That exception would replace the real one and escape this function, so the
    # report would stay `running` for ever: precisely what the retry budget
    # above exists to prevent, defeated by a logging argument.
    report_id, username = report.id, player.username_lower

    # The session may be mid-transaction from the failed ingest.
    await session.rollback()

    retryable = is_retryable(exc)
    if retryable and attempt < settings.worker_max_tries:
        delay = settings.worker_retry_delay_s * attempt
        logger.warning(
            "report %s failed on attempt %d/%d (%s); retrying in %ds",
            report_id,
            attempt,
            settings.worker_max_tries,
            exc,
            delay,
        )
        raise Retry(defer=delay) from exc

    logger.exception(
        "report %s for %s failed permanently after %d attempt(s)",
        report_id,
        username,
        attempt,
        exc_info=exc,
    )
    await service.mark_failed(session, report, user_message(exc))
    return {"report_id": str(report_id), "status": "failed", "retryable": retryable}
