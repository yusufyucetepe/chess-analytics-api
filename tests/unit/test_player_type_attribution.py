"""What the classifier is allowed to read off a game's opening.

Two rules, and both are about not putting words in a player's mouth: a game is
worth one vote however many tags its ECO code happens to carry, and an opening
only speaks for the player who chose it. The fixtures are real ECO codes and
real Lichess names, because the rules turn on what those actually say.
"""

from app.db.models import Color
from app.report.families import eco_tags, family, family_tags, owner
from app.report.player_type_weights import AGGRESSIVE, BLENDED_NOUN
from app.report.sections.player_type import OpeningRow, Signals, classify, read_openings

#: 1.e4 e5 2.Nc3 -- White's third move settles it, so ply 3.
VIENNA = "C25"
#: 1.e4 c5 -- Black's move, ply 2.
SICILIAN = "B20"
#: 1.e4 c5 2.d4 -- White's move inside Black's opening, ply 3.
SMITH_MORRA = "B21"
#: 1.e4 c5 2.Bc4 -- likewise White's, ply 3.
BOWDLER = "B20"


def row(eco: str, name: str, color: Color, ply: int, games: int) -> OpeningRow:
    return OpeningRow(eco=eco, family=family(name), color=color, ply=ply, games=games)


# --------------------------------------------------------------- one game, one vote


def test_a_game_is_worth_one_vote_however_many_tags_it_carries() -> None:
    """C25 carries three tags and D02 carries two. Ten games of each are ten
    games of each, not thirty against twenty."""
    read = read_openings(
        [
            row(VIENNA, "Vienna Game", Color.WHITE, 3, 10),
            row("D02", "Queen's Pawn Game", Color.WHITE, 3, 10),
        ]
    )

    assert sum(read.mass.values()) == 20
    assert read.tagged_games == 20


def test_the_busiest_opening_carries_the_most_weight() -> None:
    """The point of weighting by games: a 92-game system is not one opinion."""
    read = read_openings(
        [
            row(VIENNA, "Vienna Game", Color.WHITE, 3, 92),
            row("D02", "Queen's Pawn Game", Color.WHITE, 3, 5),
        ]
    )

    assert read.mass["attacking"] > read.mass["positional"] * 10


# ------------------------------------------------------------------- whose choice


def test_the_side_that_defined_the_opening_owns_it() -> None:
    assert owner(2) is Color.BLACK, "1.e4 c5 is Black's"
    assert owner(3) is Color.WHITE, "1.e4 c5 2.d4 is White's"
    assert owner(None) is None


def test_an_opponents_gambit_is_not_the_players_gambit() -> None:
    """The bug this rule exists for: Black meets the Smith-Morra and the
    classifier reads a gambiteer off White's decision."""
    read = read_openings(
        [row(SMITH_MORRA, "Sicilian Defense: Smith-Morra Gambit", Color.BLACK, 3, 40)]
    )

    assert "gambit" in eco_tags()[SMITH_MORRA], "the line really is tagged as one"
    assert "gambit" not in read.mass, "but not against the player who faced it"
    assert set(read.mass) == set(family_tags()["Sicilian Defense"])


def test_the_player_still_gets_credit_for_the_family_they_chose() -> None:
    """Black picked the Sicilian; White picked the gambit inside it. Black is
    read as a Sicilian player rather than as nothing at all."""
    read = read_openings([row(BOWDLER, "Sicilian Defense: Bowdler Attack", Color.BLACK, 3, 30)])

    assert read.tagged_games == 30
    assert set(read.mass) == set(family_tags()["Sicilian Defense"])


def test_a_defence_the_player_faced_says_nothing_about_them() -> None:
    """White meeting the Sicilian chose neither the family nor the line, so the
    game is not evidence of White's taste in either direction."""
    read = read_openings([row(SICILIAN, "Sicilian Defense", Color.WHITE, 2, 50)])

    assert read.mass == {}
    assert read.tagged_games == 0


def test_the_line_stands_when_nothing_is_known_about_who_chose_it() -> None:
    """An export without a ply is missing data. Dropping those games would empty
    the signal rather than sharpen it."""
    read = read_openings([OpeningRow(VIENNA, "Vienna Game", Color.BLACK, None, 20)])

    assert read.tagged_games == 20


# ----------------------------------------------------------------------- openness


def test_openness_reads_the_eco_volume() -> None:
    """C is open, B is the semi-open defences at half a point, A/D/E are not."""
    assert read_openings([row("C25", "Vienna Game", Color.WHITE, 3, 10)]).openness == 1.0
    assert read_openings([row("B20", "Sicilian Defense", Color.BLACK, 2, 10)]).openness == 0.5
    assert read_openings([row("D02", "Queen's Pawn Game", Color.WHITE, 3, 10)]).openness == 0.0


def test_a_sicilian_player_is_not_pushed_into_the_middle() -> None:
    """Scoring the semi-open defences as closed would put every Sicilian player
    halfway between the poles, which is the failure this replaced."""
    sicilian = read_openings([row("B20", "Sicilian Defense", Color.BLACK, 2, 100)])
    closed = read_openings([row("D02", "Queen's Pawn Game", Color.WHITE, 3, 100)])

    assert sicilian.openness is not None and closed.openness is not None
    assert sicilian.openness > closed.openness


# ------------------------------------------------------- the repertoire end to end


def test_a_vienna_and_sicilian_player_is_not_an_all_rounder() -> None:
    """The report that prompted the rewrite: 1,193 games of Vienna as White and
    Sicilian as Black, a fifth of them ending on the clock, came out as an
    All-Rounder. Both halves of that repertoire are sharp, and the label has to
    say so."""
    read = read_openings(
        [
            row(VIENNA, "Vienna Game: Stanley Variation", Color.WHITE, 3, 251),
            row("C26", "Vienna Game: Falkbeer Variation", Color.WHITE, 3, 92),
            row(SICILIAN, "Sicilian Defense", Color.BLACK, 2, 200),
            row("B23", "Sicilian Defense: Closed", Color.BLACK, 3, 90),
            row(SMITH_MORRA, "Sicilian Defense: Smith-Morra Gambit", Color.BLACK, 3, 59),
            # Faced as White, and so not the player's choice at all.
            row(SICILIAN, "Sicilian Defense", Color.WHITE, 2, 46),
        ]
    )
    signals = Signals(
        games=738,
        tagged_games=read.tagged_games,
        tag_counts=read.counts,
        tag_mass=read.mass,
        openness=read.openness,
        avg_plies=57.7,
        decisive=700,
        draws=38,
        forced_endings=593,
        flagged=93,
        avg_opening_captures=1.57,
    )

    verdict = classify(signals)

    assert verdict["leaning"] == AGGRESSIVE
    assert not verdict["label"].endswith(BLENDED_NOUN), verdict["label"]
    assert verdict["scores"][AGGRESSIVE] > verdict["scores"]["positional"] + 5
