"""add players.rated_games_count

Revision ID: d4e8a06f3b17
Revises: c3f9a71b52d8
Create Date: 2026-08-16 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d4e8a06f3b17"
down_revision: str | None = "c3f9a71b52d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable with no backfill, and null means "we do not know". There is
    # nothing to derive it from -- our own game count covers a 12-month window
    # while this is Lichess's lifetime figure, so the two are not comparable.
    # An unknown count reads as "they may have played something", which sends
    # the first request after this migration down the rebuild path and fills
    # the column in.
    op.add_column("players", sa.Column("rated_games_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("players", "rated_games_count")
