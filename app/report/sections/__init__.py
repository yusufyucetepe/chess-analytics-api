"""The five computed sections of a report.

Each module owns one section and answers with plain JSON-native values. They run
one after another rather than concurrently: they share the job's single
``AsyncSession``, and a session can only have one statement in flight.
"""

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.report.sections import headline, openings, progression, quality, time_behaviour


async def build_sections(
    session: AsyncSession,
    player_id: int,
    *,
    since: datetime,
    until: datetime,
    games_analysed: int,
) -> dict[str, Any]:
    """Compute every section a v1 report has. ``player_type`` arrives in step 6."""
    return {
        "headline": await headline.build(session, player_id, since=since, until=until),
        "openings": await openings.build(session, player_id, since=since, until=until),
        "quality": await quality.build(
            session, player_id, since=since, until=until, games_analysed=games_analysed
        ),
        "time_behaviour": await time_behaviour.build(session, player_id, since=since, until=until),
        "progression": await progression.build(session, player_id, since=since, until=until),
    }


__all__ = ["build_sections", "headline", "openings", "progression", "quality", "time_behaviour"]
