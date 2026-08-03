"""Lichess client tests.

Every response here is either a recorded fixture or a hand-built failure. The
suite never touches the network -- CI must not depend on lichess.org being up,
and Lichess should not be paying for our test runs.
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import httpx
import pytest
import respx

from app.core.config import settings
from app.lichess import (
    LichessClient,
    LichessResponseError,
    LichessUnavailableError,
    RateLimitedError,
    UserNotFoundError,
)
from app.lichess import client as client_module
from app.lichess.client import BACKOFF_BASE_S
from tests.conftest import load_fixture

USERNAME = "Zhigalko_Sergei"
GAMES_PATH = f"/api/games/user/{USERNAME}"
PROFILE_PATH = f"/api/user/{USERNAME}"
HISTORY_PATH = f"/api/user/{USERNAME}/rating-history"

SINCE = datetime(2018, 1, 1, tzinfo=UTC)
UNTIL = datetime(2019, 1, 1, tzinfo=UTC)


@pytest.fixture
def games_ndjson() -> str:
    return load_fixture("games_sample.ndjson")


@pytest.fixture
def game_ids(games_ndjson: str) -> list[str]:
    return [json.loads(line)["id"] for line in games_ndjson.splitlines() if line]


@pytest.fixture
def delays(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record backoff sleeps instead of serving them."""
    recorded: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(client_module, "sleep", _fake_sleep)
    return recorded


class TruncatedStream(httpx.AsyncByteStream):
    """A body that stops early, the way a dropped connection does."""

    def __init__(self, payload: str, *, then_raise: Exception | None = None) -> None:
        self._payload = payload.encode()
        self._then_raise = then_raise

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._payload
        if self._then_raise is not None:
            raise self._then_raise


def ndjson_response(payload: str, **kwargs: object) -> httpx.Response:
    return httpx.Response(
        200,
        stream=TruncatedStream(payload, **kwargs),  # type: ignore[arg-type]
        headers={"content-type": "application/x-ndjson"},
    )


async def collect(client: LichessClient) -> list[dict]:
    return [g async for g in client.stream_games(USERNAME, since=SINCE, until=UNTIL)]


# ------------------------------------------------------------------- streaming


@respx.mock
async def test_stream_games_yields_every_ndjson_line(
    games_ndjson: str, game_ids: list[str]
) -> None:
    respx.get(path=GAMES_PATH).mock(return_value=ndjson_response(games_ndjson))

    async with LichessClient() as client:
        games = await collect(client)

    assert [g["id"] for g in games] == game_ids
    # The fixture deliberately ends on an unanalysed game: the quality section
    # has to cope with partial coverage, so the client must not filter it out.
    assert games[-1]["players"]["white"].get("analysis") is None


@respx.mock
async def test_stream_games_sends_the_documented_query(games_ndjson: str) -> None:
    route = respx.get(path=GAMES_PATH).mock(return_value=ndjson_response(games_ndjson))

    async with LichessClient() as client:
        await collect(client)

    request = route.calls.last.request
    params = request.url.params
    assert params["sort"] == "dateAsc"
    assert params["rated"] == "true"
    assert params["opening"] == "true"
    assert params["accuracy"] == "true"
    assert params["moves"] == "true"
    assert params["clocks"] == "false"
    # Kept on for the planned puzzle feature, which needs per-move eval swings.
    assert params["evals"] == "true"
    assert int(params["since"]) == int(SINCE.timestamp() * 1000)
    assert int(params["until"]) == int(UNTIL.timestamp() * 1000)
    assert request.headers["accept"] == "application/x-ndjson"


