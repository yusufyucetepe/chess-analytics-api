"""SQLAlchemy models.

Note on the games primary key: the brief specified the Lichess game id alone as
the PK. That breaks as soon as two players in our database faced each other --
one game id, but two rows, because colour, rating, rating_diff, result and the
analysis block are all *per player*. The PK is therefore composite,
(player_id, game_id), which keeps re-ingest just as idempotent.
"""

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

#: 2 added the puzzles section. Stored payloads are never migrated -- a report
#: is a snapshot and rebuilding one is a minute of work -- so anything reading
#: `sections` has to cope with a name that was not there when it was written.
REPORT_SCHEMA_VERSION = 2


class Color(enum.StrEnum):
    WHITE = "white"
    BLACK = "black"


class Result(enum.StrEnum):
    WIN = "win"
    LOSS = "loss"
    DRAW = "draw"


class ReportStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


def pg_enum(python_type: type[enum.Enum], name: str) -> Enum:
    """A Postgres enum storing member *values*, not names.

    SQLAlchemy defaults to storing ``.name``, which would put 'WHITE' in the
    column while every API payload says 'white'. Keeping them identical means
    no translation layer and readable ad-hoc SQL.
    """
    return Enum(
        python_type,
        name=name,
        native_enum=True,
        values_callable=lambda e: [m.value for m in e],
    )


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Lowercased username: Lichess is case-insensitive, this is the lookup key.
    username_lower: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(String(8))
    country: Mapped[str | None] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    #: When we last pulled this player's games from Lichess.
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Lichess's lifetime rated-game count as it stood at that fetch. Compared
    #: against the live profile to tell whether a re-export would find anything,
    #: so it is written only when an export finishes -- see ``ingest_player``.
    #: Null for a player whose games we have never successfully fetched.
    rated_games_count: Mapped[int | None] = mapped_column(Integer)
    #: Current ratings per perf type, straight from the Lichess profile.
    ratings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")

    games: Mapped[list["Game"]] = relationship(
        back_populates="player", cascade="all, delete-orphan", passive_deletes=True
    )
    reports: Mapped[list["Report"]] = relationship(
        back_populates="player", cascade="all, delete-orphan", passive_deletes=True
    )


class Game(Base):
    __tablename__ = "games"
    __table_args__ = (
        Index("ix_games_player_played_at", "player_id", "played_at"),
        Index("ix_games_player_eco", "player_id", "eco"),
        Index("ix_games_player_analysed", "player_id", "analysed"),
    )

    #: Lichess game id -- natural key, makes re-ingest idempotent.
    game_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), primary_key=True
    )

    played_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    perf: Mapped[str] = mapped_column(String(16))
    rated: Mapped[bool] = mapped_column(Boolean, default=True)
    color: Mapped[Color] = mapped_column(pg_enum(Color, "color"))

    opponent_name: Mapped[str | None] = mapped_column(String(64))
    opponent_rating: Mapped[int | None] = mapped_column(Integer)
    player_rating: Mapped[int | None] = mapped_column(Integer)
    rating_diff: Mapped[int | None] = mapped_column(Integer)
    #: Lichess's own flag for "this rating is not settled yet" -- it sets it
    #: while the rating deviation is high, which covers a new account starting
    #: from 1500 and an old one coming back after a long break. NULL means the
    #: row predates this column, not that the rating was settled.
    provisional: Mapped[bool | None] = mapped_column(Boolean)

    result: Mapped[Result] = mapped_column(pg_enum(Result, "result"))
    #: Lichess termination: mate, resign, outoftime, draw, stalemate, ...
    #: Unbounded on purpose. The vocabulary is Lichess's, not ours -- one member
    #: is already 25 characters (``insufficientMaterialClaim``) and nothing says
    #: the next one is shorter. Nothing filters or indexes on its length, and in
    #: Postgres an unbounded varchar costs the same as a capped one, so a length
    #: here buys nothing and one day loses a whole year's ingest to one game.
    status: Mapped[str] = mapped_column(Text)

    eco: Mapped[str | None] = mapped_column(String(3))
    opening_name: Mapped[str | None] = mapped_column(String(160))
    opening_ply: Mapped[int | None] = mapped_column(SmallInteger)

    #: Total plies in the game (half-moves), not full moves.
    moves_count: Mapped[int] = mapped_column(Integer, default=0)
    #: Wall-clock seconds from first move to last. Nullable because rows written
    #: before this column existed have none; a re-ingest fills them in.
    duration_s: Mapped[int | None] = mapped_column(Integer)
    clock_initial: Mapped[int | None] = mapped_column(Integer)
    clock_increment: Mapped[int | None] = mapped_column(Integer)

    #: Quality fields are populated only when the player had the game analysed.
    analysed: Mapped[bool] = mapped_column(Boolean, default=False)
    #: NUMERIC round-trips as Decimal, which is also what asyncpg binds.
    accuracy: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    acpl: Mapped[int | None] = mapped_column(Integer)
    inaccuracies: Mapped[int | None] = mapped_column(Integer)
    mistakes: Mapped[int | None] = mapped_column(Integer)
    blunders: Mapped[int | None] = mapped_column(Integer)

    #: First N plies in SAN. The rest of the move list is discarded on ingest --
    #: a year of blitz is a lot of rows for a tree that only needs the opening.
    opening_line: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")

    player: Mapped[Player] = relationship(back_populates="games")


