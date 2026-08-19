"""Puzzles built from the player's own mistakes.

The report's other sections describe a year; this one hands it back to be played
again. Every puzzle is a position the player actually reached and a move the
engine actually flagged, so the question is never "find the tactic" in the
abstract -- it is "find the one you missed, in the game you lost".

The pipeline is in three parts, deliberately separated by what they need:

``candidates``
    Pure. One exported game in, at most one puzzle-worthy position out. No
    database, no settings beyond thresholds, so every rule about what makes a
    position worth replaying is testable against a recorded game.
``pool``
    Collects candidates across a whole export under a fixed memory bound, and
    writes them. This is the only part that touches the session.
``app.report.sections.puzzles``
    Reads the stored pool back and picks the set a report shows.

Why the pool is stored at all: the per-ply ``analysis`` array is in hand exactly
once, while the export is streaming. Recomputing a puzzle later would mean a
second export of the same year -- minutes of Lichess's time to recover data we
had and threw away. See docs/BRIEF.md, which left this decision open.
"""

from app.puzzles.candidates import Candidate, after_move, find_candidate, piece_code
from app.puzzles.pool import CandidatePool, store

__all__ = [
    "Candidate",
    "CandidatePool",
    "after_move",
    "find_candidate",
    "piece_code",
    "store",
]
