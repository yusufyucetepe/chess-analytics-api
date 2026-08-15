"""Fixtures shared by the integration tests.

A real Redis, rows to hang games off, and an HTTP client bound to the app. The
client lives here rather than in one test module because fixtures do not travel
by import -- both the JSON API and the frontend need the same wiring, and the
copy that got made instead would drift.
"""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_enqueue
from app.core import redis as redis_module
from app.core.config import settings
from app.db.base import get_session
from app.db.models import Player
from app.main import create_app
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


class Queue:
    """Stands in for arq. Records what a route asked to be built."""

    def __init__(self) -> None:
        self.enqueued: list[uuid.UUID] = []

    async def __call__(self, report_id: uuid.UUID) -> None:
        self.enqueued.append(report_id)


@pytest.fixture
def queue() -> Queue:
    return Queue()


@pytest.fixture
async def api(session: AsyncSession, queue: Queue) -> AsyncIterator[AsyncClient]:
    """The app with the test session and a recording queue wired in.

    Redis is not overridden: the autouse fixture above already points the app's
    client at the test instance, and the lock and the limiter are things these
    tests want to exercise rather than fake.
    """
    app = create_app()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_enqueue] = lambda: queue
    transport = httpx.ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()
