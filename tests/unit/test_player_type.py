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
    BLENDED_NOUN,
    CENTRIST_SHARE,
    FLAVOUR_TAGS,
    MIN_GAMES_FOR_A_LABEL,
    NAME_OVERRIDES,
    PLAIN_LABELS,
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
from app.report.style_reference import Reference, summarise


def tags(counts: dict[str, int], games: int) -> dict[str, Any]:
    """A repertoire as both of the shapes the classifier reads it in.

    ``tag_counts`` counts games and feeds the shares; ``tag_mass`` spreads one
    unit per game across its tags and feeds the vote. Keeping them in one helper
    is what stops a fixture stating a repertoire the pipeline could not produce.
    """
    total = sum(counts.values()) or 1
    return {
        "tag_counts": counts,
        "tagged_games": games,
        "tag_mass": {tag: count * games / total for tag, count in counts.items()},
    }


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

#: Ruy Lopez and English, ground out over ninety-odd moves. Draws at a rate
#: that is high for online blitz but well short of deliberate.
SQUEEZER = Signals(
    games=200,
    **tags({"positional": 200, "strategic": 200, "closed": 200}, 200),
    avg_plies=96.0,
    decisive=180,
    draws=20,
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


def test_the_gambiteer_is_a_gambit_specialist() -> None:
    """Aggressive axis plus a gambit repertoire, which has its own name."""
    assert classify(ATTACKER)["label"] == "Gambit Specialist"


def test_the_grinder_is_a_positional_grinder() -> None:
    """Closed but not solid: Ruy Lopez and English, not Caro-Kann."""
    assert classify(SQUEEZER)["label"] == "Positional Grinder"


def test_the_najdorf_player_is_a_chaos_merchant() -> None:
    assert classify(BRAWLER)["label"] == "Chaos Merchant"


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


MIDDLING = Signals(
    games=200,
    **tags({"sharp": 200}, 200),
    avg_plies=65.0,
    decisive=180,
    draws=20,
    forced_endings=120,
    flagged=60,
    avg_opening_captures=1.5,
)


def population(around: dict[str, int], spread: int = 12) -> Reference:
    """A band of players scattered around a centre, as the reference sees them.

    Written out rather than mocked because the whole point of the rule is that
    the cut is a share of a real distribution: a Reference with made-up
    percentiles could be made to say anything.
    """
    samples = []
    for step in range(60):
        drift = (step % 5 - 2) * spread // 2
        samples.append(
            {
                POSITIONAL: around[POSITIONAL] + drift,
                AGGRESSIVE: around[AGGRESSIVE] - drift,
                TACTICAL: around[TACTICAL],
            }
        )
    return summarise("blitz", 1400, samples)


def test_only_the_middle_of_a_population_is_an_all_rounder() -> None:
    """The label is a claim about where somebody sits among players like them,
    so it needs a population to sit in the middle of. The old rule was a margin
    between the top two axes, which the three scores summing to 100 made the
    default outcome rather than the exception."""
    centred = classify(MIDDLING)["scores"]

    result = classify(MIDDLING, population(centred))

    assert result["centre"]["centrist"] is True
    assert result["label"].endswith(BLENDED_NOUN)
    assert result["label"] == "Sharp All-Rounder", "the repertoire still names them"


def test_a_player_away_from_the_centre_gets_their_leading_axis() -> None:
    """Same scores, a population they are nowhere near."""
    elsewhere = {POSITIONAL: 20, AGGRESSIVE: 65, TACTICAL: 15}

    result = classify(MIDDLING, population(elsewhere))

    assert result["centre"]["centrist"] is False
    assert not result["label"].endswith(BLENDED_NOUN)
    assert result["leaning"] in AXES


def test_without_a_population_nobody_is_called_average() -> None:
    """A band we cannot see is not evidence that somebody is in the middle of
    it. The leading axis stands instead, which is the honest fallback."""
    result = classify(MIDDLING)

    assert result["centre"] is None
    assert not result["label"].endswith(BLENDED_NOUN)


def test_at_most_a_sixth_of_a_band_can_be_an_all_rounder() -> None:
    """The rule the label exists to enforce. Every player in a synthetic band is
    classified against that band's own reference; All-Rounder is a percentile
    cut, so its share is bounded by construction rather than by luck."""
    band = [
        Signals(
            games=200,
            **tags({"sharp": sharp, "closed": 100 - sharp}, 200),
            avg_plies=40.0 + sharp * 0.6,
            decisive=190,
            draws=10,
            forced_endings=190 - sharp,
            flagged=sharp // 2,
            avg_opening_captures=sharp / 40,
        )
        for sharp in range(1, 100)
    ]
    scores = [classify(signals)["scores"] for signals in band]
    reference = summarise("blitz", 1400, scores)

    labels = [classify(signals, reference)["label"] for signals in band]
    centrists = [label for label in labels if label.endswith(BLENDED_NOUN)]

    assert len(centrists) / len(labels) <= CENTRIST_SHARE + 0.02
    assert len(set(labels)) > 1, "a band that all reads the same is not a classifier"


def test_a_repertoire_of_only_descriptive_tags_abstains() -> None:
    """'Offbeat' says something about taste, not about how someone wins."""
    quirky = Signals(
        games=100,
        **tags({"offbeat": 100, "flexible": 100, "hypermodern": 100}, 100),
        avg_plies=96.0,
        decisive=90,
        draws=10,
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


def test_the_signature_tag_is_the_most_over_represented_not_the_most_played() -> None:
    """`positional` sits on 47% of the ECO table, so playing it half the time is
    average. `gambit` sits on 5%, so the same share is a personality."""
    both = Signals(
        games=200,
        **tags({"positional": 180, "closed": 150, "gambit": 60}, 200),
        avg_plies=40.0,
        decisive=190,
        draws=10,
        forced_endings=150,
        avg_opening_captures=2.5,
    )

    signature = classify(both)["signature"]

    assert signature["tag"] == "gambit"
    assert signature["share"] == pytest.approx(0.3, abs=0.01)


def test_the_signature_publishes_no_multiplier() -> None:
    """The lift ranks the tags and stops there. Published, it would read as a
    comparison against other players -- and the only denominator we have is the
    share of ECO codes carrying the tag, which is a fact about the catalogue."""
    signals = Signals(
        games=200,
        **tags({"positional": 180, "closed": 150, "gambit": 60}, 200),
        avg_plies=40.0,
        decisive=190,
        draws=10,
        forced_endings=150,
        avg_opening_captures=2.5,
    )

    assert set(classify(signals)["signature"]) == {"tag", "adjective", "share"}


def test_a_tag_below_the_share_floor_cannot_name_anyone() -> None:
    """Three freak gambits in two hundred games is not a signature."""
    occasional = Signals(
        games=200,
        **tags({"closed": 190, "gambit": 3}, 200),
        avg_plies=95.0,
        decisive=160,
        draws=40,
        forced_endings=110,
        avg_opening_captures=0.4,
    )

    assert classify(occasional)["signature"]["tag"] == "closed"


def test_a_repertoire_with_nothing_distinctive_drops_the_adjective() -> None:
    """An entirely average repertoire gets the bare axis label, not a made-up one."""
    average = Signals(
        games=200,
        **tags({"positional": 200}, 200),
        avg_plies=96.0,
        decisive=180,
        draws=20,
        forced_endings=110,
        avg_opening_captures=0.4,
    )

    result = classify(average)

    assert result["signature"] is None
    assert result["label"] == PLAIN_LABELS[POSITIONAL] == "The Strategist"


def test_axis_nouns_never_repeat_the_adjective() -> None:
    """'Tactical Tactician' is not a personality, so those tags cannot be flavours."""
    assert set(FLAVOUR_TAGS) & {"positional", "strategic", "tactical", "attacking"} == set()


def test_every_override_names_a_real_tag_and_axis() -> None:
    """A typo here would silently never fire."""
    for tag, axis in NAME_OVERRIDES:
        assert tag in FLAVOUR_TAGS
        assert axis in AXES or axis is None


def test_a_caro_kann_style_repertoire_reads_solid_not_closed() -> None:
    """B10-B19 carries solid, closed and positional; solid is rarer, so it wins."""
    caro = Signals(
        games=200,
        **tags({"solid": 200, "closed": 200, "positional": 200}, 200),
        avg_plies=98.0,
        decisive=170,
        draws=30,
        forced_endings=115,
        avg_opening_captures=0.4,
    )

    result = classify(caro)

    assert result["signature"]["tag"] == "solid"
    assert result["label"] == "The Immovable Object"
