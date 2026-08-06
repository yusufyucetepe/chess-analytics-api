"""Report endpoints: ask for one, poll it, read the latest.

The POST is the only place in the app that talks to Lichess synchronously, and
only to confirm the account exists. Everything slow belongs to the worker.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import EnqueueReport, get_enqueue, get_lichess_client
from app.api.v1.schemas import ReportRequest, ReportView, Username
from app.db.base import get_session
from app.db.models import Player, Report
from app.ingest import upsert_player
from app.lichess.client import LichessClient
from app.lichess.errors import LichessError, UserNotFoundError
from app.lichess.schemas import PlayerProfile
from app.report import service
from app.worker.failures import user_message

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["reports"])


@router.post(
    "/reports",
    response_model=ReportView,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request a report for a Lichess username",
    responses={
        status.HTTP_200_OK: {"description": "A recent report already exists; returned as is"},
        status.HTTP_404_NOT_FOUND: {"description": "No such Lichess account"},
    },
)
async def request_report(
    body: ReportRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
    lichess: LichessClient = Depends(get_lichess_client),
    enqueue: EnqueueReport = Depends(get_enqueue),
) -> ReportView:
    """Return a fresh report if we have one, otherwise queue a new job.

    Rate limiting and the Redis dedupe lock arrive in step 7. Until then the
    duplicate check is a database one, so two simultaneous requests can both
    miss it -- but it covers the case that actually happens, a page refresh.
    """
    profile = await _fetch_profile(lichess, body.username)
    player = await upsert_player(session, profile)

    if fresh := await service.fresh_report(session, player.id):
        response.status_code = status.HTTP_200_OK
        return ReportView.of(fresh, player.display_name)

    if active := await service.active_report(session, player.id):
        logger.info("report %s is already in flight for %s", active.id, player.username_lower)
        return ReportView.of(active, player.display_name)

    period_start, period_end = service.report_window()
    report = await service.create_report(
        session, player.id, period_start=period_start, period_end=period_end
    )
    await enqueue(report.id)
    logger.info("queued report %s for %s", report.id, player.username_lower)
    return ReportView.of(report, player.display_name)


@router.get(
    "/reports/{report_id}",
    response_model=ReportView,
    summary="Poll a report, or read it once it is done",
)
async def read_report(
    report_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> ReportView:
    report = await service.get_report(session, report_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No report with that id.")
    return ReportView.of(report, await _display_name(session, report))


@router.get(
    "/players/{username}/report",
    response_model=ReportView,
    summary="The latest completed report for a player",
)
async def latest_report(
    username: Username, session: AsyncSession = Depends(get_session)
) -> ReportView:
    """Served entirely from our own tables -- never triggers a fetch.

    A username we have never seen is a 404 rather than an implicit job: work
    created from a GET would be an unmetered path to the Lichess export.
    """
    player = await _known_player(session, username)
    report = await service.latest_completed(session, player.id)
    if report is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No completed report for that player yet. POST /api/v1/reports to build one.",
        )
    return ReportView.of(report, player.display_name)


async def _fetch_profile(lichess: LichessClient, username: str) -> PlayerProfile:
    # A bad username has to fail here as a 404. Enqueued instead, it would
    # surface minutes later as a failed report, and the user would not know
    # whether they mistyped or we broke.
    try:
        return await lichess.get_profile(username)
    except UserNotFoundError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"No Lichess account named {username!r}."
        ) from exc
    except LichessError as exc:
        logger.warning("profile lookup for %s failed: %s", username, exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, user_message(exc)) from exc


async def _known_player(session: AsyncSession, username: str) -> Player:
    player = await service.player_by_username(session, username)
    if player is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"We have no data for {username!r} yet.")
    return player


async def _display_name(session: AsyncSession, report: Report) -> str:
    player = await session.get(Player, report.player_id)
    return player.display_name if player else ""
