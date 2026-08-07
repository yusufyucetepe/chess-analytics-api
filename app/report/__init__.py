"""Building reports, and the report row that doubles as the job record."""

from app.report.builder import SECTIONS, build_payload, coverage, quality_reliable
from app.report.sections import build_sections
from app.report.service import report_window

__all__ = [
    "SECTIONS",
    "build_payload",
    "build_sections",
    "coverage",
    "quality_reliable",
    "report_window",
]
