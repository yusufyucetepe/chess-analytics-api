"""A fixed-window request counter in Redis.

Fixed window rather than sliding: it is two commands and stores no history, and
its known flaw -- up to twice the limit across a window boundary -- costs
nothing here. The limit exists so a public form cannot be turned into a Lichess
scraping proxy, and twenty exports in a bad minute is still not that.
"""

import time
from dataclasses import dataclass

from redis.asyncio import Redis


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    #: Seconds until the window rolls over, which is how long a refused caller
    #: has to wait. Served as ``Retry-After``.
    reset_in_s: int


async def hit(
    redis: Redis, key: str, *, limit: int, window_s: int, now: float | None = None
) -> Decision:
    """Count one request against ``key`` and say whether it is allowed."""
    moment = time.time() if now is None else now
    window, offset = divmod(int(moment), window_s)
    reset_in_s = window_s - offset
    counter = f"{key}:{window}"

    pipe = redis.pipeline()
    pipe.incr(counter)
    # Expiring at the end of the window rather than a full window from now, and
    # re-set on every hit rather than only the first: an INCR whose caller died
    # before it could set a TTL would otherwise leave a counter that never goes
    # away, locking that key out for good.
    pipe.expire(counter, reset_in_s)
    used: int = (await pipe.execute())[0]

    return Decision(
        allowed=used <= limit,
        limit=limit,
        remaining=max(0, limit - used),
        reset_in_s=reset_in_s,
    )
