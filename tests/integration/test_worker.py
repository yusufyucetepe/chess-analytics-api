"""The job lifecycle end to end: a real arq worker, real Redis, real Postgres.

This is the test the brief asks for -- "enqueues a job, runs the worker inline,
and asserts the report payload shape". Everything except Lichess is genuine, so
what it proves is that a row queued by the API is the same row a worker picks
up, and that the states in between are the ones a poller would see.
"""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from arq.worker import Worker
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.db.base import get_session
from app.db.models import Player, ReportStatus
from app.lichess import client as client_module
from app.main import create_app
from app.report import service
from app.report.builder import SECTIONS
from app.worker import queue
from app.worker.jobs import build_report
from tests.conftest import TEST_REDIS_URL, load_fixture

USERNAME = "Zhigalko_Sergei"
FIXTURE_GAMES = 6


@pytest.fixture
def sessions(engine: Any) -> async_sessionmaker[AsyncSession]:
    """What the worker opens its own sessions from. The API keeps its own."""
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@pytest.fixture(autouse=True)
async def arq_redis(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Point the app's queue at the test Redis, and leave it empty afterwards.

    ``close_pool`` matters as much as the flush: the pool is a module global, so
    a connection left open here would be handed to the next test bound to the
    wrong event loop.
    """
    monkeypatch.setattr(settings, "redis_url", TEST_REDIS_URL)
    pool = await queue.get_pool()
    await pool.flushdb()
    yield
    await pool.flushdb()
    await queue.close_pool()


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the retry paths without serving the waits they ask for."""

    async def _instant(seconds: float) -> None:
        return None

    monkeypatch.setattr(client_module, "sleep", _instant)
    # One HTTP call per job attempt, so route call counts read as job attempts
    # rather than as the client's own budget.
    monkeypatch.setattr(settings, "lichess_max_retries", 0)
    # A deferred retry sits in the queue until its score comes round, and a
    # burst worker would go home first.
    monkeypatch.setattr(settings, "worker_retry_delay_s", 0)


@pytest.fixture
async def api(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The real app, with only the database session swapped for the test one."""
    app = create_app()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _session
    transport = httpx.ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def run_worker(
    sessions: async_sessionmaker[AsyncSession], max_burst_jobs: int = -1
) -> Worker:
    """Drain the queue and stop -- ``burst`` is what makes that inline.

    A burst worker waits out deferred retries rather than leaving them behind,
    so it runs a failing job to its last attempt. ``max_burst_jobs`` is how a
    test stops it partway and looks at the state in between.

    ``max_tries`` comes from the same setting the job reads, so the job always
    marks the report failed on its final attempt before arq would drop it.
    """
    worker = Worker(
        functions=[build_report],
        redis_settings=queue.redis_settings(),
        burst=True,
        poll_delay=0.01,
        max_burst_jobs=max_burst_jobs,
        max_tries=settings.worker_max_tries,
        handle_signals=False,
        ctx={"session_factory": sessions},
    )
    try:
        await worker.async_run()
    finally:
        await worker.close()
    return worker


def games_in_window() -> str:
    """The recorded export, re-dated into the report's rolling 12-month window.

    The fixture is from 2018 and the window is the last year, so the games would
    otherwise be ingested and then excluded from every count. Lichess does this
    filtering server-side via ``since``/``until``; a static fixture cannot, so
    the dates are moved instead of the assertions being loosened.
    """
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    day_ms = 86_400_000
    lines = []
    for offset, line in enumerate(load_fixture("games_sample.ndjson").splitlines()):
        if not line:
            continue
        game = json.loads(line)
        game["createdAt"] = now_ms - (offset + 1) * day_ms
        game["lastMoveAt"] = game["createdAt"] + 60_000
        lines.append(json.dumps(game))
    return "\n".join(lines) + "\n"


def mock_lichess() -> None:
    respx.get(path=f"/api/user/{USERNAME}").mock(
        return_value=httpx.Response(200, json=json.loads(load_fixture("user_profile.json")))
    )
    respx.get(path=f"/api/games/user/{USERNAME}").mock(
        return_value=httpx.Response(
            200,
            text=games_in_window(),
            headers={"content-type": "application/x-ndjson"},
        )
    )


async def request_report(api: AsyncClient) -> uuid.UUID:
    resp = await api.post("/api/v1/reports", json={"username": USERNAME})
    assert resp.status_code == 202, resp.text
    return uuid.UUID(resp.json()["id"])


# ---------------------------------------------------------------- happy path


@respx.mock
async def test_a_queued_report_is_built_end_to_end(
    api: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    mock_lichess()
    report_id = await request_report(api)

    await run_worker(sessions)

    async with sessions() as check:
        report = await service.get_report(check, report_id)
        assert report is not None
        assert report.status is ReportStatus.DONE, report.error
        assert report.error is None
        assert report.completed_at is not None
        assert report.games_total == FIXTURE_GAMES
        assert report.games_analysed == FIXTURE_GAMES - 1


@respx.mock
async def test_the_finished_payload_has_the_shape_the_frontend_will_read(
    api: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    mock_lichess()
    report_id = await request_report(api)

    await run_worker(sessions)

    async with sessions() as check:
        report = await service.get_report(check, report_id)
        assert report is not None
        payload = report.payload
    assert payload is not None
    assert payload["player"]["username"] == USERNAME
    assert payload["coverage"]["games_total"] == FIXTURE_GAMES
    assert payload["coverage"]["games_analysed"] == FIXTURE_GAMES - 1
    # Six analysed games is far under the threshold, and the report says so
    # rather than quoting an average built on them.
    assert payload["coverage"]["quality_reliable"] is False
    assert set(payload["sections"]) == set(SECTIONS)
    assert payload["sections"]["headline"]["games"] == FIXTURE_GAMES
    assert payload["sections"]["quality"]["available"] is False, "five analysed games"
    assert payload["sections"]["openings"]["tree"]["white"], "built from the real export"
    assert sum(payload["sections"]["player_type"]["scores"].values()) == 100
    assert payload["sections"]["player_type"]["confident"] is False, "six games"


@respx.mock
async def test_the_games_are_actually_in_postgres_afterwards(
    api: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The report is a summary of rows, so the rows had better be there."""
    mock_lichess()
    await request_report(api)

    await run_worker(sessions)

    async with sessions() as check:
        player = await service.player_by_username(check, USERNAME)
        assert player is not None
        assert player.last_fetched_at is not None
        start, end = service.report_window()
        total, analysed = await service.count_games(check, player.id, since=start, until=end)
        assert total == FIXTURE_GAMES
        assert analysed == FIXTURE_GAMES - 1


@respx.mock
async def test_polling_the_report_sees_it_finish(
    api: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """What the HTMX frontend will do in step 8, minus the timer."""
    mock_lichess()
    report_id = await request_report(api)

    before = await api.get(f"/api/v1/reports/{report_id}")
    assert before.json()["status"] == ReportStatus.PENDING
    assert before.json()["payload"] is None

    await run_worker(sessions)

    after = await api.get(f"/api/v1/reports/{report_id}")
    assert after.json()["status"] == ReportStatus.DONE
    assert after.json()["payload"]["coverage"]["games_total"] == FIXTURE_GAMES
    assert (await api.get(f"/api/v1/players/{USERNAME}/report")).status_code == 200


# -------------------------------------------------------------------- retries


@respx.mock
async def test_a_missing_user_fails_the_report_without_retrying(
    api: AsyncClient, sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The brief's rule: network errors get another go, a bad username does not.

    The report is created against a profile that existed at POST time, then the
    account is closed before the worker runs -- contrived, but it is the only
    way to reach this branch through the front door.
    """
    monkeypatch.setattr(settings, "worker_max_tries", 3)
    mock_lichess()
    report_id = await request_report(api)
    gone = respx.get(path=f"/api/user/{USERNAME}").mock(return_value=httpx.Response(404))
    gone.reset()

    await run_worker(sessions)

    async with sessions() as check:
        report = await service.get_report(check, report_id)
        assert report is not None
        assert report.status is ReportStatus.FAILED
        assert report.error == "No Lichess account with that username."
    assert gone.call_count == 1, "retrying a username that does not exist helps nobody"


@respx.mock
async def test_lichess_being_down_is_retried_then_given_up_on(
    api: AsyncClient, sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "worker_max_tries", 3)
    mock_lichess()
    report_id = await request_report(api)
    failing = respx.get(path=f"/api/user/{USERNAME}").mock(return_value=httpx.Response(503))
    failing.reset()

    await run_worker(sessions)

    assert failing.call_count == 3, "every attempt the budget allows"
    async with sessions() as check:
        report = await service.get_report(check, report_id)
        assert report is not None
        assert report.status is ReportStatus.FAILED
        assert report.error == "Couldn't reach Lichess. Try again shortly."


@respx.mock
async def test_a_report_with_attempts_left_stays_running(
    api: AsyncClient, sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poller must never see failed and then un-failed.

    Stopped after the first attempt, with two still to come: the row has to say
    ``running``, because that is what is true -- the job is queued again.
    """
    monkeypatch.setattr(settings, "worker_max_tries", 3)
    mock_lichess()
    report_id = await request_report(api)
    failing = respx.get(path=f"/api/user/{USERNAME}").mock(return_value=httpx.Response(503))
    failing.reset()

    await run_worker(sessions, max_burst_jobs=1)

    assert failing.call_count == 1
    async with sessions() as check:
        report = await service.get_report(check, report_id)
        assert report is not None
        assert report.status is ReportStatus.RUNNING
        assert report.error is None, "nothing to show a poller yet"


# --------------------------------------------------------------------- queue


@respx.mock
async def test_the_same_report_is_never_queued_twice(api: AsyncClient) -> None:
    """The arq job id is derived from the report id, so a repeat is a no-op."""
    mock_lichess()
    report_id = await request_report(api)

    await queue.enqueue_report(report_id)
    await queue.enqueue_report(report_id)

    pool = await queue.get_pool()
    assert await pool.zcard("arq:queue") == 1


@respx.mock
async def test_a_report_deleted_before_pickup_does_not_crash_the_worker(
    api: AsyncClient, session: AsyncSession, sessions: async_sessionmaker[AsyncSession]
) -> None:
    mock_lichess()
    report_id = await request_report(api)
    report = await service.get_report(session, report_id)
    assert report is not None
    await session.delete(report)
    await session.commit()

    await run_worker(sessions)

    async with sessions() as check:
        assert await service.get_report(check, report_id) is None


@respx.mock
async def test_a_rerun_of_the_same_report_converges(
    api: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """Replaying a job must not double the games or the counts."""
    mock_lichess()
    report_id = await request_report(api)
    await run_worker(sessions)

    pool = await queue.get_pool()
    await pool.enqueue_job("build_report", str(report_id), _job_id=f"rerun:{report_id}")
    await run_worker(sessions)

    async with sessions() as check:
        report = await service.get_report(check, report_id)
        assert report is not None
        assert report.status is ReportStatus.DONE
        assert report.games_total == FIXTURE_GAMES
        player = await check.get(Player, report.player_id)
        assert player is not None
