"""The ECO range file and its expansion into openings_meta rows."""

import json
from pathlib import Path
from typing import Any

import pytest

from app.db.seed import eco_codes, load_openings_meta
from app.report.player_type_weights import TAG_AXES

ALL_CODES = {f"{letter}{number:02d}" for letter in "ABCDE" for number in range(100)}


def write(tmp_path: Path, ranges: list[dict[str, Any]]) -> Path:
    path = tmp_path / "openings_meta.json"
    path.write_text(json.dumps({"ranges": ranges}), encoding="utf-8")
    return path


def test_every_eco_code_is_covered_exactly_once() -> None:
    """A gap would be an opening the classifier silently has no opinion about."""
    rows = load_openings_meta()

    assert len(rows) == len(ALL_CODES) == 500
    assert {row["eco"] for row in rows} == ALL_CODES


def test_every_tag_is_in_the_classifier_vocabulary() -> None:
    """A tag the weights module has never heard of contributes nothing."""
    used = {tag for row in load_openings_meta() for tag in row["style_tags"]}

    assert used <= set(TAG_AXES)


def test_the_ranges_land_on_the_openings_they_claim() -> None:
    """Spot checks against ECO codes with well-known homes."""
    by_eco = {row["eco"]: row for row in load_openings_meta()}

    assert by_eco["B22"]["family"] == "Sicilian Defense: Alapin Variation"
    assert "gambit" in by_eco["C33"]["style_tags"], "King's Gambit"
    assert "positional" in by_eco["C88"]["style_tags"], "Ruy Lopez"
    assert by_eco["E92"]["family"] == "King's Indian Defense"


def test_a_range_expands_across_the_letter_boundary() -> None:
    assert eco_codes("A98", "B01") == ["A98", "A99", "B00", "B01"]


def test_a_single_code_range_is_just_that_code() -> None:
    assert eco_codes("D45", "D45") == ["D45"]


def test_a_backwards_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="backwards"):
        eco_codes("C10", "C05")


@pytest.mark.parametrize("code", ["", "X00", "A1", "A100", "AA0"], ids=repr)
def test_a_malformed_code_is_rejected(code: str) -> None:
    with pytest.raises(ValueError, match="not an ECO code"):
        eco_codes(code, "A00")


def test_an_unknown_tag_stops_the_seed(tmp_path: Path) -> None:
    """Loudly, in the migration -- not quietly, months later, in the classifier."""
    path = write(tmp_path, [{"from": "A00", "to": "A00", "family": "X", "style_tags": ["swashy"]}])

    with pytest.raises(ValueError, match="unknown tags"):
        load_openings_meta(path)


def test_overlapping_ranges_stop_the_seed(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        [
            {"from": "A00", "to": "A10", "family": "One", "style_tags": ["solid"]},
            {"from": "A05", "to": "A15", "family": "Two", "style_tags": ["sharp"]},
        ],
    )

    with pytest.raises(ValueError, match="claimed by two ranges"):
        load_openings_meta(path)


def test_rows_come_back_sorted(tmp_path: Path) -> None:
    """The migration inserts them in one statement; stable order keeps it diffable."""
    path = write(
        tmp_path,
        [
            {"from": "E00", "to": "E01", "family": "Late", "style_tags": ["closed"]},
            {"from": "A00", "to": "A01", "family": "Early", "style_tags": ["offbeat"]},
        ],
    )

    assert [row["eco"] for row in load_openings_meta(path)] == ["A00", "A01", "E00", "E01"]
