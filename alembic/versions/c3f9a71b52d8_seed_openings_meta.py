"""seed openings_meta

Revision ID: c3f9a71b52d8
Revises: b1c7f2d4e0a3
Create Date: 2026-08-07 16:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.seed import load_openings_meta

revision: str = "c3f9a71b52d8"
down_revision: str | None = "b1c7f2d4e0a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Deliberately imports the loader rather than pasting 500 rows in here. The data
# is ours, not a user's, and the whole table is rewritten on conflict -- so a
# fresh `upgrade head` and a re-run land on the same state either way.
openings_meta = sa.table(
    "openings_meta",
    sa.column("eco", sa.String),
    sa.column("family", sa.String),
    sa.column("style_tags", postgresql.ARRAY(sa.Text)),
)


def upgrade() -> None:
    rows = load_openings_meta()
    insert = postgresql.insert(openings_meta).values(rows)
    op.get_bind().execute(
        insert.on_conflict_do_update(
            index_elements=["eco"],
            set_={"family": insert.excluded.family, "style_tags": insert.excluded.style_tags},
        )
    )


def downgrade() -> None:
    op.execute(sa.delete(openings_meta))
