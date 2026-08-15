"""The server-rendered frontend: a form, a poller, and a report page.

No SPA and no JSON fetched from the browser. The page is HTML the server built,
HTMX asks for a new fragment every couple of seconds while the job runs, and the
only JavaScript of our own draws three charts from data already on the page.

The handlers here do not reimplement the API. ``request_report`` and
``completed_report`` are called as ordinary functions with their collaborators
passed in, so there is exactly one description of what a report request does --
the difference between the two front doors is what they render, not what they
mean.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    EnqueueReport,
    enforce_rate_limit,
    enforce_read_rate_limit,
    get_enqueue,
    get_lichess_client,
)
from app.api.v1.reports import completed_report, request_report
from app.api.v1.schemas import ReportRequest, ReportView
from app.core.redis import get_redis
from app.db.base import get_session
from app.db.models import Player, ReportStatus
from app.lichess.client import LichessClient
from app.report import service
from app.web.charts import chart_data
from app.web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)

#: How often the browser asks whether the job has finished. A full export takes
#: minutes, so this is about the page feeling alive, not about precision.
POLL_SECONDS = 2

BAD_USERNAME = (
    "That doesn't look like a Lichess username -- letters, digits, "
    "underscores and hyphens, starting with a letter."
)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    """The form, and nothing else. No database, no Redis, no Lichess."""
    return templates.TemplateResponse(request, "index.html", {})


@router.post("/reports", response_class=HTMLResponse, dependencies=[Depends(enforce_rate_limit)])
async def submit(
    request: Request,
    username: str = Form(...),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    lichess: LichessClient = Depends(get_lichess_client),
    enqueue: EnqueueReport = Depends(get_enqueue),
) -> Response:
    """Start a report and hand back the fragment that will poll for it.

    Rate limited on the same bucket as the JSON endpoint: it is the same work
    and the same export, so a caller cannot double their budget by using the
    form instead.
    """
    try:
        body = ReportRequest(username=username)
    except ValidationError:
        return _problem(request, BAD_USERNAME, status.HTTP_422_UNPROCESSABLE_CONTENT)

    inner = Response()
    try:
        view = await request_report(
            body, inner, session=session, redis=redis, lichess=lichess, enqueue=enqueue
        )
    except HTTPException as exc:
        # The API's 404 for an unknown account and 503 for Lichess being down
        # are both things to say in the page, not status codes to leak.
        return _problem(request, str(exc.detail), exc.status_code)

    if view.status is ReportStatus.DONE:
        return _redirect(request, view.username)
    return _job(request, view)


@router.get("/reports/{report_id}/status", response_class=HTMLResponse)
async def job_status(
    request: Request, report_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> Response:
    """One poll. Returns the same fragment until the job reaches a terminal state.

    A finished job answers with ``HX-Redirect`` rather than the report itself:
    the result then arrives as an ordinary page load at a shareable URL, and the
    chart scripts run the way they would on any other page.
    """
    report = await service.get_report(session, report_id)
    if report is None:
        return _problem(request, "That report has gone.", status.HTTP_404_NOT_FOUND)

    player = await session.get(Player, report.player_id)
    view = ReportView.of(report, player.display_name if player else "")
    if view.status is ReportStatus.DONE:
        return _redirect(request, view.username)
    return _job(request, view)


@router.get(
    "/u/{username}",
    response_class=HTMLResponse,
    dependencies=[Depends(enforce_read_rate_limit)],
)
async def report_page(
    request: Request,
    username: str,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> Response:
    """The finished report, at a URL worth sharing.

    Never starts work: a player we have not built a report for is a page saying
    so with the form on it, not an export triggered by someone following a link.
    """
    player = await service.player_by_username(session, username)
    view = (
        await completed_report(session, redis, player, service.latest_completed) if player else None
    )
    if view is None or view.payload is None:
        return _problem(
            request,
            f"No report for {username} yet.",
            status.HTTP_404_NOT_FOUND,
            username=username,
        )
    sections = view.payload["sections"]
    return templates.TemplateResponse(
        request,
        "report.html",
        {
            "view": view,
            "report": view.payload,
            "sections": sections,
            "chart_data": chart_data(sections),
        },
    )


def _job(request: Request, view: ReportView) -> Response:
    """The polling fragment. Also a whole page, for a non-HTMX first load."""
    template = "partials/job.html" if _is_htmx(request) else "job.html"
    return templates.TemplateResponse(
        request,
        template,
        {"view": view, "poll_seconds": POLL_SECONDS, "failed": view.status is ReportStatus.FAILED},
    )


def _problem(request: Request, message: str, code: int, username: str = "") -> Response:
    template = "partials/problem.html" if _is_htmx(request) else "problem.html"
    return templates.TemplateResponse(
        request, template, {"message": message, "username": username}, status_code=code
    )


def _redirect(request: Request, username: str) -> Response:
    """Send the browser to the report page, whether or not HTMX is driving.

    ``HX-Redirect`` is how HTMX is told to navigate the whole window. A 303 in
    its place would be followed by the AJAX call itself and swapped in as a
    fragment, leaving the address bar on the form.
    """
    target = f"/u/{username}"
    if _is_htmx(request):
        return Response(status_code=status.HTTP_200_OK, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"
