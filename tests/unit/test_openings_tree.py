"""The repertoire tree, which is the only section logic that is not SQL."""

from app.report.sections.openings import TreeRow, build_tree


def line(color: str, moves: str, games: int, wins: int, draws: int, losses: int) -> TreeRow:
    return TreeRow(color, moves.split(), games, wins, draws, losses)


def test_a_node_totals_everything_played_through_it() -> None:
    """Counts belong to the whole subtree, not to the games that stopped there."""
    tree = build_tree(
        [
            line("white", "e4 e5 Nf3", 10, 6, 2, 2),
            line("white", "e4 c5", 5, 1, 0, 4),
        ],
        min_games=1,
    )

    e4 = tree[0]
    assert e4["san"] == "e4"
    assert (e4["games"], e4["wins"], e4["draws"], e4["losses"]) == (15, 7, 2, 6)
    assert e4["score"] == 0.5333


def test_shared_prefixes_become_one_node() -> None:
    tree = build_tree(
        [
            line("white", "e4 e5", 4, 4, 0, 0),
            line("white", "e4 c5", 6, 0, 0, 6),
            line("white", "d4 d5", 2, 1, 0, 1),
        ],
        min_games=1,
    )

    assert [node["san"] for node in tree] == ["e4", "d4"], "busiest first"
    assert [node["san"] for node in tree[0]["children"]] == ["c5", "e5"]


def test_rare_lines_are_pruned_at_every_depth() -> None:
    """They are most of the JSON and none of the insight."""
    tree = build_tree(
        [
            line("white", "e4 e5", 8, 4, 0, 4),
            line("white", "e4 c5", 1, 1, 0, 0),
            line("white", "d4", 1, 0, 0, 1),
        ],
        min_games=2,
    )

    assert [node["san"] for node in tree] == ["e4"]
    assert [node["san"] for node in tree[0]["children"]] == ["e5"]


def test_a_pruned_child_still_counts_towards_its_parent() -> None:
    """Dropping the branch must not quietly drop the games that went down it."""
    tree = build_tree(
        [
            line("white", "e4 e5", 8, 4, 0, 4),
            line("white", "e4 c5", 1, 1, 0, 0),
        ],
        min_games=2,
    )

    assert tree[0]["games"] == 9


def test_ties_break_on_the_move_so_the_payload_is_stable() -> None:
    tree = build_tree(
        [line("white", "e4", 3, 3, 0, 0), line("white", "d4", 3, 3, 0, 0)],
        min_games=1,
    )

    assert [node["san"] for node in tree] == ["d4", "e4"]


def test_an_empty_repertoire_is_an_empty_list() -> None:
    assert build_tree([], min_games=1) == []


def test_a_line_shorter_than_the_tree_just_ends() -> None:
    tree = build_tree([line("black", "e4", 5, 0, 0, 5)], min_games=1)

    assert tree[0]["children"] == []
