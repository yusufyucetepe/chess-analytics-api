"""Injectable collaborators for the v1 routes.

Both of these reach outside the process -- Lichess over HTTP, arq over Redis --
so they go through ``Depends`` rather than being imported into the handlers
directly. That is what lets a test swap in a recorder without a live queue.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Final

from fastapi import Depends, HTTPException, Request, Response, status
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core import ratelimit
from app.core.config import settings
from app.core.lichess import get_lichess
from app.core.redis import get_redis
from app.lichess.client import LichessClient
from app.worker.queue import enqueue_report

logger = logging.getLogger(__name__)

#: Queue a build job for a report that already exists.
EnqueueReport = Callable[[uuid.UUID], Awaitable[None]]

#: ``rate_limit_per_hour`` names the window, so it is not a setting of its own.
RATE_LIMIT_WINDOW_S: Final = 3600


def get_enqueue() -> EnqueueReport:
    return enqueue_report


def get_lichess_client() -> LichessClient:
    return get_lichess()


def client_ip(request: Request) -> str:
    """Who to charge a request to.

    Behind a proxy this is only right if the server was started with
    ``--proxy-headers``; uvicorn rewrites ``request.client`` from
    ``X-Forwarded-For`` there, and trusting the header ourselves would let any
    caller pick their own bucket. A request with no peer at all -- which ASGI
    permits -- shares one bucket, which is the safe direction to be wrong in.
    """
    return request.client.host if request.client else "unknown"


async def enforce_rate_limit(
    request: Request, response: Response, redis: Redis = Depends(get_redis)
) -> None:
    """Cap how many reports one address can ask for per hour.

    On the POST only, and before it reaches Lichess: this is what stops a public
    form from being turned into a scraping proxy, so the useful place to spend
    the check is ahead of the export it would cause.
    """
    await _enforce(
        request,
        response,
        redis,
        bucket="reports",
        limit=settings.rate_limit_per_hour,
        # Failing open would leave the Lichess export unmetered, and the job
        # this request wants to enqueue needs the same Redis regardless.
        fail_closed=True,
        unavailable="Can't take new report requests right now. Try again shortly.",
    )


async def enforce_read_rate_limit(
    request: Request, response: Response, redis: Redis = Depends(get_redis)
) -> None:
    """Cap the reads that recompute rather than serve a stored row.

    The openings tree is several aggregates over a player's whole year, behind
    no cache and no account. One address in a loop is enough to make the
    database everyone else's problem, and nothing else on the read path bounds
    that.
    """
    await _enforce(
        request,
        response,
        redis,
        bucket="reads",
        limit=settings.rate_limit_reads_per_hour,
        # Unlike the POST, this one fails open: the request it guards touches
        # only Postgres, and the rest of the read path is built to survive Redis
        # being down. Losing a ceiling beats refusing to serve.
        fail_closed=False,
        unavailable="",
    )


async def _enforce(
    request: Request,
    response: Response,
    redis: Redis,
    *,
    bucket: str,
    limit: int,
    fail_closed: bool,
    unavailable: str,
) -> None:
    if limit <= 0:
        return

    try:
        decision = await ratelimit.hit(
            redis,
            f"ratelimit:{bucket}:{client_ip(request)}",
            limit=limit,
            window_s=RATE_LIMIT_WINDOW_S,
        )
    except RedisError as exc:
        logger.error("rate limiter unavailable for %s: %s", bucket, exc)
        if not fail_closed:
            return
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, unavailable) from exc

    headers = {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset": str(decision.reset_in_s),
    }
    if not decision.allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Only {decision.limit} requests an hour from one address. "
            "Someone else's year is being wrapped; try again shortly.",
            headers={**headers, "Retry-After": str(decision.reset_in_s)},
        )
    response.headers.update(headers)
