"""add games.duration_s

Revision ID: b1c7f2d4e0a3
Revises: 86b45ae32f01
Create Date: 2026-08-07 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b1c7f2d4e0a3"
down_revision: str | None = "86b45ae32f01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable with no backfill: the export carries `lastMoveAt` and every
    # report re-ingests its window, so existing rows fill themselves in. There
    # is nothing in the database to derive it from in the meantime.
    op.add_column("games", sa.Column("duration_s", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("games", "duration_s")
