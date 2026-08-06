"""Shared Lichess client for the API process.

Same shape as ``app.core.redis``: one connection pool per process, created
lazily so importing this module never opens a socket. The worker deliberately
does not use it -- a report job owns a client for the length of one export, so
its long stream timeouts never leak into a request handler.
"""

from app.lichess.client import LichessClient

_client: LichessClient | None = None


def get_lichess() -> LichessClient:
    global _client
    if _client is None:
        _client = LichessClient()
    return _client


async def close_lichess() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
