"""Building ``games`` rows by hand, for the tests that are about the queries.

Games are written straight to the table rather than ingested: these tests ask
what the section SQL says about a given set of rows, and going through the
export would only make the inputs harder to read.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Color, Game, Player, Result

#: Deliberately fixed rather than relative to now: every assertion is about
#: buckets and orderings, and a moving window makes those flaky.
BASE = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
SINCE = BASE - timedelta(days=180)
UNTIL = BASE + timedelta(days=180)

DEFAULTS: dict[str, Any] = {
    "perf": "blitz",
    "rated": True,
    "color": Color.WHITE,
    "result": Result.WIN,
    "status": "resign",
    "moves_count": 60,
    "duration_s": 300,
    "opening_line": [],
    "analysed": False,
}


async def seed(session: AsyncSession, player: Player, specs: list[dict[str, Any]]) -> None:
    """Write games one minute apart, in list order, so time follows the source."""
    for index, spec in enumerate(specs):
        session.add(
            Game(
                **{
                    **DEFAULTS,
                    "game_id": f"game{index:04d}",
                    "player_id": player.id,
                    "played_at": BASE + timedelta(minutes=index),
                    **spec,
                }
            )
        )
    await session.commit()


def results(*letters: str) -> list[dict[str, Any]]:
    """'WWLD' as four games -- the shorthand the streak and tilt tests read in."""
    lookup = {"W": Result.WIN, "L": Result.LOSS, "D": Result.DRAW}
    return [{"result": lookup[letter]} for letter in letters]
