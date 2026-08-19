"""Shared test fixtures.

Integration tests run against a real Postgres and Redis -- the point of the
project is the pipeline, and a mocked database would test nothing. Set
TEST_DATABASE_URL / TEST_REDIS_URL to point elsewhere.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from app.db.base import get_session
from app.main import create_app

PROJECT_ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"

LOCAL_DATABASE_URL = "postgresql+asyncpg://chess:chess@localhost:5433/chess"
LOCAL_REDIS_URL = "redis://localhost:6380/0"


def _test_database_url() -> str:
    """Resolve the database tests run against.

    Tests truncate tables, so they must never point at a development database.
    TEST_DATABASE_URL wins outright; otherwise DATABASE_URL is redirected to a
    sibling ``_test`` database -- unless it already names one, which is how CI
    passes it in.
    """
    if explicit := os.getenv("TEST_DATABASE_URL"):
        return explicit
    url = os.getenv("DATABASE_URL", LOCAL_DATABASE_URL)
    return url if url.rsplit("/", 1)[-1].endswith("_test") else f"{url}_test"


#: The Redis database the tests get to themselves. Redis ships with sixteen of
#: them, 0 to 15, and applications take 0 by default, so the far end is the one
#: least likely to be somebody's.
TEST_REDIS_DB = 15


def _test_redis_url() -> str:
    """Resolve the Redis tests run against, on a database of its own.

    The same care as ``_test_database_url``, for a sharper reason. Tests flush
    between each other, and an arq queue is Redis keys like any other -- so a
    worker pointed at the same database will pick up a *test's* job, run it
    against whatever Postgres that worker is connected to, and leave the test's
    own burst worker draining a queue that is already empty. The report row the
    test is waiting on stays pending and the assertion fails somewhere far from
    the cause.

    That failure is silent, intermittent, and looks exactly like a bug in the
    code under test. It is not: it is a `docker compose up` in another terminal.

    TEST_REDIS_URL wins outright; otherwise REDIS_URL is redirected to
    ``TEST_REDIS_DB`` on the same server.
    """
    if explicit := os.getenv("TEST_REDIS_URL"):
        return explicit
    url = urlsplit(os.getenv("REDIS_URL", LOCAL_REDIS_URL))
    return urlunsplit(url._replace(path=f"/{TEST_REDIS_DB}"))


def _redis_target(url: str) -> tuple[str, str]:
    """Server and database, for comparing two URLs that may be spelled differently."""
    parts = urlsplit(url)
    return parts.netloc, parts.path.strip("/") or "0"


TEST_DATABASE_URL = _test_database_url()
TEST_REDIS_URL = _test_redis_url()


async def _reset_schema() -> None:
    eng = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with eng.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    finally:
        await eng.dispose()


def _migrate() -> None:
    """Rebuild the test schema by running the real migrations.

    Deliberately not ``Base.metadata.create_all``: that builds the schema from
    the models, so a migration that had drifted from them would still let the
    whole suite pass. Running Alembic means every test run exercises the same
    path production uses.

    Called via ``asyncio.to_thread`` -- alembic/env.py calls ``asyncio.run``,
    which cannot run inside an already-running event loop.
    """
    db_name = TEST_DATABASE_URL.rsplit("/", 1)[-1]
    if not db_name.endswith("_test"):
        raise RuntimeError(
            f"Refusing to drop the schema of {db_name!r}: the test database name "
            "must end in '_test'. Set TEST_DATABASE_URL."
        )

    # Only reachable by setting TEST_REDIS_URL to the application's own, which
    # `_test_redis_url` otherwise makes impossible. Worth the four lines: the
    # symptom is a handful of worker tests failing one run in ten, and nothing
    # about it points here.
    app_redis = os.getenv("REDIS_URL", LOCAL_REDIS_URL)
    if _redis_target(TEST_REDIS_URL) == _redis_target(app_redis):
        raise RuntimeError(
            f"TEST_REDIS_URL points at {TEST_REDIS_URL!r}, the same Redis database "
            f"as REDIS_URL. Tests flush it and enqueue jobs into it, so any worker "
            "running against it will steal them. Use a different database number."
        )

    asyncio.run(_reset_schema())

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator:
    await asyncio.to_thread(_migrate)
    eng = create_async_engine(TEST_DATABASE_URL)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    """A clean database per test. Truncate is cheaper than drop/create.

    ``openings_meta`` is deliberately spared: it is static reference data seeded
    by a migration, not something a test can dirty, and truncating it would make
    every test start with a classifier that knows no openings.

    ``style_reference`` is not spared, though it looks similar. It is a cache of
    a population the reports themselves describe, so a row left behind by one
    test is a band of players the next test never created.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE players, games, reports, style_reference RESTART IDENTITY CASCADE")
        )
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with maker() as s:
        yield s


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
