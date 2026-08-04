"""Parser tests, run entirely against the recorded export.

The fixture is six real bullet games by one GM: three as white, three as black,
five analysed and the last one not. Anything the export can throw at us that
the fixture happens not to contain is built by hand from a fixture game, so the
starting point is always a shape Lichess actually produced.
"""

import copy
import json
from decimal import Decimal
from typing import Any

import pytest

from app.core.config import settings
from app.db.models import Color, Game, Result
from app.ingest import parse_game
from tests.conftest import load_fixture

USERNAME = "zhigalko_sergei"
PLAYER_ID = 7

#: A win as white, analysed, with an opening and a clock -- the ordinary case.
WHITE_WIN = "vCbX5tAb"
#: A win as black, so colour-dependent fields must flip.
BLACK_WIN = "yFp8jwnR"
#: The one game in the fixture the player never had analysed.
UNANALYSED = "JR9Xk3lP"


@pytest.fixture
def games() -> dict[str, dict[str, Any]]:
    raw = [json.loads(line) for line in load_fixture("games_sample.ndjson").splitlines() if line]
    return {game["id"]: game for game in raw}


def parse(raw: dict[str, Any], **kwargs: Any) -> dict[str, Any] | None:
    return parse_game(raw, player_id=PLAYER_ID, username_lower=USERNAME, **kwargs)


