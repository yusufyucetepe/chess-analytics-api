"""One exported game in, at most one puzzle-worthy position out.

Pure functions over the raw NDJSON game: no database, no network. The only
input beyond the game is the player's colour, because a mistake is only a
puzzle when it was *theirs*.

What Lichess gives us, and how it is read here. With ``evals=true`` the export
carries a top-level ``analysis`` array with one entry per ply, where entry *i*
describes the move at index *i* of the move list:

- ``eval`` (centipawns) or ``mate`` (moves to mate) is the engine's verdict on
  the position **after** that move, always from White's point of view.
- ``best`` (UCI) and ``variation`` (SAN) appear only when the move was flagged,
  and describe what should have been played *instead*.
- ``judgment`` names the flag and carries Lichess's own sentence about it.

So the position to solve is the one *before* move *i*, the answer is ``best``,
and the evaluation the player threw away is the difference between entry
*i - 1* and entry *i*.
"""

import logging
import math
from dataclasses import dataclass, field
from typing import Any

import chess

from app.core.config import settings
from app.db.models import Color

logger = logging.getLogger(__name__)

#: Judgments worth replaying. Inaccuracies are excluded on purpose: Lichess
#: flags them freely in the opening, and "you played a slightly less good move"
#: is not a puzzle anybody wants to be shown.
SOLVABLE_JUDGMENTS = frozenset({"Mistake", "Blunder"})

#: Centipawn stand-in for a forced mate, so one scale orders everything. The
#: exact number barely matters -- the win-percentage curve below flattens
#: anything this large to 0 or 100.
MATE_CP = 10_000

#: The logistic that turns centipawns into a share of the point, as Lichess
#: itself does. It is what makes the swing threshold meaningful: +900 to +500
#: is a rounding error at this end of the curve, while +100 to -300 loses half
#: the game, and raw centipawns would rank those the other way round.
WIN_CURVE = -0.00368208


@dataclass(frozen=True, slots=True)
class Candidate:
    """A position the player should have played differently.

    ``fen`` is the position with the player to move, and ``best_move`` is the
    single move that answers it. Everything else is either how the board is
    drawn or how the moment is described.
    """

    game_id: str
    ply: int
    fen: str
    #: What was played, in SAN, for "you played Nxe5" after the attempt.
    move_played: str
    #: The answer, in UCI, because squares are what the board compares against.
    best_move: str
    best_move_san: str
    #: The engine's line after ``best_move``, in SAN, excluding it.
    continuation: list[str]
    judgment: str
    #: Lichess's own sentence, e.g. "Blunder. Bd8 was best."
    comment: str
    #: Share of the point held before and after the move, 0-100, player's view.
    win_before: int
    win_after: int
    #: ``win_before - win_after``: what the move cost, in points of the game.
    swing: int
    #: ``{from_square: [to_square, ...]}`` for every legal move. Precomputed so
    #: the browser can refuse an illegal drag without shipping a rules engine.
    legal_moves: dict[str, list[str]] = field(default_factory=dict)


def find_candidate(raw: dict[str, Any], *, color: Color) -> Candidate | None:
    """The single worst moment in one game, or ``None`` if it has none.

    One per game rather than all of them: a single collapse can carry a dozen
    judgments, and a report made of six views of the same disaster is a worse
    read than six games.

    Never raises. A game whose move list and analysis array disagree, or whose
    SAN this build of python-chess declines, loses its puzzle and nothing else
    -- the year's games are the product and a bonus feature does not get to
    take them down.
    """
    analysis = raw.get("analysis")
    moves = (raw.get("moves") or "").split()
    if not isinstance(analysis, list) or not moves:
        return None

    best = _best_moment(analysis, moves, color)
    if best is None:
        return None

    try:
        return _materialise(raw, moves, analysis, best, color)
    except (ValueError, IndexError, AssertionError):
        logger.warning("no puzzle from game %s ply %d", raw.get("id"), best, exc_info=True)
        return None


def _best_moment(analysis: list[Any], moves: list[str], color: Color) -> int | None:
    """Index of the flagged move that cost the player the most, by swing."""
    # The player moves on even plies as White, odd as Black.
    parity = 0 if color is Color.WHITE else 1
    winner: int | None = None
    best_swing = settings.puzzle_min_swing - 1

    for ply in range(parity, min(len(analysis), len(moves)), 2):
        entry = analysis[ply]
        if not isinstance(entry, dict):
            continue
        judgment = (entry.get("judgment") or {}).get("name")
        if judgment not in SOLVABLE_JUDGMENTS or not entry.get("best"):
            continue
        if ply < settings.puzzle_min_ply:
            continue

        before = _win_percent(_centipawns(analysis[ply - 1] if ply else None), color)
        after = _win_percent(_centipawns(entry), color)
        # A position already lost has no puzzle in it: "find the move that
        # makes this 8% instead of 3%" is not a question worth asking.
        if before < settings.puzzle_min_win_before:
            continue

        swing = before - after
        if swing > best_swing:
            winner, best_swing = ply, swing

    return winner


