"""add puzzles

Positions mined from the player's own flagged moves during ingest. Keyed on the
same composite key as ``games`` and pointed at it by foreign key, so a puzzle
cannot outlive the game it came from and every fact about that game -- when,
which time control, against whom -- stays in one place.

Revision ID: b8e3f04c7d21
Revises: a5d2e6b91c47
Create Date: 2026-08-19 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b8e3f04c7d21"
down_revision: str | None = "a5d2e6b91c47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "puzzles",
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.String(length=16), nullable=False),
        sa.Column("ply", sa.SmallInteger(), nullable=False),
        sa.Column("fen", sa.Text(), nullable=False),
        sa.Column("move_played", sa.Text(), nullable=False),
        sa.Column("best_move", sa.Text(), nullable=False),
        sa.Column("best_move_san", sa.Text(), nullable=False),
        sa.Column("continuation", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("judgment", sa.Text(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("win_before", sa.SmallInteger(), nullable=False),
        sa.Column("win_after", sa.SmallInteger(), nullable=False),
        sa.Column("swing", sa.SmallInteger(), nullable=False),
        sa.Column(
            "legal_moves",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.ForeignKeyConstraint(
            ["player_id", "game_id"],
            ["games.player_id", "games.game_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("player_id", "game_id"),
    )
    # The report ranks on the swing within one player, and never across players.
    op.create_index("ix_puzzles_player_swing", "puzzles", ["player_id", "swing"])


def downgrade() -> None:
    op.drop_index("ix_puzzles_player_swing", table_name="puzzles")
    op.drop_table("puzzles")
