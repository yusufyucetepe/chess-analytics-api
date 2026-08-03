"""Async client for the Lichess public API.

Two shapes of call live here. The profile and rating-history endpoints are
ordinary small JSON GETs. The game export is a long-lived NDJSON stream -- a
year of blitz is tens of thousands of games, so it is consumed line by line and
never buffered.

Retries cover 429 and transient 5xx/network faults. A 404 is final: the username
does not exist, and no amount of waiting will change that.
"""

import asyncio
import json
import logging
import random
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from datetime import datetime
from types import TracebackType
from typing import Any, Self

import httpx

from app.core.config import settings
from app.lichess.errors import (
    LichessError,
    LichessResponseError,
    LichessUnavailableError,
    RateLimitedError,
    UserNotFoundError,
)
from app.lichess.schemas import PerfRatingHistory, PlayerProfile

logger = logging.getLogger(__name__)

#: Statuses worth a second attempt. 404 is deliberately absent.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 60.0
#: Lichess asks for a full minute after a 429 that carries no Retry-After.
RATE_LIMIT_COOLDOWN_S = 60.0


async def sleep(seconds: float) -> None:
    """Indirection so tests can exercise the backoff path without waiting."""
    await asyncio.sleep(seconds)


def to_epoch_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


class RetryableStatusError(Exception):
    """Internal signal: this response deserves another attempt."""

    def __init__(self, status_code: int, retry_after: str | None = None) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        if status_code == 429 and retry_after is None:
            self.retry_after = str(RATE_LIMIT_COOLDOWN_S)
        super().__init__(f"HTTP {status_code}")


def _error_for(exc: Exception, context: str) -> LichessError:
    """The error to surface once retries are exhausted."""
    if isinstance(exc, RetryableStatusError) and exc.status_code == 429:
        return RateLimitedError(f"Rate limited by Lichess while {context}")
    return LichessUnavailableError(f"Lichess unreachable while {context}: {exc}")


