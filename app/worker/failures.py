"""What to do when a report job raises, and what to tell the user about it.

Retryability is a property of the exception, not of how many attempts are left:
network errors get another go, "user doesn't exist" never does. Whatever ends up
in ``reports.error`` is served straight over HTTP, so it must read as a sentence
and never as a traceback.
"""

import httpx

from app.lichess.errors import (
    LichessResponseError,
    LichessUnavailableError,
    RateLimitedError,
    UserNotFoundError,
)

#: ``OSError`` covers ``TimeoutError`` and ``ConnectionError``, which is how
#: asyncpg and redis-py surface a connection dying mid-job. httpx's own errors
#: descend from ``Exception`` instead, so they have to be named separately.
RETRYABLE = (
    LichessUnavailableError,
    RateLimitedError,
    OSError,
    httpx.TransportError,
)

#: Retrying a username that does not exist just spends someone else's rate limit.
PERMANENT = (UserNotFoundError,)

_MESSAGES: tuple[tuple[type[BaseException], str], ...] = (
    (UserNotFoundError, "No Lichess account with that username."),
    (RateLimitedError, "Lichess is rate-limiting us right now. Try again in a few minutes."),
    (LichessUnavailableError, "Couldn't reach Lichess. Try again shortly."),
    (LichessResponseError, "Lichess rejected the request. Try again later."),
)

DEFAULT_MESSAGE = "Something went wrong building this report."


def is_retryable(exc: BaseException) -> bool:
    """Whether the job should be handed back to arq for another attempt."""
    # PERMANENT wins: UserNotFoundError is a LichessError like the others, and
    # only the ordering keeps it from looking retryable.
    if isinstance(exc, PERMANENT):
        return False
    return isinstance(exc, RETRYABLE)


def user_message(exc: BaseException) -> str:
    """A sentence safe to show a stranger on a public web page.

    Anything unrecognised gets the generic message on purpose: an unexpected
    exception's text can carry a URL, a query or a stack frame. The real error
    goes to the logs.
    """
    for error_type, message in _MESSAGES:
        if isinstance(exc, error_type):
            return message
    return DEFAULT_MESSAGE
