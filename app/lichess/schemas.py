"""Typed views over the Lichess responses we consume whole.

Only the profile and rating-history payloads are modelled here. Games stay raw
``dict``s: the export is large, its shape varies with the query params, and the
mapping to database rows belongs to the ingest layer rather than the client.
"""

from datetime import UTC, date, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

#: A Lichess username: a letter, then letters, digits, underscores or hyphens.
#:
#: Written down once, and applied in both directions. Validating the request
#: body is not enough on its own, because the value the app then acts on is the
#: one Lichess sends back -- that is what goes into the export URL and into the
#: Redis lock and cache keys. Anything outside this set could change the shape
#: of a path or collide two players onto one key, so the response is held to the
#: same rule as the request. A name we would refuse to accept is one we could
#: never have looked up.
LichessUsername = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=2,
        max_length=30,
        pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{1,29}$",
    ),
]


def epoch_ms_to_datetime(value: int) -> datetime:
    """Lichess timestamps are epoch milliseconds, always UTC.

    Done explicitly rather than leaning on Pydantic's int-to-datetime coercion,
    which switches between seconds and milliseconds on a magnitude heuristic --
    fine today, silently wrong the day a field arrives in seconds.
    """
    return datetime.fromtimestamp(value / 1000, tz=UTC)


class PerfStats(BaseModel):
    """One perf's standing in a player's profile.

    Every field is optional because ``perfs`` is not homogeneous: alongside the
    time controls it carries the arcade modes (storm, racer, streak), which
    report ``{runs, score}`` and have neither a game count nor a rating.
    """

    games: int = 0
    rating: int | None = None
    #: Rating change over the player's last twelve games.
    prog: int | None = None
    #: Provisional: too few games for the rating to mean much.
    prov: bool = False


class UserProfileDetail(BaseModel):
    model_config = ConfigDict(extra="ignore")

    #: ISO-ish country code, e.g. "BY". Absent for players who never set one.
    flag: str | None = None


class ProfileCounts(BaseModel):
    """The ``count`` block: how many games the account has ever played.

    Only ``rated`` is read. The export is rated-only, so it is the number that
    moves when there is something new for us to fetch -- a week of casual games
    changes ``all`` and nothing we would store.
    """

    model_config = ConfigDict(extra="ignore")

    rated: int | None = None


class PlayerProfile(BaseModel):
    """``GET /api/user/{username}``."""

    model_config = ConfigDict(extra="ignore")

    #: Lowercased username; Lichess treats this as the canonical id, and so do
    #: we -- it becomes ``players.username_lower`` and every Redis key.
    id: LichessUsername
    username: LichessUsername
    title: str | None = None
    created_at: datetime | None = Field(default=None, alias="createdAt")
    perfs: dict[str, PerfStats] = Field(default_factory=dict)
    profile: UserProfileDetail = Field(default_factory=UserProfileDetail)
    counts: ProfileCounts = Field(default_factory=ProfileCounts, alias="count")

    @field_validator("created_at", mode="before")
    @classmethod
    def _parse_created_at(cls, value: Any) -> Any:
        return epoch_ms_to_datetime(value) if isinstance(value, int) else value

    @property
    def country(self) -> str | None:
        return self.profile.flag

    def ratings(self) -> dict[str, int]:
        """Current rating per perf, for the ``players.ratings`` JSONB column.

        Two kinds of entry are dropped. Perfs the player has never touched come
        back at a provisional 1500/2500 that would read as a real rating, and
        the arcade modes have no rating to report in the first place.
        """
        return {
            name: perf.rating
            for name, perf in self.perfs.items()
            if perf.games > 0 and perf.rating is not None
        }


class RatingPoint(BaseModel):
    day: date
    rating: int


class PerfRatingHistory(BaseModel):
    """One perf's curve from ``GET /api/user/{username}/rating-history``."""

    #: Display name, e.g. "Bullet" -- note the profile uses "bullet".
    name: str
    points: list[RatingPoint]

    @field_validator("points", mode="before")
    @classmethod
    def _parse_points(cls, value: Any) -> Any:
        """Points arrive as ``[year, month, day, rating]`` with a 0-indexed month.

        Verified against live data: a player's first bullet game on 2018-09-20
        appears as ``[2018, 8, 20, 2707]``. Reading the month as 1-indexed shifts
        every rating curve a month early.
        """
        if not isinstance(value, list):
            return value
        parsed = []
        for point in value:
            if isinstance(point, list) and len(point) == 4:
                year, month, day, rating = point
                parsed.append({"day": date(year, month + 1, day), "rating": rating})
            else:
                parsed.append(point)
        return parsed
