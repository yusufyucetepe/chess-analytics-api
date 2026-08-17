"""Where the middle of a population sits, and how far from it a player is.

The classifier needs one thing from this module: is this player near enough to
the centre of players like them to be called an All-Rounder? That has to be a
share of a population rather than a distance in points, because the population
moves with the rating band. Lower-rated players flag more, play shorter games
and reach sharper positions than higher-rated ones at identical taste, so any
fixed threshold measures rating and calls it style.

The sample is the reports already built. Nothing is fetched to make it: every
completed report carries its own three scores, its main time control and the
rating that went with it, which is exactly the population the next report is
about to join. A scheduled job folds them into percentiles here.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Report, ReportStatus, StyleReference
from app.report.player_type_weights import AXES, RATING_BAND, REFERENCE_MIN_PLAYERS

#: Percentiles stored per band: 0..100 inclusive.
STEPS = 101


@dataclass(frozen=True, slots=True)
class Reference:
    """One band's centre, and the distances that surround it."""

    perf: str
    rating_band: int
    players: int
    centroid: dict[str, float]
    distances: tuple[float, ...]

    def percentile(self, scores: dict[str, int]) -> float:
        """How far out this player sits, as a share of the population inside them.

        0.0 is dead centre, 1.0 is the furthest anybody in the sample gets.
        """
        return _percentile(distance(scores, self.centroid), self.distances)


def band_of(rating: int | None) -> int | None:
    """The band a rating falls in, named by its lower bound."""
    return None if rating is None else (rating // RATING_BAND) * RATING_BAND


def distance(scores: dict[str, int], centroid: dict[str, float]) -> float:
    """Euclidean distance between a score split and a centre, in score points."""
    return math.dist(
        [float(scores.get(axis) or 0) for axis in AXES],
        [float(centroid.get(axis, 0.0)) for axis in AXES],
    )


async def load(session: AsyncSession, perf: str, rating: int | None) -> Reference | None:
    """This player's band, else the same time control at any rating, else nothing.

    Falling back across bands is a compromise and deliberately the second
    choice: the population is closer to right than no population at all, and a
    band that is still filling up would otherwise leave every report in it
    without a centre. Falling back across *time controls* is not offered --
    bullet and classical are different games.
    """
    rows = (
        await session.execute(
            select(StyleReference)
            .where(
                StyleReference.perf == perf,
                StyleReference.players >= REFERENCE_MIN_PLAYERS,
            )
            .order_by(StyleReference.rating_band)
        )
    ).scalars()

    bands = {row.rating_band: row for row in rows}
    if not bands:
        return None
    wanted = band_of(rating)
    row = bands.get(wanted) if wanted is not None else None
    if row is None:
        # The nearest band that does have a sample, so a 1500 falls back to 1400
        # rather than to whichever band happens to sort first.
        target = wanted if wanted is not None else 0
        row = min(
            bands.values(), key=lambda band: (abs(band.rating_band - target), band.rating_band)
        )
    return _reference(row)


async def refresh(session: AsyncSession) -> list[Reference]:
    """Recompute every band from the completed reports, and store the result.

    Reports are the sample and also the consumer, which sounds circular and is
    not: a report's scores are computed without reference to any population, and
    only the *label* asks where the middle is.
    """
    rows = (
        await session.execute(
            select(Report.payload).where(
                Report.status == ReportStatus.DONE, Report.payload.is_not(None)
            )
        )
    ).scalars()

    samples: dict[tuple[str, int], list[dict[str, int]]] = {}
    for payload in rows:
        sample = _sample(payload)
        if sample is not None:
            perf, band, scores = sample
            samples.setdefault((perf, band), []).append(scores)

    computed = [_summarise(perf, band, scores) for (perf, band), scores in sorted(samples.items())]
    for reference in computed:
        await session.execute(
            insert(StyleReference)
            .values(
                perf=reference.perf,
                rating_band=reference.rating_band,
                players=reference.players,
                centroid=reference.centroid,
                distances=list(reference.distances),
                computed_at=datetime.now(UTC),
            )
            .on_conflict_do_update(
                index_elements=["perf", "rating_band"],
                set_={
                    "players": reference.players,
                    "centroid": reference.centroid,
                    "distances": list(reference.distances),
                    "computed_at": datetime.now(UTC),
                },
            )
        )
    await session.commit()
    return computed


def summarise(perf: str, rating_band: int, samples: Sequence[dict[str, int]]) -> Reference:
    """The public form of ``_summarise``, for tests and for one-off analysis."""
    return _summarise(perf, rating_band, samples)


def _summarise(perf: str, rating_band: int, samples: Sequence[dict[str, int]]) -> Reference:
    centroid = {
        axis: sum(float(sample.get(axis) or 0) for sample in samples) / len(samples)
        for axis in AXES
    }
    spread = sorted(distance(sample, centroid) for sample in samples)
    return Reference(
        perf=perf,
        rating_band=rating_band,
        players=len(samples),
        centroid={axis: round(value, 4) for axis, value in centroid.items()},
        distances=tuple(round(_quantile(spread, step / (STEPS - 1)), 4) for step in range(STEPS)),
    )


def _sample(payload: Any) -> tuple[str, int, dict[str, int]] | None:
    """One report reduced to (time control, band, scores), or nothing usable.

    Written defensively because the input is a JSONB column that older schema
    versions also wrote to: a payload without scores is a report from before
    this existed, not a crash.
    """
    if not isinstance(payload, dict):
        return None
    sections = payload.get("sections") or {}
    player_type = sections.get("player_type") or {}
    scores = player_type.get("scores") or {}
    if any(scores.get(axis) is None for axis in AXES):
        return None

    progression = sections.get("progression") or {}
    perf = progression.get("main_perf")
    by_perf = (progression.get("by_perf") or {}).get(perf) or {}
    band = band_of(by_perf.get("end"))
    if not perf or band is None:
        return None
    return perf, band, {axis: int(scores[axis]) for axis in AXES}


def _reference(row: StyleReference) -> Reference:
    return Reference(
        perf=row.perf,
        rating_band=row.rating_band,
        players=row.players,
        centroid=dict(row.centroid),
        distances=tuple(row.distances),
    )


def _quantile(ordered: Sequence[float], at: float) -> float:
    """Linear interpolation between the two nearest samples."""
    if not ordered:
        return 0.0
    position = at * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _percentile(value: float, breakpoints: Sequence[float]) -> float:
    """Where ``value`` falls among stored breakpoints, as a share in 0..1."""
    if not breakpoints:
        return 1.0
    last = len(breakpoints) - 1
    if value <= breakpoints[0]:
        return 0.0
    for index, edge in enumerate(breakpoints):
        if value <= edge:
            span = edge - breakpoints[index - 1]
            within = (value - breakpoints[index - 1]) / span if span else 0.0
            return (index - 1 + within) / last
    return 1.0
