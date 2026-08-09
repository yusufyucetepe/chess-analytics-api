"""The latest finished report, cached in Redis under the player's name.

Read-through, and the database stays authoritative: every entry here is a copy
of a row that is still in ``reports``, so a cold or broken Redis costs a query
and never an answer. That is why the reads and writes swallow Redis failures --
a cache that can take the site down with it is worse than no cache at all.

Nothing writes an entry when a report finishes. The worker deletes instead, and
the next reader repopulates: invalidation is one command that cannot go stale,
whereas writing the payload from the job would have the worker rendering an API
response it never serves.
"""

import json
import logging
from collections.abc import Mapping
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import settings

logger = logging.getLogger(__name__)


def report_key(username: str) -> str:
    return f"report:latest:{username.lower()}"


async def load(redis: Redis, username: str) -> dict[str, Any] | None:
    """The cached report view, or ``None`` on a miss or an unreachable Redis."""
    try:
        raw = await redis.get(report_key(username))
    except RedisError as exc:
        logger.warning("report cache read for %s failed: %s", username, exc)
        return None
    if raw is None:
        return None
    cached: dict[str, Any] = json.loads(raw)
    return cached


async def store(redis: Redis, username: str, view: Mapping[str, Any]) -> None:
    """Cache a finished report view. ``view`` must already be JSON-native."""
    try:
        await redis.set(report_key(username), json.dumps(view), ex=settings.report_cache_ttl_s)
    except RedisError as exc:
        logger.warning("report cache write for %s failed: %s", username, exc)


async def forget(redis: Redis, username: str) -> None:
    """Drop the entry, so the next reader sees the report that just finished.

    Failure is logged rather than raised: this runs at the end of a job whose
    report is already written and marked done, and turning a cache miss-to-be
    into a failed report would be a worse outcome than an hour of staleness.
    """
    try:
        await redis.delete(report_key(username))
    except RedisError as exc:
        logger.warning("report cache invalidation for %s failed: %s", username, exc)
