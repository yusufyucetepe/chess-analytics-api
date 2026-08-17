"""The templates and their filters, rendered without a server.

Rendering is the part of the frontend worth testing on its own: the routes are
thin, but a template reads three dozen payload keys and a wrong one is invisible
until a real report happens to reach that branch. ``StrictUndefined`` turns a
misspelled key into an exception, so simply rendering each payload is most of
the test.
"""

import json
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
    assert "+101" in html, "the rating gap on the best win, signed"
    assert "https://lichess.org/abcd1234" in html


def test_a_thin_year_says_so_instead_of_inventing_numbers(request_: Request) -> None:
    """Every degraded branch at once: no streak, no tilt, no label, no quality."""
    html = render_report(request_, sparse_payload())

    assert "Not enough games to call it" in html
    assert "too few to say anything honest" in html
    assert "Best win" not in html
    assert "Longest win streak" not in html


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


def test_the_shades_are_named_above_the_archetype(request_: Request) -> None:
    html = render_report(request_, full_payload())
    shades = full_payload()["sections"]["player_type"]["shades"]

    assert "Shades of:" in html
    for name in shades:
        assert name in html
    assert html.index("Shades of:") < html.index("archetype")


def test_a_thin_year_is_told_no_names(request_: Request) -> None:
    """Withheld with the label, not shown as an empty list."""
    assert "Shades of:" not in render_report(request_, sparse_payload())


def test_a_report_built_before_shades_existed_still_renders(request_: Request) -> None:
    """The templates run on StrictUndefined, so a missing key is an exception
    rather than a blank -- and stored payloads predate this section."""
    payload = full_payload()
    del payload["sections"]["player_type"]["shades"]

    html = render_report(request_, payload)

    assert "Shades of:" not in html
    assert "Offbeat Strategist" in html


def test_the_repertoire_reads_as_systems_rather_than_a_tree(request_: Request) -> None:
    """A card per system with its mainline, over the bar that is the actual
    story. The sorted tree stays in the payload and off the page."""
    html = render_report(request_, full_payload())

    assert "Vienna Game" in html
    assert "1.e4 e5 2.Nc3" in html, "the moves the whole system shares"
    assert 'class="stack"' in html
    assert "Everything else" in html


def test_the_openings_and_the_repertoire_are_one_section(request_: Request) -> None:
    """Concentration, then the systems, then the openings under them -- one card
    rather than two saying overlapping things about the same games."""
    html = render_report(request_, full_payload())

    assert html.count("<h2>Opening Repertoire</h2>") == 1
    assert html.index('class="stack"') < html.index("Most played") < html.index("As white")


def test_the_best_and_worst_openings_are_off_the_page(request_: Request) -> None:
    """A single opening picked out of a year is a fluke with a caption. It stays
    in the payload, where a caller can decide for themselves."""
    html = render_report(request_, full_payload())

    assert "French Defense" not in html, "the fixture's worst opening"
    assert "Ranked over openings" not in html
    assert full_payload()["sections"]["openings"]["worst"]["eco"] == "C00"


def test_the_drill_down_opens_one_system_at_a_time(request_: Request) -> None:
    """`name` on <details> is what makes them exclusive. Without it the page is
    six open trees again, which is what this section replaced."""
    html = render_report(request_, full_payload())

    assert html.count("<details") == html.count('<details name="repertoire"')
    assert "<details" in html


def test_the_branches_are_numbered_from_where_they_start(request_: Request) -> None:
    """The Vienna branches begin on Black's second move. Numbered from one they
    would read as a different line."""
    html = render_report(request_, full_payload())

    assert "2…Bc5 3.Bc4" in html
    assert "1.Bc5" not in html


def test_a_report_built_before_the_systems_existed_still_renders(request_: Request) -> None:
    """StrictUndefined: stored payloads have `tree` and no `systems`. The rest of
    the section is built from keys they do have, so it still stands."""
    payload = full_payload()
    del payload["sections"]["openings"]["systems"]
    del payload["sections"]["openings"]["concentration"]

    html = render_report(request_, payload)

    assert 'class="stack"' not in html
    assert "Vienna Game" not in html
    assert "70 different openings over the year" in html
    assert "Most played" in html, "the per-colour tables do not depend on systems"


def test_the_signature_claims_no_multiple_of_anybody(request_: Request) -> None:
    """Stored payloads still carry `lift`, so this is about the page, not the
    section: there is no denominator that makes "5.1x more often" true."""
    html = render_report(request_, full_payload())

    assert "more often" not in html
    assert "&times;" not in html
    assert "10% of games" in html, "the share it is built on still stands"


def test_the_openings_card_counts_openings_not_eco_codes(request_: Request) -> None:
    """The jargon stays a hoverable tag on the code itself; the prose says what
    the number is a count of."""
    html = render_report(request_, full_payload())

    prose = html.replace('title="Encyclopaedia of Chess Openings code"', "")
    assert "ECO" not in prose
    assert "70 different openings over the year" in html
    assert 'class="eco"' in html, "the code is still shown beside the name"


def test_the_page_no_longer_says_when_you_play(request_: Request) -> None:
    html = render_report(request_, full_payload())

    assert "When you play" not in html
    assert "Longest session" not in html
    assert "chart-hours" not in html


def test_the_player_type_donut_gets_its_canvas(request_: Request) -> None:
    html = render_report(request_, full_payload())

    assert 'id="chart-player-type"' in html
    # The numbers are text beside the canvas, not pixels inside it.
    assert "positional" in html
    assert "46" in html


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
    [(1, "1 opening"), (0, "0 openings"), (1234, "1,234 openings"), (None, "-- openings")],
)
def test_plural(value: Any, expected: str) -> None:
    assert templating.plural(value, "opening") == expected


def test_moves_are_numbered_from_the_first_move() -> None:
    assert templating.moves(["e4", "e5", "Nc3", "Nf6"]) == "1.e4 e5 2.Nc3 Nf6"


def test_a_line_starting_on_blacks_move_says_so() -> None:
    """The drill-down branches start below a shared mainline, so they open
    mid-move. "2.Bc5" is not a move anyone played."""
    assert templating.moves(["Bc5", "Bc4"], 4) == "2…Bc5 3.Bc4"
    assert templating.moves(["Bc4", "d6"], 5) == "3.Bc4 d6"


@pytest.mark.parametrize("empty", [None, []])
def test_a_line_with_no_moves_prints_nothing(empty: Any) -> None:
    assert templating.moves(empty) == ""


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


def test_dates_read_as_dates() -> None:
    assert templating.day("2026-03-03T19:40:00+00:00") == "3 Mar 2026"
    assert templating.clock("2026-03-03T19:40:00+00:00") == "3 Mar, 19:40"


@pytest.mark.parametrize("junk", [None, "", "not a date"])
def test_a_date_that_is_not_one_prints_a_dash_rather_than_raising(junk: Any) -> None:
    """The payload is JSON from a column; a template is the wrong place to crash."""
    assert templating.day(junk) == "--"
    assert templating.clock(junk) == "--"