def row(raw: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Parse a game that is expected to be kept."""
    parsed = parse(raw, **kwargs)
    assert parsed is not None, "this game should not have been skipped"
    return parsed


# ------------------------------------------------------------------- the row


def test_an_ordinary_game_maps_onto_every_column(games: dict[str, dict[str, Any]]) -> None:
    raw = games[WHITE_WIN]
    parsed = row(raw)

    assert parsed["game_id"] == WHITE_WIN
    assert parsed["player_id"] == PLAYER_ID
    # createdAt is 2018-09-20T13:59:41.883Z.
    assert parsed["played_at"].isoformat() == "2018-09-20T13:59:41.883000+00:00"
    assert parsed["perf"] == "bullet"
    assert parsed["rated"] is True
    assert parsed["color"] is Color.WHITE
    assert parsed["result"] is Result.WIN
    assert parsed["status"] == "resign"

    assert parsed["player_rating"] == 1500
    assert parsed["rating_diff"] == 174
    assert parsed["opponent_name"] == "axmerk"
    assert parsed["opponent_rating"] == 1498

    assert parsed["eco"] == "B22"
    assert parsed["opening_name"] == "Sicilian Defense: Alapin Variation"
    assert parsed["opening_ply"] == 3
    assert parsed["clock_initial"] == 60
    assert parsed["clock_increment"] == 0
    assert parsed["moves_count"] == len(raw["moves"].split())

    assert parsed["analysed"] is True
    assert parsed["accuracy"] == Decimal("96")
    assert parsed["acpl"] == 21
    assert parsed["inaccuracies"] == 1
    assert parsed["mistakes"] == 0
    assert parsed["blunders"] == 0


def test_the_row_is_written_from_the_ingested_players_side(
    games: dict[str, dict[str, Any]],
) -> None:
    """The export is per game; our rows are per player."""
    raw = games[BLACK_WIN]
    parsed = row(raw)

    assert parsed["color"] is Color.BLACK
    assert parsed["result"] is Result.WIN, "black won this one"
    assert parsed["player_rating"] == raw["players"]["black"]["rating"]
    assert parsed["opponent_name"] == raw["players"]["white"]["user"]["name"]
    assert parsed["opponent_rating"] == raw["players"]["white"]["rating"]
    assert parsed["acpl"] == raw["players"]["black"]["analysis"]["acpl"]


def test_the_row_matches_the_games_table_exactly(games: dict[str, dict[str, Any]]) -> None:
    """The batch upsert inserts these keys verbatim.

    A column added to the model without a line in the parser, or a key here
    that no column matches, both fail at INSERT time -- deep inside a worker,
    against a database this test does not need.
    """
    assert set(row(games[WHITE_WIN])) == {column.name for column in Game.__table__.columns}


def test_accuracy_is_a_decimal(games: dict[str, dict[str, Any]]) -> None:
    """The column is NUMERIC, and asyncpg refuses a float for one."""
    assert isinstance(row(games[WHITE_WIN])["accuracy"], Decimal)


def test_only_the_opening_of_the_move_list_survives(games: dict[str, dict[str, Any]]) -> None:
    raw = games[WHITE_WIN]
    moves = raw["moves"].split()
    parsed = row(raw)

    assert len(moves) > settings.opening_line_plies, "fixture must outlast the opening"
    assert parsed["opening_line"] == moves[: settings.opening_line_plies]
    assert parsed["moves_count"] == len(moves), "the count still covers the whole game"


def test_the_opening_length_is_configurable(games: dict[str, dict[str, Any]]) -> None:
    assert row(games[WHITE_WIN], opening_line_plies=4)["opening_line"] == ["e4", "c5", "c3", "d6"]


# ------------------------------------------------------------ sparse analysis


def test_an_unanalysed_game_is_kept_with_empty_quality_fields(
    games: dict[str, dict[str, Any]],
) -> None:
    """Most games are never analysed. Dropping them would lose the whole year."""
    parsed = row(games[UNANALYSED])

    assert parsed["analysed"] is False
    assert parsed["accuracy"] is None
    assert parsed["acpl"] is None
    assert parsed["inaccuracies"] is None
    assert parsed["mistakes"] is None
    assert parsed["blunders"] is None
    # Everything that needs no engine is still there.
    assert parsed["result"] is Result.WIN
    assert parsed["eco"] == "B12"


def test_analysis_by_the_opponent_alone_does_not_count(
    games: dict[str, dict[str, Any]],
) -> None:
    """`analysed` is about our player's numbers, not the game's."""
    raw = copy.deepcopy(games[WHITE_WIN])
    del raw["players"]["white"]["analysis"]

    parsed = row(raw)
    assert raw["players"]["black"]["analysis"], "the opponent still has theirs"
    assert parsed["analysed"] is False
    assert parsed["acpl"] is None


# ----------------------------------------------------------------- results


def test_a_game_with_no_winner_is_a_draw(games: dict[str, dict[str, Any]]) -> None:
    raw = copy.deepcopy(games[WHITE_WIN])
    del raw["winner"]
    raw["status"] = "draw"

    assert row(raw)["result"] is Result.DRAW


def test_the_opponent_winning_is_a_loss(games: dict[str, dict[str, Any]]) -> None:
    raw = copy.deepcopy(games[WHITE_WIN])
    raw["winner"] = "black"

    assert row(raw)["result"] is Result.LOSS


# ------------------------------------------------------------------ skipping


@pytest.mark.parametrize("status", ["aborted", "noStart", "started", "created"])
def test_games_in_which_nobody_moved_are_skipped(
    games: dict[str, dict[str, Any]], status: str
) -> None:
    raw = copy.deepcopy(games[WHITE_WIN])
    raw["status"] = status
    raw.pop("winner", None)

    assert parse(raw) is None


def test_a_forfeit_is_skipped_even_though_it_has_a_winner(
    games: dict[str, dict[str, Any]],
) -> None:
    """Shape taken from a live arena no-show: a winner, a rating change, no moves.

    Filtering on the missing winner alone would let these through and credit
    the player with games they never played.
    """
    raw = copy.deepcopy(games[WHITE_WIN])
    raw["status"] = "noStart"
    raw["winner"] = "white"
    raw["moves"] = ""

    assert parse(raw) is None


@pytest.mark.parametrize("variant", ["chess960", "atomic", "crazyhouse"])
def test_variants_are_skipped(games: dict[str, dict[str, Any]], variant: str) -> None:
    """They carry no ECO and would land in the opening tree as blanks."""
    raw = copy.deepcopy(games[WHITE_WIN])
    raw["variant"] = variant

    assert parse(raw) is None


def test_a_game_the_player_did_not_play_is_skipped(games: dict[str, dict[str, Any]]) -> None:
    assert parse_game(games[WHITE_WIN], player_id=PLAYER_ID, username_lower="someone_else") is None


@pytest.mark.parametrize("missing", ["id", "createdAt"])
def test_a_game_missing_its_identity_is_skipped(
    games: dict[str, dict[str, Any]], missing: str
) -> None:
    """Without an id it cannot be upserted; without a date it cannot be placed."""
    raw = copy.deepcopy(games[WHITE_WIN])
    del raw[missing]

    assert parse(raw) is None


# ------------------------------------------------------------ absent fields


def test_a_bare_game_still_parses(games: dict[str, dict[str, Any]]) -> None:
    """Opening, clock and moves are all optional in the export."""
    raw = copy.deepcopy(games[WHITE_WIN])
    for key in ("opening", "clock", "moves"):
        del raw[key]

    parsed = row(raw)
    assert parsed["eco"] is None
    assert parsed["opening_name"] is None
    assert parsed["opening_ply"] is None
    assert parsed["clock_initial"] is None
    assert parsed["clock_increment"] is None
    assert parsed["opening_line"] == []
    assert parsed["moves_count"] == 0


def test_an_anonymous_opponent_has_no_name(games: dict[str, dict[str, Any]]) -> None:
    raw = copy.deepcopy(games[WHITE_WIN])
    del raw["players"]["black"]["user"]

    parsed = row(raw)
    assert parsed["opponent_name"] is None
    assert parsed["opponent_rating"] == 1498, "the rating is still reported"


def test_an_engine_opponent_is_named_by_level(games: dict[str, dict[str, Any]]) -> None:
    raw = copy.deepcopy(games[WHITE_WIN])
    del raw["players"]["black"]["user"]
    raw["players"]["black"]["aiLevel"] = 5

    assert row(raw)["opponent_name"] == "Stockfish level 5"


def test_the_player_is_matched_case_insensitively(games: dict[str, dict[str, Any]]) -> None:
    """Lichess ids are lowercase, but display names carry their own casing."""
    raw = copy.deepcopy(games[WHITE_WIN])
    del raw["players"]["white"]["user"]["id"]

    assert row(raw)["color"] is Color.WHITE
