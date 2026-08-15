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

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

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


def percent(value: Any, digits: int = 1) -> str:
    """A 0..1 ratio as a percentage. The payload never stores these pre-formatted."""
    if value is None:
        return "--"
    return f"{value * 100:.{digits}f}%"


def signed(value: Any) -> str:
    """A rating change, with the sign that makes it readable at a glance."""
    if value is None:
        return "--"
    return f"{value:+,}"


def day(value: str | None) -> str:
    """An ISO timestamp as "3 Mar 2025"."""
    moment = _parse(value)
    return "--" if moment is None else f"{moment.day} {moment:%b %Y}"


def clock(value: str | None) -> str:
    """An ISO timestamp as "3 Mar, 19:40"."""
    moment = _parse(value)
    return "--" if moment is None else f"{moment.day} {moment:%b}, {moment:%H:%M}"


def hour_label(value: Any) -> str:
    return "--" if value is None else f"{int(value):02d}:00"


def duration(minutes: Any) -> str:
    """Minutes as "2h 38m" -- a 158-minute session does not read as a number."""
    if minutes is None:
        return "--"
    hours, rest = divmod(round(float(minutes)), 60)
    return f"{hours}h {rest:02d}m" if hours else f"{rest}m"


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
            "percent": percent,
            "signed": signed,
            "day": day,
            "clock": clock,
            "hour_label": hour_label,
            "duration": duration,
        }
    )
    # An undefined name in a template is a bug in the template, and rendering it
    # as an empty string is how that would reach production unnoticed.
    templates.env.undefined = StrictUndefined
    return templates


templates = build_templates()
