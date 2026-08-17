"""The reference population: built from stored reports, read back per band.

The classifier's All-Rounder is a percentile against players like you, so these
tests are about the round trip that percentile depends on -- reports go in, one
row per (time control, rating band) comes out, and a report built afterwards
lands somewhere sensible inside it.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Player, Report, ReportStatus, StyleReference
from app.report import style_reference
from app.report.player_type_weights import (
    AXES,
    BLENDED_NOUN,
    CENTRIST_SHARE,
    REFERENCE_MIN_PLAYERS,
)
from app.report.sections.player_type import Signals, classify
from tests.unit.test_player_type import tags

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)


def payload(scores: dict[str, int], perf: str = "blitz", rating: int = 1450) -> dict[str, Any]:
    """Only the parts of a report the reference reads."""
    return {
        "sections": {
            "player_type": {"scores": scores},
            "progression": {"main_perf": perf, "by_perf": {perf: {"end": rating}}},
        }
    }


async def store(session: AsyncSession, payloads: list[dict[str, Any]]) -> None:
    """One completed report per payload, each against its own player."""
    for index, body in enumerate(payloads):
        player = Player(username_lower=f"player{index:04d}", display_name=f"Player{index}")
        session.add(player)
        await session.flush()
        session.add(
            Report(
                id=uuid.uuid4(),
                player_id=player.id,
                period_start=NOW - timedelta(days=365),
                period_end=NOW,
                status=ReportStatus.DONE,
                payload=body,
                games_total=200,
            )
        )
    await session.commit()


def spread(count: int) -> list[dict[str, int]]:
    """A band of players fanned out along the positional/aggressive line."""
    return [
        {"positional": 20 + step % 50, "aggressive": 60 - step % 50, "tactical": 20}
        for step in range(count)
    ]


async def test_a_band_is_summarised_from_the_reports_in_it(session: AsyncSession) -> None:
    await store(session, [payload(scores) for scores in spread(REFERENCE_MIN_PLAYERS + 10)])

    bands = await style_reference.refresh(session)

    assert [(band.perf, band.rating_band) for band in bands] == [("blitz", 1400)]
    assert bands[0].players == REFERENCE_MIN_PLAYERS + 10
    stored = (await session.execute(select(StyleReference))).scalars().all()
    assert len(stored) == 1
    assert set(stored[0].centroid) == set(AXES)
    assert len(stored[0].distances) == style_reference.STEPS


async def test_the_bands_are_kept_apart(session: AsyncSession) -> None:
    """A 1200 and a 2200 are not the same population, and neither are a bullet
    player and a classical one."""
    await store(
        session,
        [payload(scores, rating=1250) for scores in spread(REFERENCE_MIN_PLAYERS)]
        + [payload(scores, rating=2250) for scores in spread(REFERENCE_MIN_PLAYERS)]
        + [payload(scores, perf="bullet") for scores in spread(REFERENCE_MIN_PLAYERS)],
    )

    bands = await style_reference.refresh(session)

    assert {(band.perf, band.rating_band) for band in bands} == {
        ("blitz", 1200),
        ("blitz", 2200),
        ("bullet", 1400),
    }


async def test_a_band_nobody_is_in_yet_is_not_a_population(session: AsyncSession) -> None:
    """Below the floor the classifier gets nothing rather than a centre computed
    from four people."""
    await store(session, [payload(scores) for scores in spread(REFERENCE_MIN_PLAYERS - 1)])
    await style_reference.refresh(session)

    assert await style_reference.load(session, "blitz", 1450) is None


async def test_a_player_falls_back_to_the_nearest_band_with_a_sample(
    session: AsyncSession,
) -> None:
    """Second choice and deliberately so: a neighbouring band is closer to right
    than refusing to place the player at all."""
    await store(session, [payload(scores, rating=1450) for scores in spread(REFERENCE_MIN_PLAYERS)])
    await style_reference.refresh(session)

    reference = await style_reference.load(session, "blitz", 2050)

    assert reference is not None
    assert reference.rating_band == 1400


async def test_time_control_is_never_fallen_back_across(session: AsyncSession) -> None:
    """Bullet and classical are different games, so a bullet player with no
    bullet band gets no label rather than a blitz one."""
    await store(session, [payload(scores) for scores in spread(REFERENCE_MIN_PLAYERS)])
    await style_reference.refresh(session)

    assert await style_reference.load(session, "classical", 1450) is None


async def test_a_report_from_before_the_scores_existed_is_skipped(session: AsyncSession) -> None:
    """The sample is a JSONB column older schema versions also wrote to."""
    await store(
        session,
        [payload(scores) for scores in spread(REFERENCE_MIN_PLAYERS)]
        + [{"sections": {}}, {"sections": {"player_type": {"scores": {}}}}],
    )

    bands = await style_reference.refresh(session)

    assert bands[0].players == REFERENCE_MIN_PLAYERS


async def test_refreshing_twice_replaces_rather_than_duplicates(session: AsyncSession) -> None:
    await store(session, [payload(scores) for scores in spread(REFERENCE_MIN_PLAYERS)])

    await style_reference.refresh(session)
    first = (await session.execute(select(StyleReference))).scalar_one()
    computed = first.computed_at
    await style_reference.refresh(session)
    again = (await session.execute(select(StyleReference))).scalars().all()

    assert len(again) == 1
    assert again[0].computed_at >= computed


@pytest.mark.parametrize("population", [120])
async def test_no_more_than_a_sixth_of_stored_reports_are_all_rounders(
    session: AsyncSession, population: int
) -> None:
    """The rule the rewrite exists to enforce, measured the way the complaint
    was: over a sample of stored reports rather than over one hand-built player.

    Every player here is classified against the band their own report helped
    build, which is exactly what happens in production once the cron has run.
    """
    band = [
        Signals(
            games=200,
            **tags({"sharp": sharp, "closed": 100 - sharp, "open": sharp // 2}, 200),
            openness=sharp / 100,
            avg_plies=40.0 + sharp * 0.5,
            decisive=190,
            draws=10,
            forced_endings=190 - sharp,
            flagged=sharp // 3,
            avg_opening_captures=sharp / 40,
        )
        for sharp in range(1, population + 1)
    ]
    verdicts = [classify(signals) for signals in band]
    await store(session, [payload(verdict["scores"]) for verdict in verdicts])
    await style_reference.refresh(session)
    reference = await style_reference.load(session, "blitz", 1450)

    assert reference is not None
    labels = [classify(signals, reference)["label"] for signals in band]
    centrists = [label for label in labels if label.endswith(BLENDED_NOUN)]

    assert len(centrists) / len(labels) <= CENTRIST_SHARE + 0.02
    assert centrists, "the label still has to be reachable"
