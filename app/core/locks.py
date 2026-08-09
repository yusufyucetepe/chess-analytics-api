"""The ingest lock: one export per username at a time, across every process.

Not a general-purpose distributed lock. It has exactly one job -- make a second
request for a username that is already being built join the first instead of
starting a second export -- so the value stored under the key is the id of the
report the lock was taken for. A caller that loses the race is then told what to
poll rather than just being told "no".
"""

from collections.abc import Awaitable
from typing import Any, cast

from redis.asyncio import Redis

#: Delete only if we are still the holder. A worker that overran
#: ``ingest_lock_ttl_s`` would otherwise release a lock a later request had
#: already taken, and that request's export would run unprotected.
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def report_lock_key(username: str) -> str:
    return f"lock:report:{username.lower()}"


async def acquire(redis: Redis, key: str, token: str, ttl_s: int) -> str:
    """Take the lock, and return the token of whoever ends up holding it.

    Getting ``token`` back means we won. Anything else is the current holder,
    which the caller can use to find the work already in flight.
    """
    for _ in range(2):
        if await redis.set(key, token, nx=True, ex=ttl_s):
            return token
        if (holder := await redis.get(key)) is not None:
            return str(holder)
    # Twice we lost the SET and then found no holder, so the lock is expiring as
    # fast as it is being taken. Claiming it beats spinning: the worst case is
    # one duplicate job, and the alternative is a request that never resolves.
    return token


async def force_acquire(redis: Redis, key: str, token: str, ttl_s: int) -> None:
    """Take a lock out from under its holder.

    Only for a lock whose subject has gone -- the report row it names no longer
    exists, so nothing is being built and the key is purely in the way.
    """
    await redis.set(key, token, ex=ttl_s)


async def release(redis: Redis, key: str, token: str) -> bool:
    """Give the lock up. False means it had already moved on without us."""
    # redis-py annotates `eval` with the sync client's return type, so the async
    # one needs telling that what comes back is awaitable.
    deleted = await cast(Awaitable[Any], redis.eval(_RELEASE, 1, key, token))
    return bool(deleted)