class LichessClient:
    """A thin, retrying wrapper over the endpoints the report pipeline needs.

    Usable as an async context manager. Pass an existing ``httpx.AsyncClient``
    to share a connection pool; the client then belongs to the caller and is not
    closed here.
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.lichess_base_url,
            headers=self._default_headers(),
            timeout=httpx.Timeout(settings.lichess_timeout_s),
            follow_redirects=True,
        )

    @staticmethod
    def _default_headers() -> dict[str, str]:
        # Lichess asks every client to identify itself with a contact URL, and
        # throttles harder when it can't tell who is calling.
        headers = {"User-Agent": settings.lichess_user_agent}
        if settings.lichess_token:
            headers["Authorization"] = f"Bearer {settings.lichess_token}"
        return headers

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ public

    async def get_profile(self, username: str) -> PlayerProfile:
        """Profile, current ratings, and implicitly an existence check."""
        payload = await self._get_json(f"/api/user/{username}", username=username)
        return PlayerProfile.model_validate(payload)

    async def get_rating_history(self, username: str) -> list[PerfRatingHistory]:
        """Rating progression per perf, one curve each, oldest point first."""
        payload = await self._get_json(f"/api/user/{username}/rating-history", username=username)
        return [PerfRatingHistory.model_validate(perf) for perf in payload]

    async def stream_games(
        self,
        username: str,
        *,
        since: datetime,
        until: datetime,
        rated: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield the player's games in the window, oldest first.

        Games arrive as raw decoded JSON -- mapping them to rows is the ingest
        layer's job. A dropped connection is resumed from the last game seen
        rather than restarting the export, which for a year of bullet is the
        difference between a hiccup and half an hour of refetching.
        """
        seen: set[str] = set()
        cursor_ms = to_epoch_ms(since)
        until_ms = to_epoch_ms(until)
        attempt = 0

        while True:
            progressed = False
            try:
                stream = self._stream_games_once(username, cursor_ms, until_ms, rated)
                async with aclosing(stream):
                    async for game in stream:
                        game_id = game.get("id")
                        # Resuming re-sends everything at the cursor timestamp,
                        # and two games can share a millisecond.
                        if isinstance(game_id, str):
                            if game_id in seen:
                                continue
                            seen.add(game_id)
                        created_at = game.get("createdAt")
                        if isinstance(created_at, int):
                            cursor_ms = created_at
                        progressed = True
                        yield game
                return
            except (httpx.TransportError, RetryableStatusError) as exc:
                # A retry that delivered new games earns a fresh budget. This
                # still terminates: every reset consumed at least one game from
                # a finite export.
                if progressed:
                    attempt = 0
                await self._wait_before_retry(
                    attempt,
                    error=_error_for(exc, f"streaming games for {username}"),
                    retry_after=getattr(exc, "retry_after", None),
                    what=f"game stream for {username}",
                )
                attempt += 1

    # ----------------------------------------------------------------- internal

    async def _stream_games_once(
        self, username: str, since_ms: int, until_ms: int, rated: bool
    ) -> AsyncGenerator[dict[str, Any], None]:
        params: dict[str, Any] = {
            "since": since_ms,
            "until": until_ms,
            "rated": rated,
            "opening": True,
            "accuracy": True,
            "moves": True,
            "clocks": False,
            # Per-move evals. No v1 report section uses them -- they are here
            # for the planned puzzle feature, which finds tactical positions by
            # looking for eval swings. See `lichess_include_evals`.
            "evals": settings.lichess_include_evals,
            # Ascending order is what makes resume-from-cursor possible.
            "sort": "dateAsc",
        }
        request = self._client.build_request(
            "GET",
            f"/api/games/user/{username}",
            params=params,
            headers={"Accept": "application/x-ndjson"},
            timeout=httpx.Timeout(
                settings.lichess_timeout_s, read=settings.lichess_stream_timeout_s
            ),
        )
        response = await self._client.send(request, stream=True)
        try:
            self._raise_for_status(response, username)
            async for line in response.aiter_lines():
                line = line.strip()
                if line:
                    yield json.loads(line)
        finally:
            await response.aclose()

    async def _get_json(self, path: str, *, username: str) -> Any:
        attempt = 0
        while True:
            try:
                response = await self._client.get(path)
                self._raise_for_status(response, username)
                return response.json()
            except (httpx.TransportError, RetryableStatusError) as exc:
                await self._wait_before_retry(
                    attempt,
                    error=_error_for(exc, f"requesting {path}"),
                    retry_after=getattr(exc, "retry_after", None),
                    what=path,
                )
                attempt += 1

    def _raise_for_status(self, response: httpx.Response, username: str) -> None:
        status = response.status_code
        if status == 404:
            raise UserNotFoundError(username)
        if status in RETRYABLE_STATUS:
            raise RetryableStatusError(status, response.headers.get("retry-after"))
        if status >= 400:
            raise LichessResponseError(status, str(response.url))

    async def _wait_before_retry(
        self, attempt: int, *, error: LichessError, retry_after: str | None, what: str
    ) -> None:
        """Sleep before the next attempt, or give up once the budget is spent."""
        if attempt >= settings.lichess_max_retries:
            raise error
        delay = self._retry_delay(attempt, retry_after)
        logger.warning(
            "%s failed (%s); retrying in %.1fs (attempt %d/%d)",
            what,
            error,
            delay,
            attempt + 1,
            settings.lichess_max_retries,
        )
        await sleep(delay)

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None) -> float:
        """Honour Retry-After when Lichess sends one, else exponential backoff.

        The jitter keeps a worker fleet that all hit the limit together from
        marching back in lockstep.
        """
        if retry_after is not None:
            try:
                return min(float(retry_after), BACKOFF_MAX_S)
            except ValueError:
                # Retry-After may be an HTTP date; fall through to backoff
                # rather than parsing a format Lichess doesn't actually send.
                pass
        return min(BACKOFF_BASE_S * 2.0**attempt, BACKOFF_MAX_S) + random.uniform(0, 0.5)
