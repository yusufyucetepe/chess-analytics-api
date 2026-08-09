"""The two predicates the report endpoints branch on.

Both are pure, and both decide whether a request causes a year of games to be
re-downloaded, so they are worth pinning down away from the routes that use them.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.db.models import Report, ReportStatus
from app.report import service


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