@respx.mock
async def test_stream_resumes_from_the_last_game_after_a_dropped_connection(
    games_ndjson: str, game_ids: list[str], delays: list[float]
) -> None:
    lines = games_ndjson.splitlines()
    # Cut mid-export, then resume. The second response repeats the boundary game
    # because `since` is inclusive -- exactly what the real API does.
    first = "\n".join(lines[:3]) + "\n"
    second = "\n".join(lines[2:]) + "\n"

    route = respx.get(path=GAMES_PATH).mock(
        side_effect=[
            ndjson_response(first, then_raise=httpx.ReadError("connection reset")),
            ndjson_response(second),
        ]
    )

    async with LichessClient() as client:
        games = await collect(client)

    assert [g["id"] for g in games] == game_ids, "resume must not drop or duplicate games"
    assert route.call_count == 2

    resumed_since = int(route.calls[1].request.url.params["since"])
    assert resumed_since == json.loads(lines[2])["createdAt"], (
        "resume should start from the last game seen, not the window start"
    )
    assert delays, "a resumed stream still backs off before retrying"


@respx.mock
async def test_a_body_that_stops_mid_game_is_treated_as_a_dropped_connection(
    games_ndjson: str, game_ids: list[str], delays: list[float]
) -> None:
    """A truncated line is a transport problem wearing a JSON error's clothes.

    The caller catches ``LichessError``; letting a raw ``JSONDecodeError`` out
    would skip the retry entirely and surface as an unhandled crash.
    """
    lines = games_ndjson.splitlines()
    half_written = "\n".join(lines[:2]) + "\n" + lines[2][:400]

    route = respx.get(path=GAMES_PATH).mock(
        side_effect=[
            ndjson_response(half_written),
            ndjson_response("\n".join(lines[2:]) + "\n"),
        ]
    )

    async with LichessClient() as client:
        games = await collect(client)

    assert [g["id"] for g in games] == game_ids
    assert route.call_count == 2
    assert int(route.calls[1].request.url.params["since"]) == json.loads(lines[1])["createdAt"]


@respx.mock
async def test_a_stream_that_keeps_failing_without_progress_gives_up(
    delays: list[float],
) -> None:
    route = respx.get(path=GAMES_PATH).mock(side_effect=httpx.ConnectError("no route"))

    async with LichessClient() as client:
        with pytest.raises(LichessUnavailableError):
            await collect(client)

    assert route.call_count == settings.lichess_max_retries + 1


# --------------------------------------------------------------------- retries


@respx.mock
async def test_429_is_retried_after_the_server_stated_delay(delays: list[float]) -> None:
    route = respx.get(path=PROFILE_PATH).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json=json.loads(load_fixture("user_profile.json"))),
        ]
    )

    async with LichessClient() as client:
        profile = await client.get_profile(USERNAME)

    assert profile.username == USERNAME
    assert route.call_count == 2
    assert delays == [7.0], "Retry-After must win over our own backoff curve"


@respx.mock
async def test_429_without_retry_after_waits_a_full_minute(delays: list[float]) -> None:
    respx.get(path=PROFILE_PATH).mock(return_value=httpx.Response(429))

    async with LichessClient() as client:
        with pytest.raises(RateLimitedError):
            await client.get_profile(USERNAME)

    assert delays == [client_module.RATE_LIMIT_COOLDOWN_S] * settings.lichess_max_retries


@respx.mock
async def test_server_errors_back_off_exponentially(delays: list[float]) -> None:
    respx.get(path=PROFILE_PATH).mock(return_value=httpx.Response(503))

    async with LichessClient() as client:
        with pytest.raises(LichessUnavailableError):
            await client.get_profile(USERNAME)

    assert len(delays) == settings.lichess_max_retries
    assert delays == sorted(delays), "each wait should be longer than the last"
    assert delays[0] < delays[-1]


@respx.mock
async def test_unknown_user_is_final(delays: list[float]) -> None:
    route = respx.get(path=PROFILE_PATH).mock(return_value=httpx.Response(404))

    async with LichessClient() as client:
        with pytest.raises(UserNotFoundError, match=USERNAME):
            await client.get_profile(USERNAME)

    assert route.call_count == 1, "a missing user will not appear if we ask again"
    assert delays == []


