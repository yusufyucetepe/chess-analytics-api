"""Systems, concentration and the drill-down -- the repertoire card's arithmetic.

Everything here is a pure fold over pre-aggregated lines, so the inputs are
written by hand: what these tests are about is which line a player is told they
are losing to, and that has to be legible in the test as well as on the page.
"""

from typing import Any

from app.report.sections.openings import LineRow, build_systems, concentration

# A Vienna that is forced for three plies and then leaves Black a choice: the
# shape the drill-down exists for. Shares of the whole are what the bar shows.
VIENNA = [
    LineRow("white", "Vienna Game", ["e4", "e5", "Nc3", "Nf6", "f4"], 40, 26, 0, 14),
    LineRow("white", "Vienna Game", ["e4", "e5", "Nc3", "Nf6", "Bc4"], 10, 8, 0, 2),
    LineRow("white", "Vienna Game", ["e4", "e5", "Nc3", "Bc5", "Bc4"], 20, 4, 2, 14),
]
SICILIAN = [
    LineRow("black", "Sicilian Defense", ["e4", "c5", "Nf3", "d6"], 30, 15, 0, 15),
]


def systems(rows: list[LineRow], **overrides: Any) -> list[dict[str, Any]]:
    """The production settings, so a test only states what it is about."""
    kwargs: dict[str, Any] = {
        "shown": 6,
        "min_games": 8,
        "branch_plies": 2,
        "branches_shown": 5,
        "tree_min_games": 2,
        "dominance": 0.95,
    }
    return build_systems(rows, **{**kwargs, **overrides})


def named(rows: list[dict[str, Any]]) -> list[str]:
    return [row["name"] for row in rows]


# ------------------------------------------------------------------- systems


def test_a_family_becomes_one_system_with_its_totals() -> None:
    vienna = systems(VIENNA)[0]

    assert vienna["name"] == "Vienna Game"
    assert vienna["color"] == "white"
    assert (vienna["games"], vienna["wins"], vienna["draws"], vienna["losses"]) == (70, 38, 2, 30)
    assert vienna["score"] == 0.5571
    # 30 losses and a draw: the points the system dropped over the year.
    assert vienna["points_lost"] == 31.0


def test_the_same_family_on_both_sides_is_two_systems() -> None:
    """Answering the Sicilian and choosing it are two habits, one name."""
    both = systems(
        [
            *SICILIAN,
            LineRow("white", "Sicilian Defense", ["e4", "c5", "c3"], 12, 6, 0, 6),
        ]
    )

    assert [(row["name"], row["color"]) for row in both] == [
        ("Sicilian Defense", "black"),
        ("Sicilian Defense", "white"),
    ]


def test_the_busiest_system_comes_first() -> None:
    assert named(systems([*SICILIAN, *VIENNA])) == ["Vienna Game", "Sicilian Defense"]


def test_only_the_cards_that_fit_are_returned() -> None:
    assert len(systems([*SICILIAN, *VIENNA], shown=1)) == 1


def test_a_family_played_a_handful_of_times_is_not_a_system() -> None:
    """It is an experiment. Its games are not lost -- `concentration` holds them."""
    assert named(systems([*VIENNA, LineRow("white", "Bird Opening", ["f4"], 3, 1, 0, 2)])) == [
        "Vienna Game"
    ]


def test_a_line_lichess_could_not_name_is_no_system() -> None:
    assert systems([LineRow("white", "", ["a3", "b6"], 30, 15, 0, 15)]) == []


def test_ties_break_on_the_name_so_the_payload_is_stable() -> None:
    a, b = (
        LineRow("white", "Zukertort Opening", ["Nf3"], 10, 5, 0, 5),
        LineRow("white", "Bird Opening", ["f4"], 10, 5, 0, 5),
    )
    assert named(systems([a, b])) == ["Bird Opening", "Zukertort Opening"]


# ------------------------------------------------------------------ mainline


def test_the_mainline_stops_where_the_opponent_first_chooses() -> None:
    """1.e4 e5 2.Nc3 is every game in the system; Nf6 or Bc5 is the player's news."""
    vienna = systems(VIENNA)[0]

    assert vienna["mainline"] == ["e4", "e5", "Nc3"]
    assert vienna["branch_ply"] == 4, "the branches start on Black's second move"


def test_a_system_that_never_branches_is_all_mainline() -> None:
    only = systems(SICILIAN)[0]

    assert only["mainline"] == ["e4", "c5", "Nf3", "d6"]
    assert only["branches"] == []


def test_two_transposed_games_do_not_end_the_mainline() -> None:
    """The shape that made this rule necessary: 249 Viennas by the move order
    everyone plays, two that arrived another way. Stopping there leaves a
    mainline of "1.e4" and a drill-down whose only row is the whole system."""
    vienna = systems(
        [
            LineRow("white", "Vienna Game", ["e4", "e5", "Nc3", "Nf6"], 249, 150, 0, 99),
            LineRow("white", "Vienna Game", ["e4", "Nf6", "Nc3", "e5"], 2, 1, 0, 1),
        ]
    )[0]

    assert vienna["mainline"] == ["e4", "e5", "Nc3", "Nf6"]
    assert vienna["games"] == 251, "the transposed games are still in the totals"


