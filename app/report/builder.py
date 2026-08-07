"""Assembling the report payload.

The envelope only. Sections are computed against the database in
``app.report.sections`` and handed in, which keeps this module pure and lets the
payload shape be tested without a session. Coverage is computed here, next to
the counts it comes from, so no section can quote an accuracy figure without the
caveat travelling alongside it.
"""

from datetime import UTC, datetime
from typing import Any

from app.core.config import settings
from app.db.models import REPORT_SCHEMA_VERSION, Player

SECTIONS = (
    "headline",
    "openings",
    "quality",
    "time_behaviour",
    "progression",
    "player_type",
)


def quality_reliable(games_analysed: int) -> bool:
    """Whether there is enough analysis to report quality numbers at all."""
    return games_analysed >= settings.quality_min_analysed_games


def coverage(games_total: int, games_analysed: int) -> dict[str, Any]:
    """How much of the year has engine analysis behind it.

    We never run Stockfish: the quality figures exist only for games the player
    themselves asked Lichess to analyse, a small and self-selected slice. So the
    report always states the denominator, and below
    ``quality_min_analysed_games`` it says the numbers are not worth reading
    rather than printing an average that means nothing.
    """
    reliable = quality_reliable(games_analysed)
    return {
        "games_total": games_total,
        "games_analysed": games_analysed,
        "analysis_rate": round(games_analysed / games_total, 4) if games_total else 0.0,
        "min_analysed_games": settings.quality_min_analysed_games,
        "quality_reliable": reliable,
        "caveat": (
            f"Based on the {games_analysed:,} of {games_total:,} games that were analysed."
            if reliable
            else (
                f"Only {games_analysed:,} of {games_total:,} games were analysed -- too few "
                "to say anything honest about accuracy."
            )
        ),
    }


def build_payload(
    *,
    player: Player,
    period_start: datetime,
    period_end: datetime,
    games_total: int,
    games_analysed: int,
    sections: dict[str, Any] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """The report as stored in ``reports.payload`` and served over HTTP.

    Everything is JSON-native: the column is JSONB, so datetimes are ISO strings
    rather than objects the serialiser would have to be taught about.
    """
    computed = sections or {}
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": (generated_at or datetime.now(UTC)).isoformat(),
        "player": {
            "username": player.display_name,
            "title": player.title,
            "country": player.country,
            "ratings": player.ratings,
        },
        "period": {
            "start": period_start.isoformat(),
            "end": period_end.isoformat(),
            "days": settings.report_window_days,
        },
        "coverage": coverage(games_total, games_analysed),
        # Keyed off SECTIONS rather than merged: a consumer can rely on every
        # section name being present, and on nothing else appearing.
        "sections": {name: computed.get(name) for name in SECTIONS},
    }