@respx.mock
async def test_an_unexpected_client_error_is_not_retried(delays: list[float]) -> None:
    """A 403 means we asked wrongly; asking again the same way won't help."""
    route = respx.get(path=PROFILE_PATH).mock(return_value=httpx.Response(403))

    async with LichessClient() as client:
        with pytest.raises(LichessResponseError) as caught:
            await client.get_profile(USERNAME)

    assert caught.value.status_code == 403
    assert route.call_count == 1
    assert delays == []


@respx.mock
async def test_a_retry_after_http_date_falls_back_to_backoff(delays: list[float]) -> None:
    """Retry-After may legally be a date. We don't parse it -- we back off instead."""
    respx.get(path=PROFILE_PATH).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}),
            httpx.Response(200, json=json.loads(load_fixture("user_profile.json"))),
        ]
    )

    async with LichessClient() as client:
        await client.get_profile(USERNAME)

    assert len(delays) == 1
    assert BACKOFF_BASE_S <= delays[0] < BACKOFF_BASE_S + 1, "an unparseable header must not stall"


# ---------------------------------------------------------------------- parsing


@respx.mock
async def test_profile_exposes_ratings_and_drops_unplayed_perfs() -> None:
    payload = json.loads(load_fixture("user_profile.json"))
    respx.get(path=PROFILE_PATH).mock(return_value=httpx.Response(200, json=payload))

    async with LichessClient() as client:
        profile = await client.get_profile(USERNAME)

    assert profile.id == USERNAME.lower()
    assert profile.title == "GM"
    ratings = profile.ratings()
    assert ratings["bullet"] == payload["perfs"]["bullet"]["rating"]

    unplayed = {name for name, p in payload["perfs"].items() if p.get("games") == 0}
    assert unplayed, "fixture should contain a perf the player never played"
    assert unplayed.isdisjoint(ratings), (
        "perfs with no games report a provisional 1500/2500 that is not a real rating"
    )

    # `perfs` is not just time controls: the arcade modes sit in there too,
    # shaped as {runs, score} with no rating at all.
    assert {"storm", "racer", "streak"} <= set(payload["perfs"])
    assert {"storm", "racer", "streak"}.isdisjoint(ratings)


@respx.mock
async def test_rating_history_months_are_zero_indexed() -> None:
    payload = json.loads(load_fixture("rating_history.json"))
    respx.get(path=HISTORY_PATH).mock(return_value=httpx.Response(200, json=payload))

    async with LichessClient() as client:
        history = await client.get_rating_history(USERNAME)

    bullet = next(perf for perf in history if perf.name == "Bullet")
    year, month, day, rating = payload[0]["points"][0]
    assert bullet.points[0].day == date(year, month + 1, day)
    assert bullet.points[0].rating == rating
    # The player's first bullet game is dated 2018-09-20; a 1-indexed reading
    # would put the curve's first point a month before he ever played.
    assert bullet.points[0].day == date(2018, 9, 20)


# ---------------------------------------------------------------------- headers


@respx.mock
async def test_requests_identify_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "lichess_token", None)
    route = respx.get(path=PROFILE_PATH).mock(
        return_value=httpx.Response(200, json=json.loads(load_fixture("user_profile.json")))
    )

    async with LichessClient() as client:
        await client.get_profile(USERNAME)

    headers = route.calls.last.request.headers
    assert headers["user-agent"] == settings.lichess_user_agent
    assert "authorization" not in headers


@respx.mock
async def test_a_configured_token_is_sent_as_a_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "lichess_token", "secret-token")
    route = respx.get(path=PROFILE_PATH).mock(
        return_value=httpx.Response(200, json=json.loads(load_fixture("user_profile.json")))
    )

    async with LichessClient() as client:
        await client.get_profile(USERNAME)

    assert route.calls.last.request.headers["authorization"] == "Bearer secret-token"
