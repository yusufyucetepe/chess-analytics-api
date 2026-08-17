"""add style_reference

Revision ID: f3c17d0ab982
Revises: e7b21c94af60
Create Date: 2026-08-17 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f3c17d0ab982"
down_revision: str | None = "e7b21c94af60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Empty on creation. The table is a cache of a population the reports
    # themselves describe, so a scheduled job fills it; until a band has enough
    # players in it the classifier says so rather than guessing at a centre.
    op.create_table(
        "style_reference",
        sa.Column("perf", sa.String(length=16), nullable=False),
        sa.Column("rating_band", sa.Integer(), nullable=False),
        sa.Column("players", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "centroid", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column(
            "distances",
            postgresql.ARRAY(sa.Float()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("perf", "rating_band"),
    )


def downgrade() -> None:
    op.drop_table("style_reference")
