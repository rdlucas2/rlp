"""Retirement: a project's retirement and its steps, and artifacts deleted from their registry.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-21 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("artifacts", sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "retirements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("reason", sa.Text(), server_default="", nullable=False),
        sa.Column("artifact_retention_days", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_retirements_project_id_projects"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_retirements")),
        sa.UniqueConstraint("project_id", name=op.f("uq_retirements_project_id")),
    )
    op.create_table(
        "retirement_steps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("retirement_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("repo_full_name", sa.String(length=200), nullable=True),
        sa.Column("environment", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=50), server_default="pending", nullable=False),
        sa.Column("detail", sa.Text(), server_default="", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["retirement_id"],
            ["retirements.id"],
            name=op.f("fk_retirement_steps_retirement_id_retirements"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_retirement_steps")),
        sa.UniqueConstraint("retirement_id", "position", name="uq_retirement_steps_retirement_id_position"),
    )
    op.create_index(op.f("ix_retirement_steps_retirement_id"), "retirement_steps", ["retirement_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_retirement_steps_retirement_id"), table_name="retirement_steps")
    op.drop_table("retirement_steps")
    op.drop_table("retirements")
    op.drop_column("artifacts", "expired_at")
