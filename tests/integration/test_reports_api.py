"""The reports endpoints against a real Postgres and a real Redis.

Only the queue is stubbed: what these tests are about is which rows get created,
which key gets written and which status code comes back, and arq would add a
round trip to every one of them without changing an assertion. ``test_worker.py``
covers the job actually running.
"""

import datetime
import json
import uuid
from typing import Any

import httpx
import pytest
import respx
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_enqueue
from app.core import locks
from app.core.config import settings
from app.core.redis import get_redis
from app.db.models import Color, Game, Player, Report, ReportStatus, Result
from app.main import create_app
from app.report import cache, service
from tests.conftest import load_fixture
from tests.integration.test_redis_layer import broken

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


def lock_key() -> str:
    return locks.report_lock_key(USERNAME_LOWER)


async def finish(session: AsyncSession, report_id: str) -> Report:
    """Mark a report done the way the worker would, minus the work."""
    report = await service.get_report(session, uuid.UUID(report_id))
    assert report is not None
    await service.mark_done(
        session, report, payload={"hello": "world"}, games_total=9, games_analysed=3
    )
    return report


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


@respx.mock
async def test_the_openings_endpoint_computes_from_the_games_we_hold(
    api: AsyncClient, session: AsyncSession
) -> None:
    """Not lifted out of a report payload: it works before one has ever been built."""
    mock_profile()
    await post(api)
    player = await service.player_by_username(session, USERNAME)
    assert player is not None
    since, _ = service.report_window()
    session.add_all(
        Game(
            game_id=f"opening{index}",
            player_id=player.id,
            played_at=since + datetime.timedelta(days=1),
            perf="blitz",
            color=Color.WHITE,
            result=Result.WIN,
            status="resign",
            eco="B22",
            opening_name="Sicilian Defense: Alapin Variation",
            opening_line=["e4", "c5", "c3"],
        )
        for index in range(3)
    )
    await session.commit()

    resp = await api.get(f"/api/v1/players/{USERNAME}/openings")

    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == USERNAME
    assert body["openings"]["by_color"]["white"]["top"][0]["eco"] == "B22"
    assert [node["san"] for node in body["openings"]["tree"]["white"]] == ["e4"]


@respx.mock
async def test_the_openings_endpoint_404s_for_a_player_we_have_never_seen(
    api: AsyncClient,
) -> None:
    resp = await api.get("/api/v1/players/someoneelse/openings")

    assert resp.status_code == 404


# ------------------------------------------------------------- ingest lock


@respx.mock
async def test_queueing_a_report_takes_the_lock_for_that_player(
    api: AsyncClient, redis_client: Redis
) -> None:
    """The lock names the report, so the loser of the race knows what to poll."""
    mock_profile()

    report_id = (await post(api)).json()["id"]

    assert await redis_client.get(lock_key()) == report_id


@respx.mock
async def test_the_lock_is_what_makes_a_second_request_join_the_first(
    api: AsyncClient, session: AsyncSession, queue: Queue, redis_client: Redis
) -> None:
    """Held by hand, with no row of our own: the route has only the lock to go on.

    This is the case the old database check could not cover -- two requests that
    both looked before either wrote.
    """
    mock_profile()
    first = await post(api)
    await redis_client.set(lock_key(), first.json()["id"])
    queue.enqueued.clear()

    second = await post(api)

    assert second.json()["id"] == first.json()["id"]
    assert queue.enqueued == [], "the job was already queued by the first request"


@respx.mock
async def test_a_lock_left_behind_by_a_deleted_report_is_taken_over(
    api: AsyncClient, session: AsyncSession, redis_client: Redis
) -> None:
    """Otherwise the player waits out a half-hour TTL for a job nobody is running."""
    mock_profile()
    first = await post(api)
    report = await service.get_report(session, uuid.UUID(first.json()["id"]))
    assert report is not None
    await session.delete(report)
    await session.commit()

    second = await post(api)

    assert second.status_code == 202
    assert second.json()["id"] != first.json()["id"]
    assert await redis_client.get(lock_key()) == second.json()["id"]


@respx.mock
async def test_a_lock_holding_junk_does_not_wedge_the_endpoint(
    api: AsyncClient, redis_client: Redis
) -> None:
    """Nothing else writes this key, but a 500 would be a poor way to find out."""
    mock_profile()
    await redis_client.set(lock_key(), "not-a-uuid")

    resp = await post(api)

    assert resp.status_code == 202
    assert await redis_client.get(lock_key()) == resp.json()["id"]


# ------------------------------------------------------------- rate limiting


@respx.mock
async def test_the_request_past_the_hourly_limit_is_refused(
    api: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "rate_limit_per_hour", 1)
    mock_profile()
    assert (await post(api)).status_code == 202

    resp = await post(api)

    assert resp.status_code == 429
    assert resp.headers["retry-after"].isdigit()
    assert await count(session, Report) == 1


