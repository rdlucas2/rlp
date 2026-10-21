"""The project record: SQLAlchemy models for every table the platform keeps.

A project is the root record. Repos link to it (by `metadata.project` in their
rlp.yaml), components belong to a repo, and builds, artifacts and deployments
hang off repos and components. Every foreign key is RESTRICT on delete: removing
a record that others still point at fails instead of cascading.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Stable constraint names, so migrations can refer to them on every database.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# JSONB on Postgres; plain JSON anywhere else.
JsonDocument = JSON().with_variant(JSONB(), "postgresql")


class Phase(enum.StrEnum):
    """Lifecycle phases of a project, in order."""

    PROPOSED = "proposed"
    DESIGNED = "designed"
    SCAFFOLDED = "scaffolded"
    IN_DEV = "in-dev"
    LIVE = "live"
    DEPRECATED = "deprecated"
    RETIRED = "retired"


def _phase_enum() -> Enum:
    # Store the values ("in-dev"), not the member names ("IN_DEV").
    return Enum(Phase, name="project_phase", values_callable=lambda members: [m.value for m in members])


PhaseType = _phase_enum()


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def _updated_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


def _restrict(target: str) -> ForeignKey:
    return ForeignKey(target, ondelete="RESTRICT")


class Project(Base):
    """A project: the root of the record. It spans one or more repos."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    owner: Mapped[str] = mapped_column(String(200))
    phase: Mapped[Phase] = mapped_column(PhaseType, default=Phase.PROPOSED, server_default=Phase.PROPOSED.value)
    dashboard_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    repos: Mapped[list[Repo]] = relationship(back_populates="project", passive_deletes="all")


class Repo(Base):
    """A git repository, linked to its project by `metadata.project` in rlp.yaml."""

    __tablename__ = "repos"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200), unique=True)
    project_id: Mapped[int] = mapped_column(_restrict("projects.id"), index=True)
    default_branch: Mapped[str] = mapped_column(String(200), default="main", server_default="main")
    template_source: Mapped[str | None] = mapped_column(Text)
    template_version: Mapped[str | None] = mapped_column(String(200))
    last_indexed_commit: Mapped[str | None] = mapped_column(String(64))
    # Why the last index attempt failed (an invalid or missing rlp.yaml); None once one succeeds.
    index_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    project: Mapped[Project] = relationship(back_populates="repos")
    components: Mapped[list[Component]] = relationship(back_populates="repo", passive_deletes="all")


class Component(Base):
    """A deployable unit inside a repo, as declared under `components` in rlp.yaml."""

    __tablename__ = "components"
    __table_args__ = (UniqueConstraint("repo_id", "name", name="uq_components_repo_id_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(_restrict("repos.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    language: Mapped[str] = mapped_column(String(100))
    artifact_kind: Mapped[str] = mapped_column(String(100))
    runtime_kind: Mapped[str] = mapped_column(String(100))
    # Set when the component left rlp.yaml. It is kept, not deleted, so its builds and deployments keep their history.
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    repo: Mapped[Repo] = relationship(back_populates="components")


class Artifact(Base):
    """Something a component's package verb produced: an image digest, a zip URI, a jar.

    An artifact a pipeline run reported links to its build. Until the indexer
    has seen the repo and the component, `component_id` is None and
    `component_name` says which component it belongs to.
    """

    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    component_id: Mapped[int | None] = mapped_column(_restrict("components.id"), index=True)
    build_id: Mapped[int | None] = mapped_column(_restrict("builds.id"), index=True)
    component_name: Mapped[str | None] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(100))
    reference: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Build(Base):
    """One pipeline run attempt on a repo commit, with each gate stage's result.

    A run attempt is recorded once: (repo, commit, run id, run attempt) is unique.
    A build reported for a repo the indexer hasn't seen yet keeps `repo_id`
    None and is attached when the repo is first indexed.
    """

    __tablename__ = "builds"
    __table_args__ = (
        UniqueConstraint("repo_full_name", "commit", "run_id", "run_attempt", name="uq_builds_repo_commit_run"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int | None] = mapped_column(_restrict("repos.id"), index=True)
    # OWNER/NAME as the pipeline reported it.
    repo_full_name: Mapped[str] = mapped_column(String(200))
    commit: Mapped[str] = mapped_column(String(64))
    branch: Mapped[str] = mapped_column(String(200))
    # GITHUB_RUN_ID and GITHUB_RUN_ATTEMPT; "local" and 1 outside Actions.
    run_id: Mapped[str] = mapped_column(String(100))
    run_attempt: Mapped[int] = mapped_column(Integer)
    workflow_run_url: Mapped[str | None] = mapped_column(Text)
    stages: Mapped[dict[str, Any]] = mapped_column(JsonDocument, default=dict)
    passed: Mapped[bool]
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Deployment(Base):
    """An artifact of a component deployed to an environment; one per component, environment, artifact and time."""

    __tablename__ = "deployments"
    __table_args__ = (
        UniqueConstraint(
            "component_id", "environment", "artifact_id", "deployed_at", name="uq_deployments_component_env_artifact_at"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    component_id: Mapped[int] = mapped_column(_restrict("components.id"), index=True)
    environment: Mapped[str] = mapped_column(String(100))
    artifact_id: Mapped[int] = mapped_column(_restrict("artifacts.id"), index=True)
    status: Mapped[str] = mapped_column(String(50))
    deployed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LifecycleTransition(Base):
    """A project's move from one phase to the next, or a rejected attempt: who asked, why, what the rules said."""

    __tablename__ = "lifecycle_transitions"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(_restrict("projects.id"), index=True)
    # None for the first transition: the project had no phase before it.
    from_phase: Mapped[Phase | None] = mapped_column(PhaseType)
    to_phase: Mapped[Phase] = mapped_column(PhaseType)
    actor: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(Text, default="", server_default="")
    rule_results: Mapped[list[dict[str, Any]]] = mapped_column(JsonDocument, default=list)
    # False for a move an entry rule rejected: the phase stayed, and rule_results says why.
    accepted: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
