"""Pipeline reports: builds keyed by run attempt, builds for repos not indexed yet, artifacts linked to builds.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-20 20:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Builds: who reported them (by name, so a build can arrive before the indexer sees the repo) and which run attempt.
    op.add_column("builds", sa.Column("repo_full_name", sa.String(length=200), nullable=True))
    op.add_column("builds", sa.Column("run_id", sa.String(length=100), nullable=True))
    op.add_column("builds", sa.Column("run_attempt", sa.Integer(), nullable=True))
    # Rows written before this revision had no run: each keeps its own id as one, so none collide.
    op.execute(
        "UPDATE builds SET repo_full_name = repos.full_name, run_id = 'build-' || builds.id, run_attempt = 1 "
        "FROM repos WHERE repos.id = builds.repo_id"
    )
    op.alter_column("builds", "repo_full_name", nullable=False)
    op.alter_column("builds", "run_id", nullable=False)
    op.alter_column("builds", "run_attempt", nullable=False)
    op.alter_column("builds", "repo_id", existing_type=sa.Integer(), nullable=True)
    op.create_unique_constraint(
        "uq_builds_repo_commit_run", "builds", ["repo_full_name", "commit", "run_id", "run_attempt"]
    )

    # Artifacts: the build that produced one, and its component's name until the component is indexed.
    op.add_column("artifacts", sa.Column("build_id", sa.Integer(), nullable=True))
    op.add_column("artifacts", sa.Column("component_name", sa.String(length=200), nullable=True))
    op.execute(
        "UPDATE artifacts SET component_name = components.name FROM components "
        "WHERE components.id = artifacts.component_id"
    )
    op.alter_column("artifacts", "component_id", existing_type=sa.Integer(), nullable=True)
    op.create_foreign_key(
        op.f("fk_artifacts_build_id_builds"), "artifacts", "builds", ["build_id"], ["id"], ondelete="RESTRICT"
    )
    op.create_index(op.f("ix_artifacts_build_id"), "artifacts", ["build_id"], unique=False)

    op.create_unique_constraint(
        "uq_deployments_component_env_artifact_at",
        "deployments",
        ["component_id", "environment", "artifact_id", "deployed_at"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_deployments_component_env_artifact_at", "deployments", type_="unique")

    op.drop_index(op.f("ix_artifacts_build_id"), table_name="artifacts")
    op.drop_constraint(op.f("fk_artifacts_build_id_builds"), "artifacts", type_="foreignkey")
    # Artifacts and builds the indexer never attached have no place in the older schema.
    op.execute("DELETE FROM deployments WHERE artifact_id IN (SELECT id FROM artifacts WHERE component_id IS NULL)")
    op.execute("DELETE FROM artifacts WHERE component_id IS NULL")
    op.alter_column("artifacts", "component_id", existing_type=sa.Integer(), nullable=False)
    op.drop_column("artifacts", "component_name")
    op.drop_column("artifacts", "build_id")

    op.drop_constraint("uq_builds_repo_commit_run", "builds", type_="unique")
    op.execute("DELETE FROM builds WHERE repo_id IS NULL")
    op.alter_column("builds", "repo_id", existing_type=sa.Integer(), nullable=False)
    op.drop_column("builds", "run_attempt")
    op.drop_column("builds", "run_id")
    op.drop_column("builds", "repo_full_name")
