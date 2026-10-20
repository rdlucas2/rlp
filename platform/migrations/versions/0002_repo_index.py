"""Repo index: the last index error on a repo, and removed components.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("repos", sa.Column("index_error", sa.Text(), nullable=True))
    op.add_column("components", sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("components", "removed_at")
    op.drop_column("repos", "index_error")
