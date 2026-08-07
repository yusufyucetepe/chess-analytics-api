"""The ECO range file and its expansion into openings_meta rows."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.db.seed import STYLE_TAGS, eco_codes, load_openings_meta
from app.report.player_type_weights import TAG_AXES

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

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


def test_the_classifier_has_an_opinion_about_every_tag_in_the_vocabulary() -> None:
    """`seed.py` owns the vocabulary so a migration can import it without dragging
    in the report package. This is the check that keeps the two in step."""
    assert set(TAG_AXES) == STYLE_TAGS


def test_the_data_file_uses_the_whole_vocabulary() -> None:
    """A tag nothing is tagged with is dead weight in the weights module."""
    used = {tag for row in load_openings_meta() for tag in row["style_tags"]}

    assert used == STYLE_TAGS


def test_the_seed_module_imports_on_its_own() -> None:
    """A migration imports `app.db.seed` directly; it must not need `app.report`.

    Regression test, run in a clean interpreter because import cycles only show
    up on a fresh module table. The two briefly imported each other, which made
    `alembic upgrade head` fail on any path that had not already loaded
    `app.report` by chance -- and the whole test suite hid it, because pytest
    always imports `app.report` first.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import app.db.seed; print(len(app.db.seed.load_openings_meta()))"],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "500"
