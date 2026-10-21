"""Rejected transitions: the lifecycle history keeps moves an entry rule refused.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-21 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Every row written before this revision was an allowed move.
    op.add_column(
        "lifecycle_transitions",
        sa.Column("accepted", sa.Boolean(), server_default=sa.true(), nullable=False),
    )


def downgrade() -> None:
    op.execute("DELETE FROM lifecycle_transitions WHERE NOT accepted")
    op.drop_column("lifecycle_transitions", "accepted")
