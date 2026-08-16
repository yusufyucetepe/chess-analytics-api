"""The predicates the report endpoints branch on.

All of them are pure, and between them they decide whether a request causes a
year of games to be re-downloaded, so they are worth pinning down away from the
routes that use them.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.db.models import Player, Report, ReportStatus
from app.lichess.schemas import PlayerProfile
from app.report import service
from tests.conftest import FIXTURES


def ago(**kwargs: float) -> datetime:
    return datetime.now(UTC) - timedelta(**kwargs)


def test_a_report_from_this_morning_is_fresh() -> None:
    assert service.is_fresh(ago(hours=2)) is True


def test_a_report_from_last_week_is_not() -> None:
    assert service.is_fresh(ago(days=7)) is False


def test_a_report_that_never_finished_is_not_fresh() -> None:
    """``completed_at`` is null while a job is pending, and a cached view carries
    whatever the row had -- so the freshness rule has to survive being asked."""
    assert service.is_fresh(None) is False


def test_the_boundary_is_the_configured_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "report_fresh_ttl_s", 3600)

    assert service.is_fresh(ago(minutes=59)) is True
    assert service.is_fresh(ago(minutes=61)) is False


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (ReportStatus.PENDING, True),
        (ReportStatus.RUNNING, True),
        (ReportStatus.DONE, False),
        (ReportStatus.FAILED, False),
    ],
)
def test_only_an_unfinished_report_counts_as_in_flight(
    status: ReportStatus, expected: bool
) -> None:
    """Failed is finished: its ingest lock is stale, not still working."""
    assert service.in_flight(Report(status=status)) is expected


def test_a_report_that_is_not_there_is_not_in_flight() -> None:
    """The lock outliving its row is the case this exists for."""
    assert service.in_flight(None) is False


# --------------------------------------------------------------- new games


def profile(rated: int | None = 100, **extra: object) -> PlayerProfile:
    payload = {
        "id": "tester",
        "username": "Tester",
        "count": {} if rated is None else {"rated": rated},
        **extra,
    }
    return PlayerProfile.model_validate(payload)


def test_a_moved_count_means_there_is_something_to_fetch() -> None:
    assert service.has_new_games(Player(rated_games_count=100), profile(rated=103)) is True


def test_an_unmoved_count_means_a_rebuild_would_find_nothing() -> None:
    """The saving: no export, because it would produce the same report."""
    assert service.has_new_games(Player(rated_games_count=100), profile(rated=100)) is False


def test_a_player_we_have_never_fetched_always_has_new_games() -> None:
    """Null is "we do not know", and not knowing has to mean yes."""
    assert service.has_new_games(Player(rated_games_count=None), profile(rated=100)) is True


def test_a_profile_with_no_count_block_always_has_new_games() -> None:
    """Lichess omitting it must degrade to the old always-rebuild behaviour."""
    assert service.has_new_games(Player(rated_games_count=100), profile(rated=None)) is True


def test_a_count_that_went_down_still_counts_as_changed() -> None:
    """Games can be deleted with an account. Changed is changed."""
    assert service.has_new_games(Player(rated_games_count=100), profile(rated=98)) is True


def test_the_count_is_read_off_the_profile_lichess_actually_sends() -> None:
    """Guards the alias: the field is `count`, and we call it `counts`."""
    parsed = PlayerProfile.model_validate(json.loads((FIXTURES / "user_profile.json").read_text()))

    assert parsed.counts.rated == 132896


# ------------------------------------------------------------------- stale


def test_a_report_from_this_morning_is_not_stale() -> None:
    assert service.is_stale(ago(hours=2)) is False


def test_a_report_from_last_month_is_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the ceiling the count stops being trusted, because it cannot see
    analysis arriving on games we already hold."""
    monkeypatch.setattr(settings, "report_stale_ttl_s", 7 * 24 * 3600)

    assert service.is_stale(ago(days=30)) is True


def test_a_report_that_never_finished_is_stale() -> None:
    assert service.is_stale(None) is True


def test_the_two_ages_do_not_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh and stale must be mutually exclusive, or the middle band vanishes."""
    monkeypatch.setattr(settings, "report_fresh_ttl_s", 3600)
    monkeypatch.setattr(settings, "report_stale_ttl_s", 7200)

    for age in (0.5, 1.5, 3):
        moment = ago(hours=age)
        assert not (service.is_fresh(moment) and service.is_stale(moment)), age
