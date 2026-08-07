"""Static reference data: the ECO table behind the player-type classifier.

Kept apart from the ingest layer on purpose. Everything in ``openings_meta`` is
authored by us and identical for every player, so it belongs to the schema
rather than to any one report, and it is seeded by a migration.
"""

import json
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "openings_meta.json"

#: The controlled vocabulary the data file is allowed to use. Defined here, not
#: imported from the classifier: a migration imports this module directly, and
#: reaching into `app.report` would make the seed depend on the whole report
#: package. A test asserts the classifier still has an opinion about each one.
STYLE_TAGS = frozenset(
    {
        "solid",
        "closed",
        "positional",
        "strategic",
        "gambit",
        "attacking",
        "sharp",
        "tactical",
        "open",
        "unbalanced",
        "dynamic",
        "hypermodern",
        "offbeat",
        "flexible",
    }
)

#: ECO codes run A00..E99 -- one letter and two digits, no gaps.
ECO_LETTERS = "ABCDE"
ECO_NUMBERS = 100


def eco_codes(start: str, end: str) -> list[str]:
    """Every ECO code from ``start`` to ``end`` inclusive."""
    first, last = _index(start), _index(end)
    if first > last:
        raise ValueError(f"ECO range {start}-{end} runs backwards")
    return [_code(i) for i in range(first, last + 1)]


def load_openings_meta(path: Path | None = None) -> list[dict[str, Any]]:
    """Expand the range file into one row per ECO code.

    Raises rather than skipping on a bad range or an unknown tag: this runs in a
    migration, and reference data that is quietly half-loaded would show up much
    later as a classifier that had mysteriously lost its opinions.
    """
    raw = json.loads((path or DATA_FILE).read_text(encoding="utf-8"))
    rows: dict[str, dict[str, Any]] = {}
    for entry in raw["ranges"]:
        unknown = set(entry["style_tags"]) - STYLE_TAGS
        if unknown:
            raise ValueError(f"{entry['from']}-{entry['to']} uses unknown tags: {sorted(unknown)}")
        for code in eco_codes(entry["from"], entry["to"]):
            if code in rows:
                raise ValueError(f"ECO {code} is claimed by two ranges")
            rows[code] = {
                "eco": code,
                "family": entry["family"],
                "style_tags": list(entry["style_tags"]),
            }
    return [rows[code] for code in sorted(rows)]


@lru_cache(maxsize=1)
def tag_baseline() -> dict[str, float]:
    """What share of ECO codes carries each style tag.

    The yardstick the player-type classifier measures a repertoire against. A
    tag is only distinctive if someone plays it *more* than it merely exists:
    `positional` sits on 47% of the table, so half a player's games being
    positional is unremarkable, while `gambit` at 5% makes the same share
    striking.

    Known limitation: this counts codes, not games, so it treats all 500 as
    equally likely to be played. They are not -- A00, B00 and C20 are catch-alls
    that absorb a lot of real games, which flatters the tags sitting on them.
    Weighting by games actually seen would need a corpus we do not have yet.
    """
    rows = load_openings_meta()
    counts = Counter(tag for row in rows for tag in row["style_tags"])
    return {tag: count / len(rows) for tag, count in counts.items()}


def _index(code: str) -> int:
    if len(code) != 3:
        raise ValueError(f"{code!r} is not an ECO code")
    letter, digits = code[0].upper(), code[1:]
    if letter not in ECO_LETTERS or not digits.isdigit():
        raise ValueError(f"{code!r} is not an ECO code")
    return ECO_LETTERS.index(letter) * ECO_NUMBERS + int(digits)


def _code(index: int) -> str:
    return f"{ECO_LETTERS[index // ECO_NUMBERS]}{index % ECO_NUMBERS:02d}"
