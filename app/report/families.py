"""Rolling opening leaves up to families, and deciding whose choice a line was.

Lichess names an opening as ``Family: Variation, Sub-variation``. Four Vienna
rows and four Sicilian rows are two systems to a player and eight strings to a
database, and every part of the report that groups openings wants the family:
the openings section builds its cards from it, and the classifier reads a
player's taste from it.

Whose choice a line was matters just as much. "Sicilian Defense: Smith-Morra
Gambit" is White's decision, played against a Black player who chose nothing but
the Sicilian -- crediting Black with a gambit is reading White's taste off
Black's games. The side that made the move defining an opening owns it, and
``opening_ply`` says which side that was.
"""

from collections import Counter, defaultdict
from functools import lru_cache
from typing import Any

from sqlalchemy import ColumnElement, func

from app.db.models import Color
from app.db.seed import load_openings_meta


def family(name: str | None) -> str:
    """``"Vienna Game: Stanley Variation"`` -> ``"Vienna Game"``. ``""`` for nothing."""
    return (name or "").split(":", 1)[0].strip()


def family_sql(column: Any) -> ColumnElement[str]:
    """The same rule as a SQL expression, for grouping without loading the rows.

    Kept beside ``family`` so the two cannot drift: a section that groups in
    Postgres and a classifier that groups in Python have to agree on what a
    family is, or the same games come out as different systems.
    """
    return func.split_part(func.coalesce(column, ""), ":", 1)


#: A family whose name ends in one of these is Black's answer to something,
#: whatever ply the player happened to reach it on.
DEFENCE = ("defense", "defence")


def family_owner(name: str, ply: int | None) -> Color | None:
    """Which side chose a whole family, given the shallowest ply seen for it.

    The ply alone is not enough. A Black player who only ever meets
    anti-Sicilians has no game in the corpus where the Sicilian is settled
    before White's second move, and reading ownership off ply 3 would hand the
    Sicilian to White and leave that player with no repertoire at all. The name
    settles those: an opening called a Defense was Black's idea by definition.

    Everything else falls to the ply, which handles the families the naming
    convention says nothing about -- "Indian Game" is settled by Black on ply 2
    and "Vienna Game" by White on ply 3, and neither name admits it.
    """
    if name.strip().lower().endswith(DEFENCE):
        return Color.BLACK
    return owner(ply)


def owner(ply: int | None) -> Color | None:
    """Which side chose an opening that took ``ply`` plies to define.

    White moves on the odd plies. "Sicilian Defense" is settled at ply 2, by
    Black; "Sicilian Defense: Smith-Morra Gambit" at ply 3, by White. ``None``
    when the export gave no ply, which is not the same as nobody choosing.
    """
    if not ply:
        return None
    return Color.WHITE if ply % 2 else Color.BLACK


@lru_cache(maxsize=1)
def eco_tags() -> dict[str, tuple[str, ...]]:
    """ECO code -> its style tags, straight off the seeded table."""
    return {row["eco"]: tuple(row["style_tags"]) for row in load_openings_meta()}


@lru_cache(maxsize=1)
def family_tags() -> dict[str, tuple[str, ...]]:
    """Family -> the tags most of its codes carry.

    The rollup a game falls back on when the line it reached was the opponent's
    idea. Majority rather than union: B21 is the only Sicilian code tagged
    ``gambit``, and a Sicilian player who keeps meeting the Smith-Morra should
    come out as somebody who plays the Sicilian, not as a gambiteer.
    """
    codes: dict[str, int] = Counter()
    tags: dict[str, Counter[str]] = defaultdict(Counter)
    for row in load_openings_meta():
        name = family(row["family"])
        codes[name] += 1
        tags[name].update(row["style_tags"])
    return {
        name: tuple(sorted(tag for tag, count in counted.items() if count * 2 >= codes[name]))
        for name, counted in tags.items()
    }
