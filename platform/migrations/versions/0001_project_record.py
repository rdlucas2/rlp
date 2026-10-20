"""Project record: projects, repos, components, artifacts, builds, deployments, lifecycle transitions.

Revision ID: 0001
Revises:
Create Date: 2026-10-20 04:47:26.082265
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PHASES = ("proposed", "designed", "scaffolded", "in-dev", "live", "deprecated", "retired")
# Created once, explicitly, and shared by every column that holds a phase.
PHASE = postgresql.ENUM(*PHASES, name="project_phase", create_type=False)


def upgrade() -> None:
    PHASE.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("owner", sa.String(length=200), nullable=False),
        sa.Column("phase", PHASE, server_default="proposed", nullable=False),
        sa.Column("dashboard_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
        sa.UniqueConstraint("name", name=op.f("uq_projects_name")),
    )
    op.create_table(
        "lifecycle_transitions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("from_phase", PHASE, nullable=True),
        sa.Column("to_phase", PHASE, nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("reason", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "rule_results",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_lifecycle_transitions_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lifecycle_transitions")),
    )
    op.create_index(op.f("ix_lifecycle_transitions_project_id"), "lifecycle_transitions", ["project_id"], unique=False)
    op.create_table(
        "repos",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("default_branch", sa.String(length=200), server_default="main", nullable=False),
        sa.Column("template_source", sa.Text(), nullable=True),
        sa.Column("template_version", sa.String(length=200), nullable=True),
        sa.Column("last_indexed_commit", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_repos_project_id_projects"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repos")),
        sa.UniqueConstraint("full_name", name=op.f("uq_repos_full_name")),
    )
    op.create_index(op.f("ix_repos_project_id"), "repos", ["project_id"], unique=False)
    op.create_table(
        "builds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("commit", sa.String(length=64), nullable=False),
        sa.Column("branch", sa.String(length=200), nullable=False),
        sa.Column("workflow_run_url", sa.Text(), nullable=True),
        sa.Column(
            "stages", sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"), nullable=False
        ),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["repo_id"], ["repos.id"], name=op.f("fk_builds_repo_id_repos"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_builds")),
    )
    op.create_index(op.f("ix_builds_repo_id"), "builds", ["repo_id"], unique=False)
    op.create_table(
        "components",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("language", sa.String(length=100), nullable=False),
        sa.Column("artifact_kind", sa.String(length=100), nullable=False),
        sa.Column("runtime_kind", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["repo_id"], ["repos.id"], name=op.f("fk_components_repo_id_repos"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_components")),
        sa.UniqueConstraint("repo_id", "name", name="uq_components_repo_id_name"),
    )
    op.create_index(op.f("ix_components_repo_id"), "components", ["repo_id"], unique=False)
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("component_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=100), nullable=False),
        sa.Column("reference", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["component_id"], ["components.id"], name=op.f("fk_artifacts_component_id_components"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifacts")),
    )
    op.create_index(op.f("ix_artifacts_component_id"), "artifacts", ["component_id"], unique=False)
    op.create_table(
        "deployments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("component_id", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(length=100), nullable=False),
        sa.Column("artifact_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("deployed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["artifact_id"], ["artifacts.id"], name=op.f("fk_deployments_artifact_id_artifacts"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["component_id"],
            ["components.id"],
            name=op.f("fk_deployments_component_id_components"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployments")),
    )
    op.create_index(op.f("ix_deployments_artifact_id"), "deployments", ["artifact_id"], unique=False)
    op.create_index(op.f("ix_deployments_component_id"), "deployments", ["component_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_deployments_component_id"), table_name="deployments")
    op.drop_index(op.f("ix_deployments_artifact_id"), table_name="deployments")
    op.drop_table("deployments")
    op.drop_index(op.f("ix_artifacts_component_id"), table_name="artifacts")
    op.drop_table("artifacts")
    op.drop_index(op.f("ix_components_repo_id"), table_name="components")
    op.drop_table("components")
    op.drop_index(op.f("ix_builds_repo_id"), table_name="builds")
    op.drop_table("builds")
    op.drop_index(op.f("ix_repos_project_id"), table_name="repos")
    op.drop_table("repos")
    op.drop_index(op.f("ix_lifecycle_transitions_project_id"), table_name="lifecycle_transitions")
    op.drop_table("lifecycle_transitions")
    op.drop_table("projects")
    PHASE.drop(op.get_bind(), checkfirst=True)
