"""Finding the position worth replaying, and keeping the right few of them.

Everything here is pure, so the games come from the recorded fixture rather than
from Lichess. The fixture is the point: a handwritten `analysis` array would
agree with whatever this module happens to do, while a real one has judgments
on both sides, an inaccuracy three plies before a blunder, and a game that ends
in a forced mate -- the cases the rules are actually about.
"""

import copy
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import chess
import pytest

from app.core.config import settings
from app.db.models import Color
from app.puzzles import CandidatePool, after_move, find_candidate
from app.puzzles.candidates import SOLVABLE_JUDGMENTS
from app.report.sections.puzzles import _spread
from app.web.templating import board as board_filter
from tests.conftest import load_fixture

#: A game whose blunder walks into a forced mate, and one where both players
#: were flagged. Both are real games, exported with `evals=true`.
MATED = "0M6ofSuX"
BOTH_SIDES = "aJrkZsy8"


@pytest.fixture
def games() -> dict[str, dict[str, Any]]:
    lines = load_fixture("games_sample.ndjson").splitlines()
    return {game["id"]: game for game in (json.loads(line) for line in lines)}


def moment(game: dict[str, Any], color: Color = Color.WHITE) -> Any:
    return find_candidate(game, color=color)


# ------------------------------------------------------------------ finding


def test_a_flagged_move_becomes_the_position_before_it(games: dict[str, Any]) -> None:
    """The whole feature in one assertion: the puzzle is the move not played."""
    found = moment(games[MATED])

    assert found is not None
    assert found.ply == 20
    assert found.move_played == "f3"
    assert found.best_move == "e1g1"
    assert found.best_move_san == "O-O"
    # The position is the one *before* the mistake, with the player to move.
    assert chess.Board(found.fen).turn is chess.WHITE


def test_the_answer_is_legal_in_the_position_it_is_asked_about(
    games: dict[str, Any],
) -> None:
    """The FEN and the answer come from two different places in the export."""
    for game in games.values():
        for color in Color:
            found = moment(game, color)
            if found is None:
                continue
            board = chess.Board(found.fen)
            assert chess.Move.from_uci(found.best_move) in board.legal_moves
            assert board.parse_san(found.move_played) in board.legal_moves


def test_the_move_list_shipped_to_the_browser_is_the_real_one(
    games: dict[str, Any],
) -> None:
    """The board has no rules engine, so this map is the only thing stopping it."""
    found = moment(games[MATED])

    assert found is not None
    board = chess.Board(found.fen)
    expected: dict[str, set[str]] = {}
    for move in board.legal_moves:
        source = chess.square_name(move.from_square)
        expected.setdefault(source, set()).add(chess.square_name(move.to_square))

    assert {key: set(value) for key, value in found.legal_moves.items()} == expected


def test_a_mistake_by_the_opponent_is_not_the_players_puzzle(
    games: dict[str, Any],
) -> None:
    """Both sides were flagged in this game; a report only owns one of them."""
    white = moment(games[BOTH_SIDES], Color.WHITE)
    black = moment(games[BOTH_SIDES], Color.BLACK)

    assert white is not None and black is not None
    assert white.ply % 2 == 0, "White moves on even plies"
    assert black.ply % 2 == 1
    assert white.ply != black.ply


def test_only_the_worst_moment_of_a_game_is_kept(games: dict[str, Any]) -> None:
    """A single collapse carries a dozen judgments and is still one puzzle."""
    game = games[BOTH_SIDES]
    flagged = [
        ply
        for ply in range(1, len(game["analysis"]), 2)
        if (game["analysis"][ply].get("judgment") or {}).get("name") in SOLVABLE_JUDGMENTS
    ]
    found = moment(game, Color.BLACK)

    assert flagged == [25, 37, 57, 65], "the fixture has to have several to choose from"
    # The first one: the three after it are moves played in a game already
    # decided, which is exactly what they should not be chosen for.
    assert found is not None and found.ply == 25


def test_an_inaccuracy_is_never_a_puzzle(games: dict[str, Any]) -> None:
    game = copy.deepcopy(games[MATED])
    for entry in game["analysis"]:
        if "judgment" in entry:
            entry["judgment"]["name"] = "Inaccuracy"

    assert moment(game) is None


def test_a_game_nobody_had_analysed_has_nothing_to_replay(
    games: dict[str, Any],
) -> None:
    game = copy.deepcopy(games[MATED])
    del game["analysis"]

    assert moment(game) is None


def test_a_position_already_lost_is_not_asked_about(games: dict[str, Any]) -> None:
    """Asking for the move that makes this 8% instead of 3% is not a question."""
    game = copy.deepcopy(games[MATED])
    # Bury the player before the flagged move, leaving the judgment in place.
    for entry in game["analysis"]:
        entry.pop("mate", None)
        entry["eval"] = -2000

    assert moment(game) is None


