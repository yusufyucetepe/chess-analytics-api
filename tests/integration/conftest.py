"""Fixtures shared by the integration tests: a real Redis, and rows to hang games off."""

from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_module
from app.core.config import settings
from app.db.models import Player
from tests.conftest import TEST_REDIS_URL


@pytest.fixture(autouse=True)
async def redis_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Redis]:
    """The Redis every part of the app reaches for, empty, and one per test.

    Autouse because it is as much isolation as it is a fixture. The rate limiter
    counts by client address, and every test posts from the same one, so without
    a flush between tests the eleventh test to build a report would get a 429
    from the tenth. The module global is reset too: a connection pool is bound
    to the event loop that opened it, and handing one to the next test is the
    classic way this suite would start failing for no visible reason.
    """
    monkeypatch.setattr(settings, "redis_url", TEST_REDIS_URL)
    monkeypatch.setattr(redis_module, "_client", None)
    client = redis_module.get_redis()
    await client.flushdb()
    yield client
    await redis_module.close_redis()


@pytest.fixture
async def player(session: AsyncSession) -> Player:
    """A player row to hang games off. The name never reaches Lichess."""
    row = Player(username_lower="tester", display_name="Tester")
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row
