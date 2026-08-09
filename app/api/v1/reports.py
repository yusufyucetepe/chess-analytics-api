"""Report endpoints: ask for one, poll it, read the latest.

The POST is the only place in the app that talks to Lichess synchronously, and
only to confirm the account exists. Everything slow belongs to the worker.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import EnqueueReport, enforce_rate_limit, get_enqueue, get_lichess_client
from app.api.v1.schemas import ReportRequest, ReportView, Username
from app.core import locks
from app.core.config import settings
from app.core.redis import get_redis
from app.db.base import get_session
from app.db.models import Player, Report
from app.ingest import upsert_player
from app.lichess.client import LichessClient
from app.lichess.errors import LichessError, UserNotFoundError
from app.lichess.schemas import PlayerProfile
from app.report import cache, service
from app.report.sections import openings
from app.worker.failures import user_message

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["reports"])

#: A lookup that returns the newest finished report worth serving, or nothing.
Lookup = Callable[[AsyncSession, int], Awaitable[Report | None]]

BUSY = "Can't take new report requests right now. Try again shortly."


@router.post(
    "/reports",
    response_model=ReportView,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request a report for a Lichess username",
    dependencies=[Depends(enforce_rate_limit)],
    responses={
        status.HTTP_200_OK: {"description": "A recent report already exists; returned as is"},
        status.HTTP_404_NOT_FOUND: {"description": "No such Lichess account"},
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Too many reports from this address"},
    },
)
async def request_report(
    body: ReportRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    lichess: LichessClient = Depends(get_lichess_client),
    enqueue: EnqueueReport = Depends(get_enqueue),
) -> ReportView:
    """Return a fresh report if we have one, otherwise queue a new job.

    The Redis lock, not the row, is what stops two exports of the same player:
    a database check on "is one already running" is a read followed by a write,
    which two simultaneous requests can both win.
    """
    profile = await _fetch_profile(lichess, body.username)
    player = await upsert_player(session, profile)

    recent = await _completed_report(session, redis, player, service.fresh_report)
    if recent and service.is_fresh(recent.completed_at):
        response.status_code = status.HTTP_200_OK
        return recent

    report_id = uuid.uuid4()
    if in_flight := await _claim_or_join(session, redis, player, report_id):
        logger.info("report %s is already in flight for %s", in_flight.id, player.username_lower)
        return ReportView.of(in_flight, player.display_name)

    period_start, period_end = service.report_window()
    report = await service.create_report(
        session,
        player.id,
        report_id=report_id,
        period_start=period_start,
        period_end=period_end,
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
    username: Username,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> ReportView:
    """Served entirely from Redis and our own tables -- never triggers a fetch.

    A username we have never seen is a 404 rather than an implicit job: work
    created from a GET would be an unmetered path to the Lichess export.
    """
    player = await _known_player(session, username)
    report = await _completed_report(session, redis, player, service.latest_completed)
    if report is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No completed report for that player yet. POST /api/v1/reports to build one.",
        )
    return report


@router.get(
    "/players/{username}/openings",
    summary="The player's repertoire, computed live from the games we hold",
)
async def player_openings(
    username: Username, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """The openings section on its own, for a caller that wants only the tree.

    Recomputed rather than lifted out of the last report's payload: the games
    table is the source of truth, and it may hold a window the newest completed
    report does not cover.
    """
    player = await _known_player(session, username)
    since, until = service.report_window()
    return {
        "username": player.display_name,
        "period": {"start": since.isoformat(), "end": until.isoformat()},
        "openings": await openings.build(session, player.id, since=since, until=until),
    }


async def _completed_report(
    session: AsyncSession, redis: Redis, player: Player, lookup: Lookup
) -> ReportView | None:
    """Read through Redis to the table, caching whatever the table gave.

    Both callers want the same thing -- the newest finished report -- and differ
    only in whether they will accept an old one. So one entry serves both, and
    the freshness rule is applied to ``completed_at`` afterwards rather than
    being baked into the key.
    """
    if cached := await cache.load(redis, player.username_lower):
        return ReportView.model_validate(cached)

    report = await lookup(session, player.id)
    if report is None:
        return None

    view = ReportView.of(report, player.display_name)
    await cache.store(redis, player.username_lower, view.model_dump(mode="json"))
    return view


async def _claim_or_join(
    session: AsyncSession, redis: Redis, player: Player, report_id: uuid.UUID
) -> Report | None:
    """Take the ingest lock, or return the report already holding it.

    ``None`` means the lock is ours and the caller should create the row.
    """
    key = locks.report_lock_key(player.username_lower)
    try:
        holder = await locks.acquire(redis, key, str(report_id), settings.ingest_lock_ttl_s)
        if holder == str(report_id):
            return None
        if existing := await _in_flight_report(session, holder):
            return existing
        # The lock names a report that is finished or gone, so nothing is being
        # built and the key is purely in the way -- a worker killed between
        # writing its result and releasing. Take it over rather than making this
        # player wait out a half-hour TTL for a job nobody is running.
        logger.warning("ingest lock for %s named stale report %s", player.username_lower, holder)
        await locks.force_acquire(redis, key, str(report_id), settings.ingest_lock_ttl_s)
    except RedisError as exc:
        # Without the lock there is nothing stopping a duplicate export, and the
        # enqueue needs the same Redis anyway -- so this fails rather than
        # quietly running unprotected.
        logger.error("ingest lock for %s unavailable: %s", player.username_lower, exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, BUSY) from exc
    return None


async def _in_flight_report(session: AsyncSession, holder: str) -> Report | None:
    """The report a lock token names, if it is still being worked on."""
    try:
        report_id = uuid.UUID(holder)
    except ValueError:
        return None
    report = await service.get_report(session, report_id)
    return report if service.in_flight(report) else None


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