def test_a_move_list_that_does_not_match_the_analysis_loses_only_the_puzzle(
    games: dict[str, Any],
) -> None:
    """The lesson of the status column: a bonus feature does not get to raise."""
    game = copy.deepcopy(games[MATED])
    game["moves"] = "e4 e5 Nf3 Nc6 Bb5 " + game["moves"]

    assert moment(game) is None


def test_the_swing_is_measured_in_points_of_the_game_not_centipawns(
    games: dict[str, Any],
) -> None:
    """Walking into mate from a playable position costs everything left."""
    found = moment(games[MATED])

    assert found is not None
    assert found.win_after == 0
    assert found.swing == found.win_before
    assert 0 < found.swing <= 100


# --------------------------------------------------------------------- pool


def candidate(game_id: str, swing: int) -> Any:
    return SimpleNamespace(game_id=game_id, swing=swing)


def when(month: int, day: int = 1) -> datetime:
    return datetime(2025, month, day, tzinfo=UTC)


def test_the_pool_keeps_the_worst_few_of_each_month() -> None:
    pool = CandidatePool(per_month=2)
    for index, swing in enumerate([10, 40, 20, 30]):
        pool.add(candidate(f"g{index}", swing), when(3, index + 1))

    assert [item.swing for item in pool.candidates()] == [40, 30]


def test_the_pool_does_not_grow_with_the_length_of_the_export() -> None:
    """It is drained from a stream of tens of thousands of games."""
    pool = CandidatePool(per_month=3)
    for index in range(5_000):
        pool.add(candidate(f"g{index}", index % 97), when(index % 12 + 1))

    assert len(pool) == 3 * 12


def test_the_pool_reads_back_in_month_order() -> None:
    pool = CandidatePool(per_month=2)
    pool.add(candidate("late", 90), when(11))
    pool.add(candidate("early", 10), when(2))

    assert [item.game_id for item in pool.candidates()] == ["early", "late"]


# ---------------------------------------------------------------- selecting


def row(month: int, swing: int) -> Any:
    return SimpleNamespace(played_at=when(month), swing=swing)


def test_the_pick_takes_one_month_at_a_time_before_it_takes_two() -> None:
    """Ranking on swing alone answers "your worst evening", six times over."""
    march = [row(3, 90), row(3, 80), row(3, 70)]
    july = [row(7, 40)]
    october = [row(10, 30)]

    picked = _spread(march + july + october, 3)

    assert sorted({item.played_at.month for item in picked}) == [3, 7, 10]


def test_a_heavier_month_still_gives_up_more_than_a_quiet_one() -> None:
    """The spread deals in rounds; it does not cap a month at one."""
    rows = [row(3, 90), row(3, 80), row(3, 70), row(7, 40)]

    picked = _spread(rows, 3)

    assert [item.played_at.month for item in picked] == [3, 7, 3]


def test_the_pick_is_not_padded_when_there_is_less_than_asked_for() -> None:
    assert len(_spread([row(3, 90)], settings.report_puzzles_shown)) == 1
    assert _spread([], settings.report_puzzles_shown) == []


# --------------------------------------------------------------- resolving


def test_a_quiet_move_empties_one_square_and_fills_another() -> None:
    changed = after_move("4k3/8/8/8/8/8/4Q3/4K3 w - - 0 1", "e2e7")

    assert changed == {"e2": None, "e7": "wQ"}


def test_a_capture_names_only_the_squares_that_changed() -> None:
    """The taken piece was already on the square the taker lands on."""
    changed = after_move("4k3/4r3/8/8/8/8/4Q3/4K3 w - - 0 1", "e2e7")

    assert changed == {"e2": None, "e7": "wQ"}


def test_castling_moves_the_rook_too() -> None:
    """The answer is two squares; the board has to change four."""
    changed = after_move("4k3/8/8/8/8/8/8/4K2R w K - 0 1", "e1g1")

    assert changed == {"e1": None, "g1": "wK", "h1": None, "f1": "wR"}


def test_en_passant_empties_a_square_nothing_landed_on() -> None:
    changed = after_move("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1", "e5d6")

    assert changed == {"e5": None, "d6": "wP", "d5": None}


def test_a_promoting_pawn_arrives_as_what_it_promoted_to() -> None:
    changed = after_move("4k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a7a8q")

    assert changed == {"a7": None, "a8": "wQ"}


def test_a_resolution_that_cannot_be_worked_out_is_no_resolution() -> None:
    """A board that will not move is worse than one that does, and far better
    than a report that fails to build."""
    assert after_move("nonsense", "e2e4") == {}
    assert after_move("4k3/8/8/8/8/8/4Q3/4K3 w - - 0 1", "a1a2") == {}


def test_the_board_and_the_resolution_name_pieces_the_same_way(
    games: dict[str, Any],
) -> None:
    """They describe the same squares, so they have to agree on the vocabulary."""
    found = moment(games[MATED])

    assert found is not None
    drawn = {square["name"]: square["piece"] for square in board_filter(found.fen, "white")}
    for name, piece in after_move(found.fen, found.best_move).items():
        assert name in drawn
        assert piece is None or piece in set(drawn.values())
