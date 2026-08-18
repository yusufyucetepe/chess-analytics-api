"""Widen games.status: Lichess has a 25-character status

``insufficientMaterialClaim`` is 25 characters and the column was 24, so a
single game with that ending failed the whole batch insert -- and with it the
player's entire year, since the export is one transaction per batch.

Widened to unbounded rather than to a bigger number: the vocabulary belongs to
Lichess, and the next value they add is not ours to size either.

Revision ID: a5d2e6b91c47
Revises: f3c17d0ab982
"""

import sqlalchemy as sa

from alembic import op

revision = "a5d2e6b91c47"
down_revision = "f3c17d0ab982"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("games", "status", type_=sa.Text(), existing_nullable=False)


def downgrade() -> None:
    # Truncates anything longer than the old cap, which is the only way back to
    # a column that could not hold it.
    op.execute("UPDATE games SET status = left(status, 24)")
    op.alter_column("games", "status", type_=sa.String(24), existing_nullable=False)
