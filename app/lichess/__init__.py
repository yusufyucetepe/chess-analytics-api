"""Lichess API client."""

from app.lichess.client import LichessClient
from app.lichess.errors import (
    LichessError,
    LichessResponseError,
    LichessUnavailableError,
    RateLimitedError,
    UserNotFoundError,
)
from app.lichess.schemas import PerfRatingHistory, PerfStats, PlayerProfile, RatingPoint

__all__ = [
    "LichessClient",
    "LichessError",
    "LichessResponseError",
    "LichessUnavailableError",
    "PerfRatingHistory",
    "PerfStats",
    "PlayerProfile",
    "RateLimitedError",
    "RatingPoint",
    "UserNotFoundError",
]
