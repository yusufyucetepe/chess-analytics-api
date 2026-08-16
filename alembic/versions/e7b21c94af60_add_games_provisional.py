"""add games.provisional

Revision ID: e7b21c94af60
Revises: d4e8a06f3b17
Create Date: 2026-08-16 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e7b21c94af60"
down_revision: str | None = "d4e8a06f3b17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable with no backfill: whether a rating was provisional at the time is
    # only knowable from the export, and rows we already hold were parsed before
    # we read the flag. Progression treats NULL as settled, which is how those
    # rows already behaved, and a re-ingest fills them in.
    op.add_column("games", sa.Column("provisional", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("games", "provisional")