@respx.mock
async def test_a_refused_request_never_reaches_lichess(
    api: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: the limit has to sit in front of the export it would cause."""
    monkeypatch.setattr(settings, "rate_limit_per_hour", 1)
    route = respx.get(path__startswith="/api/user/").mock(
        return_value=httpx.Response(200, json=json.loads(load_fixture("user_profile.json")))
    )
    await post(api)

    await post(api)

    assert route.call_count == 1


@respx.mock
async def test_every_answer_says_how_much_budget_is_left(api: AsyncClient) -> None:
    mock_profile()

    resp = await post(api)

    assert resp.headers["x-ratelimit-limit"] == str(settings.rate_limit_per_hour)
    assert resp.headers["x-ratelimit-remaining"] == str(settings.rate_limit_per_hour - 1)


@respx.mock
async def test_the_limit_can_be_switched_off(
    api: AsyncClient, monkeypatch: pytest.MonkeyPatch, redis_client: Redis
) -> None:
    """Zero means unmetered -- for a private deployment that is nobody's proxy."""
    monkeypatch.setattr(settings, "rate_limit_per_hour", 0)
    mock_profile()

    for _ in range(3):
        assert (await post(api)).status_code == 202

    assert await redis_client.keys("ratelimit:*") == [], "not even counted"


@respx.mock
async def test_reading_a_report_is_never_rate_limited(
    api: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the POST can cause an export; polling one is what the frontend does."""
    monkeypatch.setattr(settings, "rate_limit_per_hour", 1)
    mock_profile()
    report_id = (await post(api)).json()["id"]

    for _ in range(5):
        assert (await api.get(f"/api/v1/reports/{report_id}")).status_code == 200


# -------------------------------------------------------------- report cache


@respx.mock
async def test_a_finished_report_is_cached_on_the_way_out(
    api: AsyncClient, session: AsyncSession, redis_client: Redis
) -> None:
    mock_profile()
    await finish(session, (await post(api)).json()["id"])

    resp = await api.get(f"/api/v1/players/{USERNAME}/report")

    assert resp.status_code == 200
    assert (await cache.load(redis_client, USERNAME_LOWER))["payload"] == {"hello": "world"}


@respx.mock
async def test_the_second_read_comes_out_of_redis(api: AsyncClient, session: AsyncSession) -> None:
    """Proved by deleting the row: only the cache can still answer."""
    mock_profile()
    report = await finish(session, (await post(api)).json()["id"])
    await api.get(f"/api/v1/players/{USERNAME}/report")
    await session.delete(report)
    await session.commit()

    resp = await api.get(f"/api/v1/players/{USERNAME}/report")

    assert resp.status_code == 200
    assert resp.json()["payload"] == {"hello": "world"}


@respx.mock
async def test_a_cached_report_answers_the_next_post_without_a_new_job(
    api: AsyncClient, session: AsyncSession, queue: Queue
) -> None:
    """200 rather than 202, and the freshness rule read off the cached view."""
    mock_profile()
    await finish(session, (await post(api)).json()["id"])
    await api.get(f"/api/v1/players/{USERNAME}/report")

    resp = await post(api)

    assert resp.status_code == 200
    assert len(queue.enqueued) == 1, "no second export"


@respx.mock
async def test_a_cached_report_that_has_aged_out_does_not_block_a_new_one(
    api: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entry can outlive the 24h freshness rule, so the rule is re-checked."""
    mock_profile()
    await finish(session, (await post(api)).json()["id"])
    await api.get(f"/api/v1/players/{USERNAME}/report")
    monkeypatch.setattr(settings, "report_fresh_ttl_s", 0)

    resp = await post(api)

    assert resp.status_code == 202, "a fresh job, off a cache hit that was too old"


@respx.mock
async def test_a_player_with_no_finished_report_caches_nothing(
    api: AsyncClient, redis_client: Redis
) -> None:
    """A 404 must not be remembered -- the report is minutes away."""
    mock_profile()
    await post(api)

    assert (await api.get(f"/api/v1/players/{USERNAME}/report")).status_code == 404
    assert await cache.load(redis_client, USERNAME_LOWER) is None


# --------------------------------------------------------------- redis down


@pytest.fixture
async def offline(session: AsyncSession, queue: Queue):
    """The app with a Redis that refuses every command."""
    from app.db.base import get_session

    app = create_app()

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_enqueue] = lambda: queue
    app.dependency_overrides[get_redis] = broken
    transport = httpx.ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


@respx.mock
async def test_a_dead_redis_refuses_new_reports_rather_than_running_unmetered(
    offline: AsyncClient, session: AsyncSession, queue: Queue
) -> None:
    mock_profile()

    resp = await post(offline)

    assert resp.status_code == 503
    assert queue.enqueued == []
    assert await count(session, Report) == 0


@respx.mock
async def test_a_dead_redis_refuses_at_the_lock_as_well_as_at_the_limiter(
    offline: AsyncClient, session: AsyncSession, queue: Queue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the limiter switched off, the lock is the next thing that needs Redis.

    It has to refuse rather than carry on: an export with no lock behind it is
    exactly the duplicate this step exists to prevent.
    """
    monkeypatch.setattr(settings, "rate_limit_per_hour", 0)
    mock_profile()

    resp = await post(offline)

    assert resp.status_code == 503
    assert queue.enqueued == []
    assert await count(session, Report) == 0


@respx.mock
async def test_a_dead_redis_refuses_at_the_limiter_before_touching_lichess(
    offline: AsyncClient,
) -> None:
    route = respx.get(path__startswith="/api/user/").mock(return_value=httpx.Response(200))

    assert (await post(offline)).status_code == 503
    assert not route.called


@respx.mock
async def test_a_dead_redis_still_serves_a_report_we_already_have(
    offline: AsyncClient, api: AsyncClient, session: AsyncSession
) -> None:
    """Reads fall through to Postgres: the cache is an optimisation, not the source."""
    mock_profile()
    await finish(session, (await post(api)).json()["id"])

    resp = await offline.get(f"/api/v1/players/{USERNAME}/report")

    assert resp.status_code == 200
    assert resp.json()["payload"] == {"hello": "world"}
