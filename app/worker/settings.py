"""``arq app.worker.WorkerSettings`` -- what the worker container runs."""

import logging
from typing import Any, ClassVar

from arq.connections import RedisSettings

from app.core.config import settings
from app.db.base import SessionLocal, engine
from app.worker.jobs import build_report
from app.worker.queue import redis_settings

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    """``setdefault`` so a test can inject its own session factory via ``ctx``."""
    ctx.setdefault("session_factory", SessionLocal)
    logger.info("worker ready (max_tries=%d)", settings.worker_max_tries)


async def shutdown(ctx: dict[str, Any]) -> None:
    # Only the pool this process opened; an injected factory belongs to whoever
    # passed it in.
    if ctx.get("session_factory") is SessionLocal:
        await engine.dispose()


class WorkerSettings:
    """Read by arq off the class body, so every attribute is a plain value.

    ``arq.worker.get_kwargs`` picks these out of ``__dict__`` and hands them to
    ``Worker(...)`` unchanged -- a property or a method here would be passed
    along as the object itself. Parsing the Redis DSN opens no socket, so doing
    it at import time is safe.
    """

    functions: ClassVar[list[Any]] = [build_report]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings: RedisSettings = redis_settings()
    max_tries = settings.worker_max_tries
    job_timeout = settings.worker_job_timeout_s
    keep_result = settings.worker_keep_result_s
