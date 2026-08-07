"""Static reference data: the ECO table behind the player-type classifier.

Kept apart from the ingest layer on purpose. Everything in ``openings_meta`` is
authored by us and identical for every player, so it belongs to the schema
rather than to any one report, and it is seeded by a migration.
"""

import json
from pathlib import Path
from typing import Any

from app.report.player_type_weights import TAG_AXES

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "openings_meta.json"

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
        unknown = set(entry["style_tags"]) - set(TAG_AXES)
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


def _index(code: str) -> int:
    if len(code) != 3:
        raise ValueError(f"{code!r} is not an ECO code")
    letter, digits = code[0].upper(), code[1:]
    if letter not in ECO_LETTERS or not digits.isdigit():
        raise ValueError(f"{code!r} is not an ECO code")
    return ECO_LETTERS.index(letter) * ECO_NUMBERS + int(digits)


def _code(index: int) -> str:
    return f"{ECO_LETTERS[index // ECO_NUMBERS]}{index % ECO_NUMBERS:02d}"
