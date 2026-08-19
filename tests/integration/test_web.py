"""The frontend end to end: form, poll, redirect, report page.

Against the real app, so what these cover is the wiring the template tests
cannot -- which template a route picks, what HTMX gets back, and the fact that
the two front doors share one rate limit and one lock.
"""

import json
import uuid
from typing import Any

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Player, Report
from app.report import service
from app.report.builder import build_payload
from app.report.sections import build_sections
from tests.conftest import load_fixture
from tests.integration.conftest import Queue
from tests.payloads import full_payload

USERNAME = "Zhigalko_Sergei"
HTMX = {"HX-Request": "true"}


def mock_profile(username: str = USERNAME) -> None:
    respx.get(path=f"/api/user/{username}").mock(
        return_value=httpx.Response(200, json=json.loads(load_fixture("user_profile.json")))
    )


async def submit(api: AsyncClient, username: str = USERNAME, htmx: bool = True) -> httpx.Response:
    return await api.post("/reports", data={"username": username}, headers=HTMX if htmx else {})


async def finish(session: AsyncSession, report_id: uuid.UUID, payload: dict[str, Any]) -> Report:
    report = await service.get_report(session, report_id)
    assert report is not None
    await service.mark_done(session, report, payload=payload, games_total=357, games_analysed=45)
    return report


# ------------------------------------------------------------------- landing


async def test_the_landing_page_is_just_a_form(api: AsyncClient) -> None:
    """No database, no Redis, no Lichess -- it must render before anything is up."""
    resp = await api.get("/")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert 'hx-post="/reports"' in resp.text
    assert 'name="username"' in resp.text


async def test_the_vendored_assets_are_served(api: AsyncClient) -> None:
    """A CDN would put the page's fate in someone else's hands."""
    for path in ("/static/app.css", "/static/htmx.min.js", "/static/chart.umd.min.js"):
        resp = await api.get(path)
        assert resp.status_code == 200, path
        assert len(resp.content) > 100, path


# -------------------------------------------------------------------- submit


@respx.mock
async def test_submitting_starts_a_job_and_returns_the_poller(
    api: AsyncClient, session: AsyncSession, queue: Queue
) -> None:
    mock_profile()

    resp = await submit(api)

    assert resp.status_code == 200
    assert len(queue.enqueued) == 1
    assert f"/reports/{queue.enqueued[0]}/status" in resp.text
    assert "hx-trigger" in resp.text


@respx.mock
async def test_a_browser_without_htmx_gets_a_whole_page(api: AsyncClient) -> None:
    """The fragment is the page's middle; on a plain POST it needs the shell."""
    mock_profile()

    resp = await submit(api, htmx=False)

    assert "<!doctype html>" in resp.text.lower()
    assert "hx-trigger" in resp.text, "and it still starts polling once htmx loads"


@respx.mock
async def test_a_junk_username_never_reaches_lichess(api: AsyncClient, queue: Queue) -> None:
    route = respx.get(path__startswith="/api/user/").mock(return_value=httpx.Response(200))

    resp = await submit(api, "not a username")

    assert resp.status_code == 422
    # Apostrophes come back escaped, which is autoescaping doing its job.
    assert "look like a Lichess username" in resp.text
    assert not route.called
    assert queue.enqueued == []


@respx.mock
async def test_an_unknown_account_is_said_in_the_page(api: AsyncClient, queue: Queue) -> None:
    """A 404 the visitor can read, rather than a JSON body in a browser window."""
    respx.get(path="/api/user/nosuchplayer").mock(return_value=httpx.Response(404))

    resp = await submit(api, "nosuchplayer")

    assert resp.status_code == 404
    assert "No Lichess account named" in resp.text
    assert "<div" in resp.text, "an HTML fragment, not a JSON detail"
    assert queue.enqueued == []


@respx.mock
async def test_lichess_being_down_is_a_message_not_a_traceback(api: AsyncClient) -> None:
    respx.get(path=f"/api/user/{USERNAME}").mock(return_value=httpx.Response(503))

    resp = await submit(api)

    assert resp.status_code == 503
    assert "Lichess" in resp.text


