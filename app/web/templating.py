"""The Jinja environment, and the filters the templates lean on.

Formatting lives here rather than in the payload. The report is stored as JSON
and served over the API to callers that are not this frontend, so it carries raw
numbers -- a ``win_rate`` of 0.5432, not the string "54.3%". Turning those into
something readable is a presentation job, and this is the presentation layer.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates
from jinja2 import StrictUndefined
from markupsafe import Markup

from app.core.config import settings
from app.puzzles import piece_code

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def asset_version() -> str:
    """A token that changes whenever a static file does.

    Appended to every ``/static`` URL. Without it a browser keeps the stylesheet
    and the chart script it already has, and an edit to either shows up as a
    page that is half old and half new -- a stale ``charts.js`` reading a key the
    server no longer sends is an exception that stops *every* chart drawing.
    """
    stamps = (path.stat().st_mtime_ns for path in STATIC_DIR.glob("*") if path.is_file())
    return f"{max(stamps, default=0):x}"[-10:]


#: The characters that could end a ``<script>`` element early, or that a JS
#: parser treats as a line break where JSON does not. See ``json_data``.
_SCRIPT_ESCAPES = {
    ord("<"): "\\u003c",
    ord(">"): "\\u003e",
    ord("&"): "\\u0026",
    0x2028: "\\u2028",
    0x2029: "\\u2029",
}


def json_data(value: Any) -> Markup:
    """JSON safe to embed in a ``<script type="application/json">`` block.

    Autoescaping does not help inside a script element: its content is raw text,
    so Jinja would write ``&quot;`` where the JSON parser needs ``"``. The fix is
    to escape the three characters that could close the element early --
    ``</script>`` becomes ``\\u003c/script>``, which is the same string to JSON
    and inert to the HTML parser.

    Worth the care even though the strings that reach it -- perf names, month
    stamps -- look harmless: they come from Lichess rather than from us, and the
    escaping is what makes that not matter.
    """
    encoded = json.dumps(value, separators=(",", ":")).translate(_SCRIPT_ESCAPES)
    return Markup(encoded)


def commas(value: Any) -> str:
    """1234 -> "1,234". Blank for nothing, so a template can print it bare."""
    if value is None:
        return "--"
    return f"{value:,}"


def moves(sans: Any, ply: int = 1) -> str:
    """A list of SAN moves, numbered from the ply it starts on.

    ``["e4", "e5", "Nc3"]`` is "1.e4 e5 2.Nc3". A line that starts mid-move --
    the drill-down branches do, since the mainline above them is shared -- opens
    with the black-to-move form, "2...Nf6 3.f4", or the moves would be numbered
    a move out and read as a different line entirely.
    """
    written = []
    for offset, san in enumerate(sans or []):
        absolute = ply + offset
        number = (absolute + 1) // 2
        if absolute % 2:
            written.append(f"{number}.{san}")
        elif offset == 0:
            written.append(f"{number}…{san}")
        else:
            written.append(san)
    return " ".join(written)


def plural(value: Any, noun: str, suffix: str = "s") -> str:
    """A count and its noun, agreeing: "1 opening", "38 openings"."""
    if value is None:
        return f"-- {noun}{suffix}"
    return f"{commas(value)} {noun}{'' if value == 1 else suffix}"


#: Lichess names its time controls in camelCase and prints them capitalised, so
#: the raw key is what we store and this is what a reader should see. Only the
#: irregular ones are listed; the rest just want a capital letter.
PERF_NAMES = {
    "ultraBullet": "UltraBullet",
    "kingOfTheHill": "King of the Hill",
    "racingKings": "Racing Kings",
    "threeCheck": "Three-check",
    "chess960": "Chess960",
}


def perf(value: Any) -> str:
    """A Lichess perf key as a name: "ultraBullet" -> "UltraBullet"."""
    if not value:
        return "--"
    return PERF_NAMES.get(value) or f"{value[:1].upper()}{value[1:]}"


def perf_rows(rows: Any, total: Any, floor: float = 0.01) -> list[dict[str, Any]]:
    """The record table's rows, with the negligible time controls merged into one.

    Two classical games in a year do not deserve a row, and their score is a
    percentage of noise printed to one decimal. They are merged only when more
    than one is small enough for it -- an "Other" row that names a single perf
    says strictly less than that perf's own row would.
    """
    listed = list(rows or [])
    if not listed or not total:
        return []
    cutoff = total * floor
    minor = [row for row in listed if row["games"] < cutoff]
    if len(minor) < 2:
        return [_perf_row(row) for row in listed]
    major = (row for row in listed if row["games"] >= cutoff)
    return [_perf_row(row) for row in major] + [
        {
            "label": "Other",
            "games": sum(row["games"] for row in minor),
            # The names, where a row that stood alone would show its record:
            # over a handful of games, which they were is the more useful fact.
            "record": ", ".join(perf(row["perf"]).lower() for row in minor),
            "score": None,
        }
    ]


def _perf_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": perf(row["perf"]),
        "games": row["games"],
        "record": f"{commas(row['wins'])} / {commas(row['draws'])} / {commas(row['losses'])}",
        "score": row["score"],
    }


def percent(value: Any, digits: int = 1) -> str:
    """A 0..1 ratio as a percentage. The payload never stores these pre-formatted."""
    if value is None:
        return "--"
    return f"{value * 100:.{digits}f}%"


def decimal(value: Any, digits: int = 1) -> str:
    """A number at a fixed number of decimals.

    The payload stores what the arithmetic produced -- an accuracy of 84.44,
    an ACPL of 41.1 -- and how many of those digits are worth printing is a
    presentation decision. Nothing is None here in practice, but a perf whose
    analysed games all lack an accuracy figure would otherwise print "None%".
    """
    if value is None:
        return "--"
    return f"{value:.{digits}f}"


def signed(value: Any) -> str:
    """A rating change, with the sign that makes it readable at a glance."""
    if value is None:
        return "--"
    return f"{value:+,}"


FILES = "abcdefgh"


def board(fen: Any, orientation: Any = "white") -> list[dict[str, Any]]:
    """A FEN as 64 squares in reading order, from ``orientation``'s side.

    Server-side so the position is on the page before any JavaScript runs: with
    scripting off a puzzle is still a diagram and a link to the game, which is
    most of what it is. The script only adds the part that needs a click.

    Only the placement field is read. Castling rights and the en-passant square
    matter to the rules, and the rules were applied at ingest -- what reaches
    the browser is a list of legal moves, not a position to reason about.

    Squares carry the *name* of a piece, not a picture of one. The artwork is
    twelve vendored SVGs the stylesheet points at, so a diagram is 64 empty
    elements and no markup that changes when the set does.
    """
    placement = str(fen or "").split(" ")[0]
    squares: list[dict[str, Any]] = []
    for rank_index, row in enumerate(placement.split("/")[:8]):
        rank = 8 - rank_index
        file_index = 0
        for char in row:
            if char.isdigit():
                for _ in range(int(char)):
                    squares.append(_square(file_index, rank, None))
                    file_index += 1
            else:
                squares.append(_square(file_index, rank, char))
                file_index += 1
    if orientation == "black":
        squares.reverse()
    return squares


def _square(file_index: int, rank: int, piece: str | None) -> dict[str, Any]:
    return {
        "name": f"{FILES[file_index]}{rank}" if file_index < 8 else "",
        # "wK", "bQ" and so on: the filename of the piece's artwork, and what
        # the CSS class is built from. Shared with the puzzle resolution, which
        # names the same squares when a move is played on top of this.
        "piece": None if piece is None else piece_code(piece),
        "dark": (file_index + rank) % 2 == 1,
    }


def day(value: str | None) -> str:
    """An ISO timestamp as "3 Mar 2025"."""
    moment = _parse(value)
    return "--" if moment is None else f"{moment.day} {moment:%b %Y}"


def clock(value: str | None) -> str:
    """An ISO timestamp as "3 Mar, 19:40"."""
    moment = _parse(value)
    return "--" if moment is None else f"{moment.day} {moment:%b}, {moment:%H:%M}"


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def build_templates() -> Jinja2Templates:
    """The configured environment. Autoescaping is Jinja2Templates' default."""
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters.update(
        {
            "json_data": json_data,
            "commas": commas,
            "plural": plural,
            "moves": moves,
            "perf": perf,
            "perf_rows": perf_rows,
            "percent": percent,
            "decimal": decimal,
            "signed": signed,
            "board": board,
            "day": day,
            "clock": clock,
        }
    )
    # Read once: the token only has to change between deploys, and in dev the
    # reloader rebuilds this module whenever the files it measures change.
    templates.env.globals["asset_v"] = asset_version()
    templates.env.globals["puzzles_teased"] = settings.report_puzzles_teased
    # An undefined name in a template is a bug in the template, and rendering it
    # as an empty string is how that would reach production unnoticed.
    templates.env.undefined = StrictUndefined
    return templates


templates = build_templates()
