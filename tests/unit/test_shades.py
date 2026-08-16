"""The "Shades of" match, and the table it reads.

Nothing here names a grandmaster. The table is meant to be curated, so every
case derives its fixtures from whatever is currently in ``FAMOUS_PLAYERS`` --
swapping names in and out should never mean editing this file.
"""

import math
from itertools import combinations

import pytest

from app.report.famous_players import FAMOUS_PLAYERS, SHADES_SHOWN
from app.report.player_type_weights import AXES, FLAVOUR_TAGS, MIN_GAMES_FOR_A_LABEL
from app.report.sections.player_type import Signals, classify
from app.report.shades import match

Profile = tuple[int, int, int]


def scores(profile: Profile) -> dict[str, int]:
    return dict(zip(AXES, profile, strict=True))


def midpoint(left: Profile, right: Profile) -> dict[str, int]:
    return scores(tuple(round((a + b) / 2) for a, b in zip(left, right, strict=True)))  # type: ignore[arg-type]


# --------------------------------------------------------------- the table


@pytest.mark.parametrize(("name", "profile", "tags"), FAMOUS_PLAYERS, ids=lambda arg: str(arg))
def test_every_row_is_shaped_like_a_verdict(
    name: str, profile: Profile, tags: tuple[str, ...]
) -> None:
    """The rows are compared against real scores, so they have to be scores."""
    assert sum(profile) == 100, name
    assert all(value >= 0 for value in profile), name
    # Anything outside FLAVOUR_TAGS can never be a report's signature, so it
    # would sit in the table earning its owner nothing.
    assert set(tags) <= set(FLAVOUR_TAGS), name


def test_nobody_appears_twice() -> None:
    names = [name for name, _, _ in FAMOUS_PLAYERS]

    assert len(set(names)) == len(names)


def test_no_two_players_stand_on_the_same_spot() -> None:
    """Identical triples make one of the pair unreachable: ties break on the
    name, so the alphabetically later player could never be a closest match."""
    points = [profile for _, profile, _ in FAMOUS_PLAYERS]

    assert len(set(points)) == len(points)


def test_there_are_more_players_than_a_report_shows() -> None:
    assert len(FAMOUS_PLAYERS) > SHADES_SHOWN


def test_every_flavour_has_somebody_who_plays_it() -> None:
    """A flavour nobody in the table carries is a bonus that never fires."""
    covered = {tag for _, _, tags in FAMOUS_PLAYERS for tag in tags}

    assert set(FLAVOUR_TAGS) - covered == set()


# --------------------------------------------------------------- the match


def test_a_player_is_their_own_closest_match() -> None:
    for name, profile, _ in FAMOUS_PLAYERS:
        assert match(scores(profile))[0] == name


def test_the_signature_does_not_change_who_you_already_are() -> None:
    for name, profile, tags in FAMOUS_PLAYERS:
        assert match(scores(profile), tags[0])[0] == name


def test_a_report_gets_exactly_three_names() -> None:
    assert len(match(scores((34, 33, 33)))) == SHADES_SHOWN


def test_the_names_are_distinct() -> None:
    found = match(scores((50, 25, 25)))

    assert len(set(found)) == SHADES_SHOWN


def test_no_signature_is_fine() -> None:
    """Most players never earn an adjective, and still get shades."""
    assert len(match(scores((40, 30, 30)), None)) == SHADES_SHOWN


def test_a_shared_signature_breaks_a_dead_heat() -> None:
    """Halfway between two players, the one playing your openings wins."""
    (a_name, a_profile, a_tags), (b_name, b_profile, b_tags) = _nearest_distinct_pair()
    between = midpoint(a_profile, b_profile)

    assert match(between, next(iter(set(a_tags) - set(b_tags))))[0] == a_name
    assert match(between, next(iter(set(b_tags) - set(a_tags))))[0] == b_name


def test_a_shared_signature_cannot_cross_the_board() -> None:
    """The bonus nudges; it does not overrule the scores.

    Given the most aggressive player's own numbers, the most positional player
    in the table must stay out of the list even when the report's signature is
    one of theirs.
    """
    attacker = max(FAMOUS_PLAYERS, key=lambda row: row[1][1])
    strategist = max(FAMOUS_PLAYERS, key=lambda row: row[1][0])

    assert strategist[0] not in match(scores(attacker[1]), strategist[2][0])


def _nearest_distinct_pair() -> tuple[
    tuple[str, Profile, tuple[str, ...]], tuple[str, Profile, tuple[str, ...]]
]:
    """The two closest players who each have a tag the other does not."""
    pairs = [
        (math.dist(a[1], b[1]), a, b)
        for a, b in combinations(FAMOUS_PLAYERS, 2)
        if set(a[2]) - set(b[2]) and set(b[2]) - set(a[2])
    ]
    _, first, second = min(pairs, key=lambda row: row[0])
    return first, second


# ---------------------------------------------------------- in the payload


def _signals(games: int) -> Signals:
    return Signals(
        games=games,
        tag_counts={"solid": games, "closed": games},
        tagged_games=games,
        avg_plies=90.0,
        decisive=games,
        draws=0,
        forced_endings=games // 2,
        flagged=0,
        avg_opening_captures=0.5,
    )


def test_a_confident_verdict_carries_its_shades() -> None:
    verdict = classify(_signals(MIN_GAMES_FOR_A_LABEL))

    assert verdict["label"] is not None
    assert len(verdict["shades"]) == SHADES_SHOWN


def test_too_few_games_withholds_the_names_too() -> None:
    """Naming three grandmasters is a bigger claim than the label we refused."""
    verdict = classify(_signals(MIN_GAMES_FOR_A_LABEL - 1))

    assert verdict["label"] is None
    assert verdict["shades"] == []


def test_a_player_with_no_signals_at_all_gets_no_shades() -> None:
    verdict = classify(Signals())

    assert verdict["shades"] == []
