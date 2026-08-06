"""Building reports, and the report row that doubles as the job record."""

from app.report.builder import SECTIONS, build_payload, coverage
from app.report.service import report_window

__all__ = ["SECTIONS", "build_payload", "coverage", "report_window"]
