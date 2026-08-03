"""Lichess client exceptions.

Split by what the caller should *do*, not by HTTP status. The worker retries
transport trouble and gives up on a bad username, so those are the two shapes
that matter; everything else is a bug on our side and stays generic.
"""


class LichessError(Exception):
    """Base class for every failure raised by the Lichess client."""


class UserNotFoundError(LichessError):
    """The username does not exist. Never retryable -- waiting won't create it."""

    def __init__(self, username: str) -> None:
        self.username = username
        super().__init__(f"Lichess has no user {username!r}")


class RateLimitedError(LichessError):
    """Lichess returned 429 and our retry budget is spent."""


class LichessUnavailableError(LichessError):
    """Network trouble, a truncated body, or a 5xx that outlived the budget."""


class LichessResponseError(LichessError):
    """A response we don't know how to handle -- a 4xx that isn't 404 or 429."""

    def __init__(self, status_code: int, url: str) -> None:
        self.status_code = status_code
        super().__init__(f"Unexpected {status_code} from {url}")
