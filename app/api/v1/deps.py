"""Injectable collaborators for the v1 routes.

Both of these reach outside the process -- Lichess over HTTP, arq over Redis --
so they go through ``Depends`` rather than being imported into the handlers
directly. That is what lets a test swap in a recorder without a live queue.
"""

import uuid
from collections.abc import Awaitable, Callable

from app.core.lichess import get_lichess
from app.lichess.client import LichessClient
from app.worker.queue import enqueue_report

#: Queue a build job for a report that already exists.
EnqueueReport = Callable[[uuid.UUID], Awaitable[None]]


def get_enqueue() -> EnqueueReport:
    return enqueue_report


def get_lichess_client() -> LichessClient:
    return get_lichess()
