"""Shared test fixtures.

Integration tests run against a real Postgres and Redis -- the point of the
project is the pipeline, and a mocked database would test nothing. Set
TEST_DATABASE_URL / TEST_REDIS_URL to point elsewhere.
"""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import models  # noqa: F401  (registers tables on Base.metadata)
from app.db.base import Base, get_session
from app.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    os.getenv("DATABASE_URL", "postgresql+asyncpg://chess:chess@localhost:5433/chess") + "_test",
)
TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", os.getenv("REDIS_URL", "redis://localhost:6380/0"))


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator:
    eng = create_async_engine(TEST_DATABASE_URL)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    """A clean database per test. Truncate is cheaper than drop/create."""
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE players, games, reports, openings_meta RESTART IDENTITY CASCADE")
        )
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with maker() as s:
        yield s


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """HTTP client bound to the app, with the test session injected."""
    app = create_app()

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text()
