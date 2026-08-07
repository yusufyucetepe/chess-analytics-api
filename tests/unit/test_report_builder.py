"""The payload envelope, and the coverage rule the brief calls non-negotiable."""

from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.config import settings
from app.db.models import REPORT_SCHEMA_VERSION, Player
from app.report.builder import SECTIONS, build_payload, coverage

PERIOD_START = datetime(2025, 8, 6, tzinfo=UTC)
PERIOD_END = datetime(2026, 8, 6, tzinfo=UTC)


@pytest.fixture
def player() -> Player:
    """Detached, never added to a session -- the builder only reads attributes."""
    return Player(
        id=1,
        username_lower="zhigalko_sergei",
        display_name="Zhigalko_Sergei",
        title="GM",
        country="BY",
        ratings={"bullet": 3021, "blitz": 2836},
    )


def payload(player: Player, total: int = 1207, analysed: int = 84) -> dict[str, Any]:
    return build_payload(
        player=player,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        games_total=total,
        games_analysed=analysed,
        generated_at=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
    )


# ----------------------------------------------------------------- coverage


def test_coverage_states_the_denominator() -> None:
    """ "Based on 84 of 1,207 games analysed" is the brief's own example."""
    block = coverage(1207, 84)

    assert block["games_total"] == 1207
    assert block["games_analysed"] == 84
    assert block["analysis_rate"] == pytest.approx(84 / 1207, abs=1e-4)
    assert "84 of 1,207" in block["caveat"]


def test_thin_coverage_is_called_out_rather_than_averaged() -> None:
    """Under the threshold the report says so instead of quoting a number."""
    block = coverage(400, settings.quality_min_analysed_games - 1)

    assert block["quality_reliable"] is False
    assert "too few" in block["caveat"]


def test_coverage_at_the_threshold_is_usable() -> None:
    assert coverage(400, settings.quality_min_analysed_games)["quality_reliable"] is True


def test_a_player_with_no_games_does_not_divide_by_zero() -> None:
    block = coverage(0, 0)

    assert block["analysis_rate"] == 0.0
    assert block["quality_reliable"] is False


def test_every_payload_carries_coverage(player: Player) -> None:
    """No section may ever be read without the caveat that qualifies it."""
    assert payload(player)["coverage"]["games_analysed"] == 84


# ------------------------------------------------------------------ envelope


def test_the_envelope_identifies_the_player_and_the_window(player: Player) -> None:
    body = payload(player)

    assert body["schema_version"] == REPORT_SCHEMA_VERSION
    assert body["generated_at"] == "2026-08-06T12:00:00+00:00"
    assert body["player"] == {
        "username": "Zhigalko_Sergei",
        "title": "GM",
        "country": "BY",
        "ratings": {"bullet": 3021, "blitz": 2836},
    }
    assert body["period"] == {
        "start": PERIOD_START.isoformat(),
        "end": PERIOD_END.isoformat(),
        "days": settings.report_window_days,
    }


def test_a_section_the_builder_was_not_given_is_null(player: Player) -> None:
    """``player_type`` arrives in step 6; until then it is present and empty."""
    assert payload(player)["sections"] == dict.fromkeys(SECTIONS)


def test_computed_sections_are_placed_by_name(player: Player) -> None:
    built = build_payload(
        player=player,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        games_total=10,
        games_analysed=0,
        sections={"headline": {"games": 10}, "not_a_section": {"games": 99}},
    )

    assert built["sections"]["headline"] == {"games": 10}
    assert built["sections"]["openings"] is None
    # Keyed off SECTIONS, so a consumer can rely on the set of names it gets.
    assert set(built["sections"]) == set(SECTIONS)


def test_the_payload_is_json_native(player: Player) -> None:
    """It goes into a JSONB column, so nothing may need a custom encoder."""
    import json

    assert json.loads(json.dumps(payload(player)))["coverage"]["games_total"] == 1207
