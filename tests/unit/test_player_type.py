"""The classifier, against players whose type is not in doubt.

Pure: ``classify`` takes signals and returns scores, so the archetypes below are
written as numbers rather than as rows. The SQL that produces those numbers is
covered in the integration suite.
"""

from typing import Any

import pytest

from app.report.player_type_weights import (
    AGGRESSIVE,
    AXES,
    BLENDED_LABEL,
    LABELS,
    MIN_GAMES_FOR_A_LABEL,
    POSITIONAL,
    TACTICAL,
)
from app.report.sections.player_type import (
    NEUTRAL,
    Signals,
    _normalise,
    _weighted_mean,
    classify,
)


def tags(counts: dict[str, int], games: int) -> dict[str, Any]:
    return {"tag_counts": counts, "tagged_games": games}


#: A King's Gambit and Evans Gambit diet: short games, almost no draws, and
#: somebody gets mated.
ATTACKER = Signals(
    games=200,
    **tags({"gambit": 200, "attacking": 200, "open": 200}, 200),
    avg_plies=34.0,
    decisive=196,
    draws=4,
    forced_endings=180,
    flagged=16,
    avg_opening_captures=2.2,
)

#: Ruy Lopez and Caro-Kann, ground out over ninety-odd moves, one game in five
#: shaken hands on.
SQUEEZER = Signals(
    games=200,
    **tags({"positional": 200, "strategic": 200, "closed": 200}, 200),
    avg_plies=96.0,
    decisive=160,
    draws=40,
    forced_endings=110,
    flagged=50,
    avg_opening_captures=0.4,
)

#: Najdorf and Semi-Slav: pieces come off early, nothing is ever quiet, and the
#: result is decisive one way or the other.
BRAWLER = Signals(
    games=200,
    **tags({"sharp": 200, "tactical": 200, "unbalanced": 200}, 200),
    avg_plies=62.0,
    decisive=192,
    draws=8,
    forced_endings=120,
    flagged=72,
    avg_opening_captures=3.6,
)


def test_the_gambiteer_is_an_attacker() -> None:
    assert classify(ATTACKER)["label"] == LABELS[AGGRESSIVE]


def test_the_grinder_is_a_squeezer() -> None:
    assert classify(SQUEEZER)["label"] == LABELS[POSITIONAL]


def test_the_najdorf_player_is_a_brawler() -> None:
    assert classify(BRAWLER)["label"] == LABELS[TACTICAL]


@pytest.mark.parametrize(
    ("player", "axis"),
    [(ATTACKER, AGGRESSIVE), (SQUEEZER, POSITIONAL), (BRAWLER, TACTICAL)],
    ids=["attacker", "squeezer", "brawler"],
)
def test_each_archetype_leads_on_its_own_axis(player: Signals, axis: str) -> None:
    scores = classify(player)["scores"]

    assert scores[axis] == max(scores.values())


@pytest.mark.parametrize(
    "player",
    [ATTACKER, SQUEEZER, BRAWLER, Signals(games=1)],
    ids=["attacker", "squeezer", "brawler", "one-game"],
)
def test_the_scores_always_sum_to_one_hundred(player: Signals) -> None:
    """The frontend draws these as a split bar; 99 or 101 would show."""
    scores = classify(player)["scores"]

    assert sum(scores.values()) == 100
    assert set(scores) == set(AXES)
    assert all(value >= 0 for value in scores.values())


def test_a_player_with_no_games_scores_null_rather_than_an_even_split() -> None:
    """A third each would read as "perfectly balanced", which is a claim.

    Having nothing to read is not the same statement, so it gets no numbers.
    """
    result = classify(Signals())

    assert result["scores"] == dict.fromkeys(AXES)
    assert result["leaning"] is None
    assert result["label"] is None
    assert result["confident"] is False


def test_zeroing_every_weight_also_yields_no_opinion() -> None:
    """The other way to end up with nothing: a config module tuned to silence."""
    assert _weighted_mean([]) is None
    assert _weighted_mean([(0.0, dict(NEUTRAL))]) is None


def test_the_label_is_withheld_until_there_are_enough_games() -> None:
    """Ten gambits is a mood, not a playing style."""
    few = Signals(
        games=MIN_GAMES_FOR_A_LABEL - 1,
        **tags({"gambit": 10, "attacking": 10}, 10),
        avg_plies=30.0,
        decisive=10,
        forced_endings=10,
        avg_opening_captures=3.0,
    )

    result = classify(few)

    assert result["confident"] is False
    assert result["label"] is None
    assert result["scores"][AGGRESSIVE] > 0, "the scores are still computed"


def test_two_axes_within_the_margin_blend_instead_of_picking() -> None:
    """A dead heat should read as an all-rounder, not as a coin toss."""
    balanced = Signals(
        games=200,
        **tags({"sharp": 200}, 200),
        avg_plies=65.0,
        decisive=180,
        draws=20,
        forced_endings=120,
        flagged=60,
        avg_opening_captures=1.5,
    )

    result = classify(balanced)
    top, second = sorted(result["scores"].values(), reverse=True)[:2]

    assert top - second < 8
    assert result["label"] == BLENDED_LABEL


def test_a_repertoire_of_only_descriptive_tags_abstains() -> None:
    """'Offbeat' says something about taste, not about how someone wins."""
    quirky = Signals(
        games=100,
        **tags({"offbeat": 100, "flexible": 100, "hypermodern": 100}, 100),
        avg_plies=96.0,
        decisive=80,
        draws=20,
        forced_endings=55,
        flagged=25,
        avg_opening_captures=0.4,
    )

    # The other signals all point positional, so if the empty opening vote were
    # counted as a third of nothing it would drag the answer to the middle.
    assert classify(quirky)["scores"][POSITIONAL] > 40


def test_the_disclaimer_travels_with_the_verdict() -> None:
    """The brief asks for this to be labelled a heuristic wherever it is shown."""
    result = classify(ATTACKER)

    assert "not a measurement" in result["disclaimer"]


def test_the_raw_signals_are_reported_alongside_the_scores() -> None:
    """So the frontend can say why, rather than just asserting a label."""
    reported = classify(ATTACKER)["signals"]

    assert reported["gambit_share"] == 1.0
    assert reported["avg_plies"] == 34.0
    assert reported["draw_share"] == 0.02


def test_normalising_a_vote_with_no_mass_abstains() -> None:
    """A guard, not a path: every caller checks first. But zeroing a pole in the
    weights module must degrade to "no opinion", never to a ZeroDivisionError."""
    assert _normalise(dict.fromkeys(AXES, 0.0)) == pytest.approx(NEUTRAL)
