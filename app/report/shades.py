"""Who a verdict most resembles: the "Shades of" line.

Matched on the three scores rather than on the label, which is what makes the
table in ``famous_players`` thirty rows instead of one row per label. There are
forty-four reachable labels and several of them -- "Closed Attacker", "Gambit
Strategist" -- describe nobody, so a label-keyed table could not be filled
honestly. Nearest-neighbour covers every label, including ones added later.
"""

import math
from collections.abc import Mapping

from app.report.famous_players import FAMOUS_PLAYERS, SHADES_SHOWN, SHADES_TAG_BONUS
from app.report.player_type_weights import AXES


def match(scores: Mapping[str, int], flavour: str | None = None) -> list[str]:
    """The closest names to a score split, nearest first.

    ``flavour`` is the report's signature tag, and sharing it is worth
    ``SHADES_TAG_BONUS`` points of distance -- two players sitting equally close
    should not be separated by nothing, and the one playing the same openings is
    the better answer.
    """
    point = tuple(scores[axis] for axis in AXES)
    # Ties break on the name, so the list never depends on the table's order.
    ranked = sorted(
        (
            math.dist(point, profile) - (SHADES_TAG_BONUS if flavour in tags else 0.0),
            name,
        )
        for name, profile, tags in FAMOUS_PLAYERS
    )
    return [name for _, name in ranked[:SHADES_SHOWN]]
