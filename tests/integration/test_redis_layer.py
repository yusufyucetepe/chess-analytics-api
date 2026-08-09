"""The lock, the counter and the cache, against a real Redis.

Not unit tests with a fake: all three are made almost entirely of Redis
semantics -- ``SET NX``, an expiring counter, a TTL -- so a stand-in would be
asserting that my model of Redis matches my model of Redis. The one thing worth
faking is a Redis that is down, which is hard to arrange on purpose.
"""

import json
from typing import Any, cast

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core import locks, ratelimit
from app.core.config import settings
from app.report import cache

KEY = "lock:test"
HOUR = 3600


class Unreachable:
    """A Redis that refuses every command, the way a dead one does.

    Doubles as its own pipeline: queueing commands is local work that succeeds
    even when the server is gone, so ``execute`` is where a real one would fail.
    """

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        raise RedisError("connection refused")

    async def set(self, *args: Any, **kwargs: Any) -> Any:
        raise RedisError("connection refused")

    async def delete(self, *args: Any, **kwargs: Any) -> Any:
        raise RedisError("connection refused")

    def pipeline(self, *args: Any, **kwargs: Any) -> "Unreachable":
        return self

    def incr(self, *args: Any, **kwargs: Any) -> "Unreachable":
        return self

    def expire(self, *args: Any, **kwargs: Any) -> "Unreachable":
        return self

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise RedisError("connection refused")


