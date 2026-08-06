"""The reports endpoints against a real Postgres.

The queue is stubbed out here: what these tests are about is which rows get
created and which status code comes back, and a real Redis would only make the
same assertions slower. ``test_worker.py`` covers the job actually running.
"""

import json
import uuid
from typing import Any

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_enqueue
from app.db.models import Player, Report, ReportStatus
from app.main import create_app
from app.report import service
from tests.conftest import load_fixture

USERNAME = "Zhigalko_Sergei"
USERNAME_LOWER = USERNAME.lower()


class Queue:
    """Stands in for arq. Records what the route asked to be built."""

    def __init__(self) -> None:
        self.enqueued: list[uuid.UUID] = []

    async def __call__(self, report_id: uuid.UUID) -> None:
        self.enqueued.append(report_id)


@pytest.fixture
def queue() -> Queue:
    return Queue()


@pytest.fixture
async def api(session: AsyncSession, queue: Queue):
    """The app with the test session and a recording queue wired in."""
    from app.db.base import get_session

    app = create_app()

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_enqueue] = lambda: queue
    transport = httpx.ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


def mock_profile(username: str = USERNAME) -> None:
    respx.get(path=f"/api/user/{username}").mock(
        return_value=httpx.Response(200, json=json.loads(load_fixture("user_profile.json")))
    )


async def post(api: AsyncClient, username: str = USERNAME) -> httpx.Response:
    return await api.post("/api/v1/reports", json={"username": username})


async def count(session: AsyncSession, model: Any) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


# ------------------------------------------------------------------ POST


@respx.mock
async def test_a_new_username_is_accepted_and_queued(
    api: AsyncClient, session: AsyncSession, queue: Queue
) -> None:
    mock_profile()

    resp = await post(api)

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == ReportStatus.PENDING
    assert body["username"] == USERNAME
    assert body["payload"] is None
    assert queue.enqueued == [uuid.UUID(body["id"])]


@respx.mock
async def test_the_player_row_is_created_by_the_request(
    api: AsyncClient, session: AsyncSession
) -> None:
    """The profile call is needed for validation anyway, so the row is free."""
    mock_profile()

    await post(api)

    player = await service.player_by_username(session, USERNAME)
    assert player is not None
    assert player.title == "GM"
    assert player.last_fetched_at is None, "no games have been fetched yet"


@respx.mock
async def test_an_unknown_username_is_a_404_and_creates_nothing(
    api: AsyncClient, session: AsyncSession, queue: Queue
) -> None:
    """It has to fail here, not minutes later as a mysteriously failed report."""
    respx.get(path="/api/user/nosuchplayer").mock(return_value=httpx.Response(404))

    resp = await post(api, "nosuchplayer")

    assert resp.status_code == 404
    assert await count(session, Report) == 0
    assert await count(session, Player) == 0
    assert queue.enqueued == []


@pytest.mark.parametrize("username", ["", "x", "1abc", "has spaces", "drop;table", "a" * 31])
@respx.mock
async def test_junk_usernames_never_reach_lichess(
    api: AsyncClient, queue: Queue, username: str
) -> None:
    """A public form must not become a probe against Lichess's user endpoint."""
    route = respx.get(path__startswith="/api/user/").mock(return_value=httpx.Response(200))

    resp = await post(api, username)

    assert resp.status_code == 422
    assert not route.called
    assert queue.enqueued == []


@respx.mock
async def test_lichess_being_down_is_a_503_not_a_queued_job(
    api: AsyncClient, session: AsyncSession, queue: Queue
) -> None:
    respx.get(path=f"/api/user/{USERNAME}").mock(return_value=httpx.Response(503))

    resp = await post(api)

    assert resp.status_code == 503
    assert "Lichess" in resp.json()["detail"]
    assert await count(session, Report) == 0
    assert queue.enqueued == []


@respx.mock
async def test_a_second_request_joins_the_job_already_running(
    api: AsyncClient, session: AsyncSession, queue: Queue
) -> None:
    """Refreshing the page must not start a second export of the same year."""
    mock_profile()

    first = await post(api)
    second = await post(api)

    assert second.status_code == 202
    assert second.json()["id"] == first.json()["id"]
    assert await count(session, Report) == 1
    assert len(queue.enqueued) == 1