class Puzzle(Base):
    """One position from the player's own games, worth playing again.

    Mined during ingest, while the per-ply ``analysis`` array is still in hand:
    recomputing one later would cost a second export of the whole year. The row
    carries only what the board needs -- everything about the *game* it came
    from is one join away on the composite key it shares with ``games``.

    One puzzle per game, so a single collapse cannot fill a report.
    """

    __tablename__ = "puzzles"
    __table_args__ = (
        ForeignKeyConstraint(
            ["player_id", "game_id"],
            ["games.player_id", "games.game_id"],
            ondelete="CASCADE",
        ),
        Index("ix_puzzles_player_swing", "player_id", "swing"),
    )

    player_id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[str] = mapped_column(String(16), primary_key=True)

    #: Index into the game's move list of the move that should not have been
    #: played. The position below is the one *before* it.
    ply: Mapped[int] = mapped_column(SmallInteger)
    fen: Mapped[str] = mapped_column(Text)
    #: SAN, for telling the player what they did.
    move_played: Mapped[str] = mapped_column(Text)
    #: UCI, because the board answers in squares.
    best_move: Mapped[str] = mapped_column(Text)
    best_move_san: Mapped[str] = mapped_column(Text)
    #: The engine's line after the best move, in SAN, excluding it.
    continuation: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    judgment: Mapped[str] = mapped_column(Text)
    #: Lichess's own sentence about the move.
    comment: Mapped[str] = mapped_column(Text)

    #: Share of the point, 0-100, from the player's side, either side of the move.
    win_before: Mapped[int] = mapped_column(SmallInteger)
    win_after: Mapped[int] = mapped_column(SmallInteger)
    #: What the move cost. The ranking key.
    swing: Mapped[int] = mapped_column(SmallInteger)

    #: ``{from_square: [to_square, ...]}``. Precomputed so the browser can
    #: refuse an illegal move without shipping a rules engine to do it.
    legal_moves: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")


class Report(Base):
    """A report row *is* the job record -- there is no separate jobs table."""

    __tablename__ = "reports"
    __table_args__ = (
        Index("ix_reports_player_status_created", "player_id", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), index=True)

    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    status: Mapped[ReportStatus] = mapped_column(
        pg_enum(ReportStatus, "report_status"),
        default=ReportStatus.PENDING,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: User-safe message; never a raw traceback.
    error: Mapped[str | None] = mapped_column(Text)

    games_total: Mapped[int] = mapped_column(Integer, default=0)
    games_analysed: Mapped[int] = mapped_column(Integer, default=0)

    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    schema_version: Mapped[int] = mapped_column(Integer, default=REPORT_SCHEMA_VERSION)

    player: Mapped[Player] = relationship(back_populates="reports")


class OpeningMeta(Base):
    """Static ECO metadata, seeded from a file. Never user data."""

    __tablename__ = "openings_meta"

    eco: Mapped[str] = mapped_column(String(3), primary_key=True)
    family: Mapped[str] = mapped_column(String(80))
    #: e.g. {sharp, gambit} or {solid, closed}. Drives the player-type classifier.
    style_tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")


class StyleReference(Base):
    """Where the middle of a population is, per time control and rating band.

    The player-type classifier calls somebody an All-Rounder when they sit near
    the centre of players like them, which needs to know where that centre is.
    Recomputed on a schedule from the reports already built, because the answer
    moves as more players are reported on -- and because nothing about it can be
    a fixed number: a 1200 blitz population flags and blunders its way to a
    different centre than a 2200 one, at no difference in style.
    """

    __tablename__ = "style_reference"

    perf: Mapped[str] = mapped_column(String(16), primary_key=True)
    #: The band's lower bound, e.g. 1400 for 1400-1599. See ``RATING_BAND``.
    rating_band: Mapped[int] = mapped_column(Integer, primary_key=True)

    players: Mapped[int] = mapped_column(Integer, default=0)
    #: The mean of the three axis scores across the sample.
    centroid: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    #: Distance from that centroid at each percentile, index 0..100. A player's
    #: own distance is read off this by interpolation, so the cut is always a
    #: share of the population rather than a number of points.
    distances: Mapped[list[float]] = mapped_column(ARRAY(Float), default=list, server_default="{}")
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
