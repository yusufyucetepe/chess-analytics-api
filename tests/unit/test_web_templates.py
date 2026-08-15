"""The templates and their filters, rendered without a server.

Rendering is the part of the frontend worth testing on its own: the routes are
thin, but a template reads three dozen payload keys and a wrong one is invisible
until a real report happens to reach that branch. ``StrictUndefined`` turns a
misspelled key into an exception, so simply rendering each payload is most of
the test.
"""

import json
import re
from typing import Any

import pytest
from starlette.requests import Request

from app.web import templating
from app.web.charts import chart_data
from app.web.templating import templates
from tests.payloads import empty_payload, full_payload, sparse_payload

PAYLOADS = {"full": full_payload, "sparse": sparse_payload, "empty": empty_payload}


class View:
    """Enough of a ``ReportView`` for the templates that take one."""

    id = "0d5b2b1e-2c1a-4a2e-9c3e-1a2b3c4d5e6f"
    username = "Zhigalko_Sergei"
    status = "running"
    error = None


@pytest.fixture
def request_() -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"test")],
            "client": ("127.0.0.1", 1),
            "server": ("test", 80),
            "app": None,
        }
    )


def render(name: str, request_: Request, **context: Any) -> str:
    return templates.get_template(name).render(request=request_, **context)


def render_report(request_: Request, payload: dict[str, Any]) -> str:
    sections = payload["sections"]
    return render(
        "report.html",
        request_,
        view=View(),
        report=payload,
        sections=sections,
        chart_data=chart_data(sections),
    )


# ------------------------------------------------------------------ rendering


@pytest.mark.parametrize("name", list(PAYLOADS))
def test_every_payload_renders(name: str, request_: Request) -> None:
    """Any undefined name would raise here rather than print as an empty string."""
    html = render_report(request_, PAYLOADS[name]())

    assert '<article class="report">' in html
    assert "Zhigalko_Sergei" in html


def test_a_busy_year_shows_the_numbers_that_earned_it(request_: Request) -> None:
    html = render_report(request_, full_payload())

    assert "Offbeat Strategist" in html
    assert "12,844" in html, "moves, with separators"
    assert "59.5" in html, "hours played"
    assert "2h 38m" in html, "the longest session, not '157.9'"
    assert "+101" in html, "the rating gap on the best win, signed"
    assert "https://lichess.org/abcd1234" in html


def test_a_thin_year_says_so_instead_of_inventing_numbers(request_: Request) -> None:
    """Every degraded branch at once: no streak, no tilt, no label, no quality."""
    html = render_report(request_, sparse_payload())

    assert "Not enough games to call it" in html
    assert "too few to say anything honest" in html
    assert "Best win" not in html
    assert "Longest win streak" not in html
    assert "After two losses" not in html


def test_a_year_with_no_games_stops_after_the_header(request_: Request) -> None:
    html = render_report(request_, empty_payload())

    assert "There is nothing to wrap yet" in html
    assert "Move quality" not in html, "no sections at all, not empty ones"


def test_the_quality_caveat_travels_with_the_numbers(request_: Request) -> None:
    """The brief's rule: a coverage figure never appears without its denominator."""
    html = render_report(request_, full_payload())

    assert "83.81" in html
    assert "Based on the 45 of 357 games that were analysed." in html


def test_the_disclaimer_is_on_the_page_not_just_in_the_payload(request_: Request) -> None:
    assert "not a measurement" in render_report(request_, full_payload())


def test_the_repertoire_tree_nests(request_: Request) -> None:
    """A line with a continuation is a <details>, so it folds without JavaScript."""
    html = render_report(request_, full_payload())

    assert "<details" in html
    assert re.search(r"<summary>.*?e4.*?</summary>", html, re.S)


@pytest.mark.parametrize(
    ("template", "context", "expected"),
    [
        ("index.html", {}, 'hx-post="/reports"'),
        ("job.html", {"view": View(), "poll_seconds": 2, "failed": False}, "hx-trigger"),
        ("problem.html", {"message": "Nope.", "username": "someone"}, "someone"),
    ],
)
def test_the_other_pages_render(
    template: str, context: dict[str, Any], expected: str, request_: Request
) -> None:
    assert expected in render(template, request_, **context)


def test_a_finished_job_fragment_stops_asking(request_: Request) -> None:
    """The failed fragment carries no hx-get, which is what ends the polling."""
    html = render("partials/job.html", request_, view=View(), poll_seconds=2, failed=True)

    assert "hx-get" not in html
    assert "hx-trigger" not in html


def test_a_running_job_fragment_keeps_asking(request_: Request) -> None:
    html = render("partials/job.html", request_, view=View(), poll_seconds=2, failed=False)

    assert f"/reports/{View.id}/status" in html
    assert "every 2s" in html


# ------------------------------------------------------------------- escaping


def test_the_chart_block_cannot_end_the_script_element() -> None:
    """The payload is JSON in a <script>, where autoescaping does not apply.

    Left alone, a string containing ``</script>`` would close the element and
    everything after it would be parsed as HTML.
    """
    encoded = templating.json_data({"perf": "</script><img src=x onerror=alert(1)>"})

    assert "</script>" not in encoded
    assert "\\u003c/script\\u003e" in encoded
    assert json.loads(str(encoded))["perf"] == "</script><img src=x onerror=alert(1)>"


@pytest.mark.parametrize("hostile", ["<", ">", "&", "\u2028", "\u2029"])
def test_every_character_that_could_break_out_is_escaped(hostile: str) -> None:
    encoded = str(templating.json_data({"x": hostile}))

    assert hostile not in encoded
    assert json.loads(encoded)["x"] == hostile


def test_a_players_name_is_escaped_in_the_page_body(request_: Request) -> None:
    """Autoescaping does cover the body -- this is the test that says so."""
    payload = full_payload()
    payload["player"]["username"] = "<img src=x onerror=alert(1)>"

    html = render_report(request_, payload)

    assert "<img src=x" not in html
    assert "&lt;img src=x" in html


# -------------------------------------------------------------------- filters


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1234, "1,234"), (0, "0"), (None, "--")],
)
def test_commas(value: Any, expected: str) -> None:
    assert templating.commas(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.5630, "56.3%"), (1.0, "100.0%"), (0.0, "0.0%"), (None, "--")],
)
def test_percent(value: Any, expected: str) -> None:
    assert templating.percent(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"), [(101, "+101"), (-58, "-58"), (0, "+0"), (None, "--")]
)
def test_signed(value: Any, expected: str) -> None:
    assert templating.signed(value) == expected


@pytest.mark.parametrize(
    ("minutes", "expected"), [(157.9, "2h 38m"), (45, "45m"), (0, "0m"), (None, "--")]
)
def test_duration(minutes: Any, expected: str) -> None:
    assert templating.duration(minutes) == expected


def test_dates_read_as_dates() -> None:
    assert templating.day("2026-03-03T19:40:00+00:00") == "3 Mar 2026"
    assert templating.clock("2026-03-03T19:40:00+00:00") == "3 Mar, 19:40"
    assert templating.hour_label(7) == "07:00"


@pytest.mark.parametrize("junk", [None, "", "not a date"])
def test_a_date_that_is_not_one_prints_a_dash_rather_than_raising(junk: Any) -> None:
    """The payload is JSON from a column; a template is the wrong place to crash."""
    assert templating.day(junk) == "--"
    assert templating.clock(junk) == "--"