@respx.mock
async def test_the_form_shares_the_api_rate_limit(
    api: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise a caller doubles their export budget by switching front doors."""
    monkeypatch.setattr(settings, "rate_limit_per_hour", 1)
    mock_profile()
    await api.post("/api/v1/reports", json={"username": USERNAME})

    resp = await submit(api)

    assert resp.status_code == 429


@respx.mock
async def test_the_form_and_the_api_share_the_ingest_lock(
    api: AsyncClient, session: AsyncSession, queue: Queue
) -> None:
    """Both doors, one export: the second request joins rather than starting another."""
    mock_profile()
    first = await api.post("/api/v1/reports", json={"username": USERNAME})

    resp = await submit(api)

    assert first.json()["id"] in resp.text
    assert len(queue.enqueued) == 1


@respx.mock
async def test_asking_again_for_a_fresh_report_goes_straight_to_it(
    api: AsyncClient, session: AsyncSession, queue: Queue
) -> None:
    """No job, no spinner: the answer already exists, so show it."""
    mock_profile()
    await submit(api)
    await finish(session, queue.enqueued[0], full_payload())

    resp = await submit(api)

    assert resp.headers["HX-Redirect"] == f"/u/{USERNAME}"
    assert len(queue.enqueued) == 1, "no second export"


@respx.mock
async def test_a_browser_without_htmx_is_redirected_the_ordinary_way(
    api: AsyncClient, session: AsyncSession, queue: Queue
) -> None:
    """HX-Redirect is a header only HTMX reads; without it, a 303 is the answer."""
    mock_profile()
    await submit(api)
    await finish(session, queue.enqueued[0], full_payload())

    resp = await submit(api, htmx=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/u/{USERNAME}"


# ---------------------------------------------------------------------- poll


@respx.mock
async def test_polling_a_running_job_returns_the_same_fragment(
    api: AsyncClient, queue: Queue
) -> None:
    mock_profile()
    await submit(api)
    report_id = queue.enqueued[0]

    resp = await api.get(f"/reports/{report_id}/status", headers=HTMX)

    assert resp.status_code == 200
    assert "hx-trigger" in resp.text, "still asking"
    assert "HX-Redirect" not in resp.headers


@respx.mock
async def test_polling_a_finished_job_sends_the_browser_to_the_report(
    api: AsyncClient, session: AsyncSession, queue: Queue
) -> None:
    """HX-Redirect rather than the report inline: the result gets its own URL."""
    mock_profile()
    await submit(api)
    await finish(session, queue.enqueued[0], full_payload())

    resp = await api.get(f"/reports/{queue.enqueued[0]}/status", headers=HTMX)

    assert resp.headers["HX-Redirect"] == f"/u/{USERNAME}"


@respx.mock
async def test_polling_a_failed_job_stops_asking(
    api: AsyncClient, session: AsyncSession, queue: Queue
) -> None:
    """The fragment it gets back has no hx-get in it, so the browser gives up."""
    mock_profile()
    await submit(api)
    report = await service.get_report(session, queue.enqueued[0])
    assert report is not None
    await service.mark_failed(session, report, "Couldn't reach Lichess. Try again shortly.")

    resp = await api.get(f"/reports/{report.id}/status", headers=HTMX)

    assert "reach Lichess. Try again shortly." in resp.text
    assert "hx-get" not in resp.text


async def test_polling_a_report_that_never_existed(api: AsyncClient) -> None:
    resp = await api.get(f"/reports/{uuid.uuid4()}/status", headers=HTMX)

    assert resp.status_code == 404
    assert "hx-get" not in resp.text, "nothing to keep asking about"


# --------------------------------------------------------------- report page


@respx.mock
async def test_the_report_page_renders_the_finished_report(
    api: AsyncClient, session: AsyncSession, queue: Queue
) -> None:
    mock_profile()
    await submit(api)
    await finish(session, queue.enqueued[0], full_payload())

    resp = await api.get(f"/u/{USERNAME}")

    assert resp.status_code == 200
    assert "Offbeat Strategist" in resp.text
    assert 'id="chart-data"' in resp.text
    assert "/static/chart.umd.min.js" in resp.text


@respx.mock
async def test_the_report_page_is_a_permalink_not_a_trigger(api: AsyncClient, queue: Queue) -> None:
    """Following a link must never cause an export -- that is an unmetered path."""
    route = respx.get(path__startswith="/api/user/").mock(return_value=httpx.Response(200))

    resp = await api.get("/u/someonewenevermet")

    assert resp.status_code == 404
    assert "No report for someonewenevermet yet" in resp.text
    assert 'name="username"' in resp.text, "and the form to build one"
    assert not route.called
    assert queue.enqueued == []


@respx.mock
async def test_a_player_with_only_an_unfinished_report_is_not_a_page_yet(
    api: AsyncClient, queue: Queue
) -> None:
    mock_profile()
    await submit(api)

    resp = await api.get(f"/u/{USERNAME}")

    assert resp.status_code == 404, "pending is not something to render"


@respx.mock
async def test_the_report_page_is_served_from_the_same_cache_as_the_api(
    api: AsyncClient, session: AsyncSession, queue: Queue
) -> None:
    """Proved by deleting the row: only Redis can still answer."""
    mock_profile()
    await submit(api)
    report = await finish(session, queue.enqueued[0], full_payload())
    await api.get(f"/u/{USERNAME}")
    await session.delete(report)
    await session.commit()

    resp = await api.get(f"/u/{USERNAME}")

    assert resp.status_code == 200
    assert "Offbeat Strategist" in resp.text


# ---------------------------------------------------------------- puzzle page


@respx.mock
async def test_the_puzzle_page_plays_the_positions_the_report_only_shows(
    api: AsyncClient, session: AsyncSession, queue: Queue
) -> None:
    mock_profile()
    await submit(api)
    await finish(session, queue.enqueued[0], full_payload())

    report = await api.get(f"/u/{USERNAME}")
    puzzles = await api.get(f"/u/{USERNAME}/puzzles")

    assert puzzles.status_code == 200
    assert "Learn from your mistakes" in puzzles.text
    assert "/static/puzzles.js" in puzzles.text
    assert "/static/puzzles.js" not in report.text, "the report links there, it does not play"
    assert f'href="/u/{USERNAME}/puzzles"' in report.text


@respx.mock
async def test_the_puzzle_page_is_a_permalink_not_a_trigger(api: AsyncClient, queue: Queue) -> None:
    """Same rule as the report page: a link is not a reason to export a year."""
    route = respx.get(path__startswith="/api/user/").mock(return_value=httpx.Response(200))

    resp = await api.get("/u/someonewenevermet/puzzles")

    assert resp.status_code == 404
    assert not route.called
    assert queue.enqueued == []


@respx.mock
async def test_the_puzzle_page_reads_the_report_rather_than_the_puzzles_table(
    api: AsyncClient, session: AsyncSession, queue: Queue
) -> None:
    """The pick was made when the report was built; this page recomputes none of it."""
    mock_profile()
    await submit(api)
    report = await finish(session, queue.enqueued[0], full_payload())
    await api.get(f"/u/{USERNAME}/puzzles")
    await session.delete(report)
    await session.commit()

    resp = await api.get(f"/u/{USERNAME}/puzzles")

    assert resp.status_code == 200
    assert "Learn from your mistakes" in resp.text


# ------------------------------------------------------------ payload shape


async def test_the_test_payloads_match_what_the_pipeline_builds(
    session: AsyncSession, player: Player
) -> None:
    """The hand-written fixtures are only useful while they stay true.

    A section that gained or lost a key would leave the template tests passing
    against a shape nothing produces, which is the failure mode that makes
    fixtures worse than useless.
    """
    since, until = service.report_window()
    sections = await build_sections(session, player.id, since=since, until=until, games_analysed=0)
    real = build_payload(
        player=player,
        period_start=since,
        period_end=until,
        games_total=0,
        games_analysed=0,
        sections=sections,
    )
    mine = full_payload()

    assert set(real) == set(mine)
    assert set(real["coverage"]) == set(mine["coverage"])
    assert set(real["player"]) == set(mine["player"])
    assert set(real["sections"]) == set(mine["sections"])
    for name, section in real["sections"].items():
        assert set(section) == set(mine["sections"][name]), name
