"""Enqueueing side of the worker, used by the API process.

Kept apart from ``jobs`` so that importing "how to queue a report" never drags
in the database session factory and the Lichess client that running one needs.
"""

import logging
import uuid

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.core.config import settings
from app.worker.jobs import BUILD_REPORT

logger = logging.getLogger(__name__)

_pool: ArqRedis | None = None


def redis_settings() -> RedisSettings:
    """arq's own Redis config, derived from the one URL the app is given."""
    return RedisSettings.from_dsn(settings.redis_url)


async def get_pool() -> ArqRedis:
    """The process-wide enqueue connection, opened on first use."""
    global _pool
    if _pool is None:
        _pool = await create_pool(redis_settings())
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


def job_id_for(report_id: uuid.UUID) -> str:
    """One arq job per report row.

    arq refuses to enqueue a job id it already holds, so deriving the id from
    the report makes a double-submit a no-op rather than a second export.
    """
    return f"report:{report_id}"


async def enqueue_report(report_id: uuid.UUID) -> None:
    """Queue the build job for an existing pending report."""
    pool = await get_pool()
    job = await pool.enqueue_job(BUILD_REPORT, str(report_id), _job_id=job_id_for(report_id))
    if job is None:
        # Not an error: the job is already queued or running, which is exactly
        # what the id is there to guarantee.
        logger.info("report %s is already queued; not enqueueing again", report_id)