def _materialise(
    raw: dict[str, Any],
    moves: list[str],
    analysis: list[Any],
    ply: int,
    color: Color,
) -> Candidate | None:
    """Replay the game up to ``ply`` and describe the position there."""
    board = chess.Board()
    for san in moves[:ply]:
        board.push_san(san)

    entry = analysis[ply]
    played = board.parse_san(moves[ply])
    best = chess.Move.from_uci(entry["best"])
    if best not in board.legal_moves:
        # Only reachable if the analysis array and the move list describe
        # different games, which would make every other field a lie too.
        return None

    before = _win_percent(_centipawns(analysis[ply - 1] if ply else None), color)
    after = _win_percent(_centipawns(entry), color)
    variation = (entry.get("variation") or "").split()

    return Candidate(
        game_id=str(raw["id"]),
        ply=ply,
        fen=board.fen(),
        move_played=board.san(played),
        best_move=best.uci(),
        best_move_san=board.san(best),
        continuation=variation[1:],
        judgment=(entry.get("judgment") or {}).get("name") or "Mistake",
        comment=(entry.get("judgment") or {}).get("comment") or "",
        win_before=before,
        win_after=after,
        swing=before - after,
        legal_moves=_legal_moves(board),
    )


def piece_code(symbol: str) -> str:
    """A FEN letter as the name the browser knows a piece by: ``K`` -> ``wK``.

    One definition, used by the board filter that draws a position and by the
    resolution below that changes one. The two have to agree exactly -- the
    second names squares the first already drew -- and agreeing by coincidence
    is how a piece quietly turns invisible.
    """
    return f"{'w' if symbol.isupper() else 'b'}{symbol.upper()}"


def after_move(fen: str, uci: str) -> dict[str, str | None]:
    """Which squares change when ``uci`` is played in ``fen``, and to what.

    ``{"e1": None, "g1": "wK", "h1": None, "f1": "wR"}`` for a castle: the
    squares emptied map to ``None`` and the squares filled map to a piece name,
    so the browser can play the move by setting four elements without knowing
    that castling moves two pieces, that en passant takes a pawn off a square
    nothing landed on, or that a pawn reaching the eighth rank stops being one.

    Derived rather than stored. It is a function of two columns already in the
    table, so a column of its own would be a third copy of the same fact -- and
    deriving it means every puzzle mined before this existed can play its move
    too, instead of only the ones from the next export.

    Never raises: a puzzle whose board does not move is worse than one that
    does, and far better than a report that fails to build.
    """
    try:
        board = chess.Board(fen)
        move = chess.Move.from_uci(uci)
        before = board.piece_map()
        board.push(move)
        after = board.piece_map()
    except (ValueError, AssertionError):
        logger.warning("could not resolve %s in %s", uci, fen, exc_info=True)
        return {}

    changed: dict[str, str | None] = {}
    for square in before.keys() | after.keys():
        was, now = before.get(square), after.get(square)
        if was != now:
            changed[chess.square_name(square)] = None if now is None else piece_code(now.symbol())
    return changed


def _legal_moves(board: chess.Board) -> dict[str, list[str]]:
    """Every legal move as ``{from: [to, ...]}``, in square names.

    Promotions collapse into the destination square they share: the board
    always promotes to a queen, which is the best move in all but a handful of
    positions in history and none of them worth a piece-picker dialog.
    """
    grouped: dict[str, list[str]] = {}
    for move in board.legal_moves:
        source = chess.square_name(move.from_square)
        target = chess.square_name(move.to_square)
        squares = grouped.setdefault(source, [])
        if target not in squares:
            squares.append(target)
    return grouped


def _centipawns(entry: Any) -> int:
    """One evaluation as centipawns from White's point of view.

    ``None`` is the starting position, which is a dead level 0 by definition.
    """
    if not isinstance(entry, dict):
        return 0
    if (mate := entry.get("mate")) is not None:
        # Sign carries who is mating; distance only breaks ties between two
        # forced mates, which the win curve then flattens to nothing anyway.
        return MATE_CP - abs(int(mate)) * 100 if mate > 0 else -MATE_CP + abs(int(mate)) * 100
    value = entry.get("eval")
    return int(value) if isinstance(value, int | float) else 0


def _win_percent(centipawns: int, color: Color) -> int:
    """Centipawns to the player's share of the point, 0-100."""
    if color is Color.BLACK:
        centipawns = -centipawns
    chances = 2 / (1 + math.exp(WIN_CURVE * centipawns)) - 1
    return round(50 + 50 * chances)