def test_a_genuine_sideline_still_ends_the_mainline() -> None:
    """One game in eight is a choice the player makes, not a transposition."""
    split = systems(
        [
            LineRow("white", "Vienna Game", ["e4", "e5", "Nc3"], 70, 40, 0, 30),
            LineRow("white", "Vienna Game", ["e4", "e5", "Bc4"], 10, 5, 0, 5),
        ]
    )[0]

    assert split["mainline"] == ["e4", "e5"]
    assert [branch["sans"] for branch in split["branches"]] == [["Nc3"], ["Bc4"]]


def test_a_family_reached_two_ways_has_no_shared_mainline() -> None:
    split = systems(
        [
            LineRow("white", "Queen's Pawn Game", ["d4", "d5", "Nf3"], 20, 10, 0, 10),
            LineRow("white", "Queen's Pawn Game", ["Nf3", "d5", "d4"], 20, 10, 0, 10),
        ]
    )[0]

    assert split["mainline"] == []
    assert split["branch_ply"] == 1


# ------------------------------------------------------------------ branches


def test_the_branches_read_best_score_to_worst() -> None:
    """So the list ends on the line going badly, which is the one to read."""
    branches = systems(VIENNA)[0]["branches"]

    assert [branch["sans"] for branch in branches] == [
        ["Nf6", "Bc4"],
        ["Nf6", "f4"],
        ["Bc5", "Bc4"],
    ]
    assert [branch["score"] for branch in branches] == [0.8, 0.65, 0.25]


def test_points_lost_cannot_be_the_order() -> None:
    """It is dominated by volume: the 40-game line shed 14 points scoring 65%,
    the 20-game one shed 15 scoring 25%. Ranking on it puts them a place apart
    and calls the healthy line the worst thing in the system."""
    branches = systems(VIENNA)[0]["branches"]

    assert [branch["points_lost"] for branch in branches] == [2.0, 14.0, 15.0]
    assert max(branches, key=lambda b: b["points_lost"])["score"] == 0.25


def test_a_branch_carries_the_ply_it_starts_on() -> None:
    """Without it the page cannot number the moves, and "Nxe4" is not a move."""
    for branch in systems(VIENNA)[0]["branches"]:
        assert branch["ply"] == 4


def test_only_the_deepest_node_on_a_path_is_reported() -> None:
    """A parent's counts include its children's, so listing both would charge the
    same losses twice and rank a line above its own prefix."""
    branches = systems(VIENNA)[0]["branches"]

    assert ["Nf6"] not in [branch["sans"] for branch in branches]
    assert sum(branch["points_lost"] for branch in branches) == 31.0


def test_a_path_that_ends_early_is_still_a_branch() -> None:
    """Shorter than `branch_plies` because the games ended, not because it was cut."""
    branches = systems(
        [
            LineRow("white", "Vienna Game", ["e4", "e5", "Nc3", "Nf6"], 20, 10, 0, 10),
            LineRow("white", "Vienna Game", ["e4", "e5", "Nc3", "Bc5", "Bc4"], 20, 5, 0, 15),
        ],
        branch_plies=2,
    )[0]["branches"]

    assert [branch["sans"] for branch in branches] == [["Nf6"], ["Bc5", "Bc4"]]


def test_the_cap_keeps_the_lines_the_player_actually_meets() -> None:
    """Cutting by score instead would keep the five best and drop exactly the
    lines the drill-down exists to show."""
    kept = systems(VIENNA, branches_shown=1)[0]["branches"]

    assert [branch["sans"] for branch in kept] == [["Nf6", "f4"]], "the busiest, not the best"


# ------------------------------------------------------------- concentration


def test_the_bar_ends_with_everything_the_systems_do_not_cover() -> None:
    bar = concentration(systems([*VIENNA, *SICILIAN]), 200, target=0.6)

    assert [segment["name"] for segment in bar["segments"]] == [
        "Vienna Game",
        "Sicilian Defense",
        None,
    ]
    assert bar["segments"][-1]["games"] == 100, "200 games, 100 of them in a system"
    assert sum(segment["games"] for segment in bar["segments"]) == 200


def test_a_repertoire_that_covers_everything_has_no_leftover_segment() -> None:
    bar = concentration(systems(VIENNA), 70, target=0.6)

    assert [segment["name"] for segment in bar["segments"]] == ["Vienna Game"]


def test_the_headline_is_the_fewest_systems_that_reach_the_target() -> None:
    bar = concentration(systems([*VIENNA, *SICILIAN]), 100, target=0.6)

    assert bar["covering"] == {"systems": 1, "share": 0.7, "reached": True}


def test_a_scattered_repertoire_reports_how_far_it_got() -> None:
    """Not a missing value: never reaching the target is the finding."""
    bar = concentration(systems([*VIENNA, *SICILIAN]), 1000, target=0.6)

    assert bar["covering"] == {"systems": 2, "share": 0.1, "reached": False}


def test_the_shares_are_of_every_game_not_of_the_named_ones() -> None:
    """Games that never reached a named opening still sit in the denominator."""
    bar = concentration(systems(VIENNA), 140, target=0.6)

    assert bar["segments"][0]["share"] == 0.5


def test_a_year_with_no_games_divides_by_nothing() -> None:
    bar = concentration([], 0, target=0.6)

    assert bar["segments"] == []
    assert bar["covering"] == {"systems": 0, "share": 0.0, "reached": False}
