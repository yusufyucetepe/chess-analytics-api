"""Retry policy and the text a failed report shows a stranger.

The brief's rule -- "arq retries on network errors only, not on 'user doesn't
exist'" -- is one `isinstance` check away from being silently wrong, because
`UserNotFoundError` is itself a `LichessError`. Hence the tests.
"""

import httpx
import pytest

from app.lichess.errors import (
    LichessError,
    LichessResponseError,
    LichessUnavailableError,
    RateLimitedError,
    UserNotFoundError,
)
from app.worker.failures import DEFAULT_MESSAGE, is_retryable, user_message


@pytest.mark.parametrize(
    "exc",
    [
        LichessUnavailableError("lichess is down"),
        RateLimitedError("429"),
        TimeoutError(),
        ConnectionError(),
        OSError("connection reset"),
    ],
)
def test_transient_faults_are_retried(exc: Exception) -> None:
    assert is_retryable(exc) is True


def test_a_missing_user_is_never_retried() -> None:
    """It is a LichessError like the others; only the ordering saves us."""
    exc = UserNotFoundError("nosuchplayer")

    assert isinstance(exc, LichessError)
    assert is_retryable(exc) is False


@pytest.mark.parametrize(
    "exc",
    [
        LichessResponseError(400, "https://lichess.org/api/user/x"),
        ValueError("a bug in our own code"),
    ],
)
def test_everything_else_fails_once(exc: Exception) -> None:
    """A bug does not get better on the fourth attempt."""
    assert is_retryable(exc) is False


def test_httpx_transport_errors_are_retried() -> None:
    """The client wraps these, but a raw one must not end up permanent."""
    assert is_retryable(httpx.ConnectError("dns")) is True


# ------------------------------------------------------------------ messages


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (UserNotFoundError("nosuchplayer"), "No Lichess account with that username."),
        (
            RateLimitedError("429"),
            "Lichess is rate-limiting us right now. Try again in a few minutes.",
        ),
        (LichessUnavailableError("boom"), "Couldn't reach Lichess. Try again shortly."),
    ],
)
def test_known_failures_get_a_sentence(exc: Exception, expected: str) -> None:
    assert user_message(exc) == expected


def test_an_unexpected_error_never_leaks_its_text() -> None:
    """`reports.error` is served over HTTP, and this one carries a query string."""
    exc = RuntimeError("psql://chess:chess@db/chess failed on SELECT ... WHERE id = 7")

    assert user_message(exc) == DEFAULT_MESSAGE
    assert "chess:chess" not in user_message(exc)


def test_the_missing_user_message_does_not_echo_the_username() -> None:
    """The route already names it; the stored row need not repeat user input."""
    assert "nosuchplayer" not in user_message(UserNotFoundError("nosuchplayer"))
