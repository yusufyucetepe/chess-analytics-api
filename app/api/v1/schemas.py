"""Request and response models for the v1 API."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.db.models import Report, ReportStatus

Username = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=2,
        max_length=30,
        pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{1,29}$",
    ),
]


class ReportRequest(BaseModel):
    username: Username


class ReportView(BaseModel):
    """A report row as served: the job status, and the payload once there is one."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    status: ReportStatus
    period_start: datetime
    period_end: datetime
    created_at: datetime
    completed_at: datetime | None
    games_total: int
    games_analysed: int
    #: User-safe text, never a traceback. Set only when ``status`` is failed.
    error: str | None
    #: Absent until the job finishes -- a poller checks ``status``, not this.
    payload: dict[str, Any] | None

    @classmethod
    def of(cls, report: Report, username: str) -> "ReportView":
        """Build from a row plus the player's display name.

        The name is passed in rather than read off ``report.player`` so that
        serialising can never trigger a lazy load on an async session.
        """
        return cls.model_validate({**_columns(report), "username": username})


def _columns(report: Report) -> dict[str, Any]:
    return {
        "id": report.id,
        "status": report.status,
        "period_start": report.period_start,
        "period_end": report.period_end,
        "created_at": report.created_at,
        "completed_at": report.completed_at,
        "games_total": report.games_total,
        "games_analysed": report.games_analysed,
        "error": report.error,
        "payload": report.payload,
    }
