"""``arq app.worker.WorkerSettings`` -- what the worker container runs."""

import logging
from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings

from app.core.config import settings
from app.core.redis import close_redis
from app.db.base import SessionLocal, engine
from app.worker.jobs import build_report, refresh_style_reference
from app.worker.queue import redis_settings

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    # `setdefault` so a test can inject its own session factory via `ctx`.
    ctx.setdefault("session_factory", SessionLocal)
    logger.info("worker ready (max_tries=%d)", settings.worker_max_tries)


async def shutdown(ctx: dict[str, Any]) -> None:
    await close_redis()
    # Only the pool this process opened; an injected factory belongs to whoever
    # passed it in.
    if ctx.get("session_factory") is SessionLocal:
        await engine.dispose()


class WorkerSettings:
    """Read by arq off the class body, so every attribute is a plain value.

    ``arq.worker.get_kwargs`` picks these out of ``__dict__`` and hands them to
    ``Worker(...)`` unchanged -- a property or a method would be passed along as
    the object itself. Parsing the Redis DSN opens no socket, so import time is
    safe for it.
    """

    functions: ClassVar[list[Any]] = [build_report]
    #: The style reference is a cache of a population, so it is rebuilt on a
    #: clock rather than by anything a user does. `run_at_startup` so a fresh
    #: deployment -- or a dev box -- has a centre without waiting for the hour.
    cron_jobs: ClassVar[list[Any]] = [
        cron(
            refresh_style_reference,
            hour=settings.style_reference_hour,
            minute=0,
            run_at_startup=True,
        )
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings: RedisSettings = redis_settings()
    max_tries = settings.worker_max_tries
    job_timeout = settings.worker_job_timeout_s
    keep_result = settings.worker_keep_result_s
