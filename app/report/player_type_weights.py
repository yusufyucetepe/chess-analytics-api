"""Every number the player-type classifier uses. No logic lives here.

The brief asks for the weights to sit in one module so they can be tuned without
touching the classifier, and for the result to be presented as a playful
heuristic rather than a measurement. Both are load-bearing: these values are
judgement calls about chess, not measurements of anything, and the only honest
way to change the classifier's opinion is to edit this file.
"""

from typing import Final

POSITIONAL: Final = "positional"
AGGRESSIVE: Final = "aggressive"
TACTICAL: Final = "tactical"

AXES: Final[tuple[str, str, str]] = (POSITIONAL, AGGRESSIVE, TACTICAL)

#: The controlled vocabulary for `openings_meta.style_tags`, and what each tag
#: says about the player who keeps choosing it. A tag outside this mapping is a
#: seeding mistake, so the seeder rejects the file rather than ignoring it.
TAG_AXES: Final[dict[str, dict[str, float]]] = {
    "solid": {POSITIONAL: 1.0},
    "closed": {POSITIONAL: 1.0},
    "positional": {POSITIONAL: 1.0},
    "strategic": {POSITIONAL: 1.0},
    "gambit": {AGGRESSIVE: 1.0, TACTICAL: 0.5},
    "attacking": {AGGRESSIVE: 1.0},
    "sharp": {AGGRESSIVE: 0.5, TACTICAL: 0.5},
    "tactical": {TACTICAL: 1.0},
    "open": {TACTICAL: 0.5, AGGRESSIVE: 0.25},
    "unbalanced": {TACTICAL: 0.75, AGGRESSIVE: 0.25},
    "dynamic": {TACTICAL: 0.5, AGGRESSIVE: 0.5},
    # Descriptive rather than stylistic: a hypermodern or offbeat repertoire
    # says something about taste, not about how the player wins.
    "hypermodern": {},
    "offbeat": {},
    "flexible": {},
}

#: How much each signal counts towards the final split. Raising one of these
#: makes the classifier care more about that behaviour.
SIGNAL_WEIGHTS: Final[dict[str, float]] = {
    "openings": 3.0,
    "gambits": 1.5,
    "game_length": 2.0,
    "finish": 2.0,
    "decisiveness": 1.5,
    "exchanges": 2.0,
}

#: Plies. A game shorter than this reads as fully aggressive, longer than the
#: high mark as fully positional, and anything between is interpolated.
SHORT_GAME_PLIES: Final = 40.0
LONG_GAME_PLIES: Final = 90.0

#: Captures in the first 12 plies. Zero is a quiet opening, this many or more is
#: a brawl from move one.
BUSY_OPENING_CAPTURES: Final = 3.0

#: Below this many games the label is withheld: three openings and a lucky mate
#: is not a playing style.
MIN_GAMES_FOR_A_LABEL: Final = 30

#: Awarded to the axis with the highest score. The runner-up only makes it into
#: the blurb when it is within `BLEND_MARGIN` points.
LABELS: Final[dict[str, str]] = {
    POSITIONAL: "The Squeezer",
    AGGRESSIVE: "The Attacker",
    TACTICAL: "The Brawler",
}

BLENDED_LABEL: Final = "The All-Rounder"
#: Percentage points. Inside this, the top two axes are close enough that
#: picking one of them would be noise dressed up as a verdict.
BLEND_MARGIN: Final = 8.0

DISCLAIMER: Final = (
    "A playful heuristic from your openings and how your games ended -- not a measurement."
)
