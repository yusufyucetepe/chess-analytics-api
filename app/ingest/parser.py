"""One NDJSON game object -> one ``games`` row.

Pure functions: no database, no network, no settings beyond a ply count. That
keeps the layer that has to cope with every oddity in Lichess's export testable
against recorded fixtures alone.

The export is per *game*, but our rows are per *player*: colour, ratings,
result and the analysis block all depend on whose report we are building, so
the username being ingested is an argument, not a detail.
"""

from decimal import Decimal
from typing import Any

from app.core.config import settings
from app.db.models import Color, Result
from app.lichess.schemas import epoch_ms_to_datetime

#: We only ingest standard chess. Chess960, atomic and the rest carry no ECO
#: code and would quietly pollute the opening tree and the perf breakdown.
STANDARD_VARIANT = "standard"

#: Games in which nobody moved a piece. Some of these still carry a winner --
#: a no-show in an arena forfeits the point, and `noStart` reports a winner and
#: a rating change (verified on a live game, kfDYQ0Yw) -- so filtering on the
#: winner alone is not enough. They are excluded because a report that credits
#: the player with wins they never played, at a game length of zero, is lying.
UNPLAYED_STATUSES = frozenset({"created", "started", "aborted", "noStart"})


def parse_game(
    raw: dict[str, Any],
    *,
    player_id: int,
    username_lower: str,
    opening_line_plies: int | None = None,
) -> dict[str, Any] | None:
    """Map one exported game to a ``games`` row, or ``None`` to skip it.

    Skipping is normal, not an error: the export contains variants and aborted
    games that have no place in the report. The caller counts them.
    """
    if raw.get("variant") != STANDARD_VARIANT:
        return None
    if raw.get("status") in UNPLAYED_STATUSES:
        return None

    game_id = raw.get("id")
    created_at = raw.get("createdAt")
    if not isinstance(game_id, str) or not isinstance(created_at, int):
        return None

    side = _find_player(raw, username_lower)
    if side is None:
        return None
    color, me, opponent = side

    plies = settings.opening_line_plies if opening_line_plies is None else opening_line_plies
    moves = (raw.get("moves") or "").split()
    opening = raw.get("opening") or {}
    clock = raw.get("clock") or {}
    analysis = me.get("analysis") or {}

    return {
        "game_id": game_id,
        "player_id": player_id,
        # `createdAt`, not `lastMoveAt`: it is what `since`/`until` filter on
        # and what `sort=dateAsc` orders by, so every other part of the
        # pipeline already agrees this is when the game happened.
        "played_at": epoch_ms_to_datetime(created_at),
        "perf": raw.get("perf") or raw.get("speed") or "unknown",
        "rated": bool(raw.get("rated", False)),
        "color": color,
        "opponent_name": _display_name(opponent),
        "opponent_rating": opponent.get("rating"),
        "player_rating": me.get("rating"),
        "rating_diff": me.get("ratingDiff"),
        "result": _result_for(raw.get("winner"), color),
        "status": raw.get("status") or "unknown",
        "eco": opening.get("eco"),
        "opening_name": opening.get("name"),
        "opening_ply": opening.get("ply"),
        "moves_count": len(moves),
        "clock_initial": clock.get("initial"),
        "clock_increment": clock.get("increment"),
        # Only the games the player asked Lichess to analyse carry these, which
        # is why every quality stat has to be reported with its coverage.
        "analysed": bool(analysis),
        "accuracy": _decimal(analysis.get("accuracy")),
        "acpl": analysis.get("acpl"),
        "inaccuracies": analysis.get("inaccuracy"),
        "mistakes": analysis.get("mistake"),
        "blunders": analysis.get("blunder"),
        # The rest of the move list is discarded: the opening tree is the only
        # thing that needs moves, and a year of bullet is a lot of text.
        "opening_line": moves[:plies],
    }


def _find_player(
    raw: dict[str, Any], username_lower: str
) -> tuple[Color, dict[str, Any], dict[str, Any]] | None:
    """Locate the player we are ingesting for, and their opponent."""
    players = raw.get("players") or {}
    for color in Color:
        side = players.get(color.value) or {}
        user = side.get("user") or {}
        # Lichess ids are the lowercased username and are stable across the
        # capitalisation changes a display name can go through.
        identity = user.get("id") or user.get("name") or ""
        if identity.lower() == username_lower:
            other = Color.BLACK if color is Color.WHITE else Color.WHITE
            return color, side, players.get(other.value) or {}
    return None


def _display_name(side: dict[str, Any]) -> str | None:
    """The opponent's name; ``None`` for anonymous players."""
    user = side.get("user") or {}
    if name := user.get("name"):
        return str(name)
    if (level := side.get("aiLevel")) is not None:
        return f"Stockfish level {level}"
    return None


def _result_for(winner: str | None, color: Color) -> Result:
    """A game with no winner is a draw -- aborted games are filtered earlier."""
    if winner is None:
        return Result.DRAW
    return Result.WIN if winner == color.value else Result.LOSS


def _decimal(value: Any) -> Decimal | None:
    """Accuracy lands in a NUMERIC column, and asyncpg wants a Decimal there."""
    if value is None:
        return None
    return Decimal(str(value))