@respx.mock
async def test_a_recent_finished_report_comes_straight_back(
    api: AsyncClient, session: AsyncSession, queue: Queue
) -> None:
    """200 rather than 202: there is no job, the answer is already here."""
    mock_profile()
    first = await post(api)
    report = await service.get_report(session, uuid.UUID(first.json()["id"]))
    assert report is not None
    await service.mark_done(
        session, report, payload={"hello": "world"}, games_total=9, games_analysed=3
    )

    second = await post(api)

    assert second.status_code == 200
    assert second.json()["id"] == str(report.id)
    assert second.json()["payload"] == {"hello": "world"}
    assert len(queue.enqueued) == 1, "no second job"


@respx.mock
async def test_a_failed_report_does_not_block_a_retry(
    api: AsyncClient, session: AsyncSession, queue: Queue
) -> None:
    """Failed is a finished state: the next request gets a fresh job."""
    mock_profile()
    first = await post(api)
    report = await service.get_report(session, uuid.UUID(first.json()["id"]))
    assert report is not None
    await service.mark_failed(session, report, "Couldn't reach Lichess. Try again shortly.")

    second = await post(api)

    assert second.status_code == 202
    assert second.json()["id"] != first.json()["id"]
    assert len(queue.enqueued) == 2


@respx.mock
async def test_the_username_is_matched_case_insensitively(
    api: AsyncClient, session: AsyncSession
) -> None:
    mock_profile()
    mock_profile("ZHIGALKO_SERGEI")

    await post(api)
    await post(api, "ZHIGALKO_SERGEI")

    assert await count(session, Player) == 1, "Lichess ids are case-insensitive"


# ------------------------------------------------------------------- GET


@respx.mock
async def test_polling_a_report_reports_its_progress(
    api: AsyncClient, session: AsyncSession
) -> None:
    mock_profile()
    report_id = (await post(api)).json()["id"]

    resp = await api.get(f"/api/v1/reports/{report_id}")

    assert resp.status_code == 200
    assert resp.json()["status"] == ReportStatus.PENDING
    assert resp.json()["username"] == USERNAME


async def test_an_unknown_report_id_is_a_404(api: AsyncClient) -> None:
    assert (await api.get(f"/api/v1/reports/{uuid.uuid4()}")).status_code == 404


async def test_a_malformed_report_id_is_a_422(api: AsyncClient) -> None:
    assert (await api.get("/api/v1/reports/not-a-uuid")).status_code == 422


@respx.mock
async def test_the_latest_report_endpoint_serves_the_finished_one(
    api: AsyncClient, session: AsyncSession
) -> None:
    mock_profile()
    report_id = uuid.UUID((await post(api)).json()["id"])
    report = await service.get_report(session, report_id)
    assert report is not None
    await service.mark_done(
        session, report, payload={"done": True}, games_total=5, games_analysed=1
    )

    resp = await api.get(f"/api/v1/players/{USERNAME}/report")

    assert resp.status_code == 200
    assert resp.json()["payload"] == {"done": True}
    assert resp.json()["games_total"] == 5


@respx.mock
async def test_the_latest_report_endpoint_ignores_unfinished_jobs(
    api: AsyncClient, session: AsyncSession
) -> None:
    mock_profile()
    await post(api)

    resp = await api.get(f"/api/v1/players/{USERNAME}/report")

    assert resp.status_code == 404, "pending is not something to serve"


@respx.mock
async def test_reading_a_player_we_have_never_seen_starts_no_work(
    api: AsyncClient, session: AsyncSession, queue: Queue
) -> None:
    """A GET that could trigger an export would be an unmetered scraping path."""
    route = respx.get(path__startswith="/api/user/").mock(return_value=httpx.Response(200))

    resp = await api.get("/api/v1/players/someoneelse/report")

    assert resp.status_code == 404
    assert not route.called
    assert queue.enqueued == []
