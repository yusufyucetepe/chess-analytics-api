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
    ForeignKey,
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

REPORT_SCHEMA_VERSION = 1


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

    result: Mapped[Result] = mapped_column(pg_enum(Result, "result"))
    #: Lichess termination: mate, resign, outoftime, draw, stalemate, ...
    status: Mapped[str] = mapped_column(String(24))

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