class Vanishing:
    """A Redis whose keys are gone the instant after they are written.

    Not a state a real server reaches, but it is the shape of one: a lock with a
    TTL so short that every ``SET NX`` loses to a holder that has already gone.
    """

    async def set(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        return None


def broken() -> Redis:
    return cast(Redis, Unreachable())


# -------------------------------------------------------------------- locks


async def test_an_uncontested_lock_is_ours(redis_client: Redis) -> None:
    assert await locks.acquire(redis_client, KEY, "mine", 60) == "mine"


async def test_the_second_caller_is_told_who_holds_it(redis_client: Redis) -> None:
    """Not just "no": the holder is a report id, which is what to poll."""
    await locks.acquire(redis_client, KEY, "first", 60)

    assert await locks.acquire(redis_client, KEY, "second", 60) == "first"


async def test_the_lock_expires_on_its_own(redis_client: Redis) -> None:
    """A worker killed mid-ingest must not lock a player out for good."""
    await locks.acquire(redis_client, KEY, "mine", 60)

    assert 0 < await redis_client.ttl(KEY) <= 60


async def test_releasing_lets_the_next_caller_in(redis_client: Redis) -> None:
    await locks.acquire(redis_client, KEY, "first", 60)

    assert await locks.release(redis_client, KEY, "first") is True
    assert await locks.acquire(redis_client, KEY, "second", 60) == "second"


async def test_a_slow_worker_cannot_release_someone_elses_lock(redis_client: Redis) -> None:
    """The reason release is a compare-and-delete rather than a delete.

    An overrunning job whose lock has expired and been re-taken would otherwise
    unlock the export that replaced it, and two of them would run at once.
    """
    await locks.acquire(redis_client, KEY, "second", 60)

    assert await locks.release(redis_client, KEY, "first") is False
    assert await redis_client.get(KEY) == "second"


async def test_releasing_a_lock_that_is_already_gone_is_not_an_error(
    redis_client: Redis,
) -> None:
    assert await locks.release(redis_client, KEY, "mine") is False


async def test_forcing_takes_the_lock_from_its_holder(redis_client: Redis) -> None:
    await locks.acquire(redis_client, KEY, "stale", 60)

    await locks.force_acquire(redis_client, KEY, "mine", 60)

    assert await redis_client.get(KEY) == "mine"
    assert 0 < await redis_client.ttl(KEY) <= 60


async def test_a_lock_nobody_can_hold_is_claimed_rather_than_spun_on() -> None:
    """The last resort. Losing the SET and then finding no holder means the key
    is expiring as fast as it is taken; the caller still has to be given an
    answer, and one duplicate export beats a request that never returns."""
    assert await locks.acquire(cast(Redis, Vanishing()), KEY, "mine", 60) == "mine"


async def test_the_lock_key_is_the_lowercased_username() -> None:
    assert locks.report_lock_key("Zhigalko_Sergei") == locks.report_lock_key("zhigalko_sergei")


# --------------------------------------------------------------- rate limit


async def test_the_first_request_is_allowed_and_counted(redis_client: Redis) -> None:
    decision = await ratelimit.hit(redis_client, "rl", limit=10, window_s=HOUR)

    assert decision.allowed is True
    assert decision.remaining == 9
    assert 0 < decision.reset_in_s <= HOUR


async def test_the_request_past_the_limit_is_refused(redis_client: Redis) -> None:
    for _ in range(3):
        await ratelimit.hit(redis_client, "rl", limit=3, window_s=HOUR)

    decision = await ratelimit.hit(redis_client, "rl", limit=3, window_s=HOUR)

    assert decision.allowed is False
    assert decision.remaining == 0, "never negative, however far over they go"


async def test_two_callers_have_their_own_budgets(redis_client: Redis) -> None:
    await ratelimit.hit(redis_client, "rl:a", limit=1, window_s=HOUR)

    assert (await ratelimit.hit(redis_client, "rl:b", limit=1, window_s=HOUR)).allowed is True


async def test_the_next_window_starts_over(redis_client: Redis) -> None:
    """Fixed window, so the reset is a clock boundary rather than a rolling hour."""
    await ratelimit.hit(redis_client, "rl", limit=1, window_s=HOUR, now=HOUR * 100)

    refused = await ratelimit.hit(redis_client, "rl", limit=1, window_s=HOUR, now=HOUR * 100 + 5)
    allowed = await ratelimit.hit(redis_client, "rl", limit=1, window_s=HOUR, now=HOUR * 101)

    assert refused.allowed is False
    assert allowed.allowed is True


async def test_the_counter_expires_at_the_end_of_its_window(redis_client: Redis) -> None:
    """An INCR whose caller died before setting a TTL would lock a key out forever.

    So the expiry is re-set on every hit, and always to the end of the window --
    a full window from now would keep the key alive past its own reset.
    """
    await ratelimit.hit(redis_client, "rl", limit=10, window_s=HOUR, now=HOUR * 100 + 3540)

    ttl = await redis_client.ttl(f"rl:{100}")
    assert 0 < ttl <= 60


# --------------------------------------------------------------- report cache


def view(**overrides: Any) -> dict[str, Any]:
    return {"id": "abc", "status": "done", "payload": {"hello": "world"}, **overrides}


async def test_a_stored_report_comes_back_unchanged(redis_client: Redis) -> None:
    await cache.store(redis_client, "Tester", view())

    assert await cache.load(redis_client, "tester") == view()


async def test_a_player_with_nothing_cached_is_a_miss(redis_client: Redis) -> None:
    assert await cache.load(redis_client, "nobody") is None


async def test_forgetting_a_report_is_a_miss_afterwards(redis_client: Redis) -> None:
    await cache.store(redis_client, "tester", view())

    await cache.forget(redis_client, "tester")

    assert await cache.load(redis_client, "tester") is None


async def test_the_entry_carries_the_configured_ttl(
    redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It has to expire on its own: nothing re-reads the table to check."""
    monkeypatch.setattr(settings, "report_cache_ttl_s", 120)

    await cache.store(redis_client, "tester", view())

    assert 0 < await redis_client.ttl(cache.report_key("tester")) <= 120


async def test_the_cache_is_keyed_case_insensitively(redis_client: Redis) -> None:
    await cache.store(redis_client, "Zhigalko_Sergei", view())

    assert await cache.load(redis_client, "ZHIGALKO_SERGEI") is not None


async def test_a_dead_redis_reads_as_a_miss_rather_than_an_error() -> None:
    """The table is authoritative, so a broken cache costs a query, not an answer."""
    assert await cache.load(broken(), "tester") is None


async def test_a_dead_redis_swallows_writes_and_invalidations() -> None:
    """The write happens after the report is already served or already done."""
    await cache.store(broken(), "tester", view())
    await cache.forget(broken(), "tester")


async def test_a_corrupt_entry_is_not_quietly_served(redis_client: Redis) -> None:
    """Only this module writes the key, so junk in it is a bug worth hearing about."""
    await redis_client.set(cache.report_key("tester"), "not json")

    with pytest.raises(json.JSONDecodeError):
        await cache.load(redis_client, "tester")
