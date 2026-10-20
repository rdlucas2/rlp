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
    DateTime,
    Enum,
    ForeignKey,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
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
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    repo: Mapped[Repo] = relationship(back_populates="components")


class Artifact(Base):
    """Something a component's package verb produced: an image digest, a zip URI, a jar."""

    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    component_id: Mapped[int] = mapped_column(_restrict("components.id"), index=True)
    kind: Mapped[str] = mapped_column(String(100))
    reference: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Build(Base):
    """One pipeline run on a repo commit, with each gate stage's result."""

    __tablename__ = "builds"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(_restrict("repos.id"), index=True)
    commit: Mapped[str] = mapped_column(String(64))
    branch: Mapped[str] = mapped_column(String(200))
    workflow_run_url: Mapped[str | None] = mapped_column(Text)
    stages: Mapped[dict[str, Any]] = mapped_column(JsonDocument, default=dict)
    passed: Mapped[bool]
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Deployment(Base):
    """An artifact of a component deployed to an environment."""

    __tablename__ = "deployments"

    id: Mapped[int] = mapped_column(primary_key=True)
    component_id: Mapped[int] = mapped_column(_restrict("components.id"), index=True)
    environment: Mapped[str] = mapped_column(String(100))
    artifact_id: Mapped[int] = mapped_column(_restrict("artifacts.id"), index=True)
    status: Mapped[str] = mapped_column(String(50))
    deployed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LifecycleTransition(Base):
    """A project's move from one phase to the next, with who asked, why and what the rules said."""

    __tablename__ = "lifecycle_transitions"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(_restrict("projects.id"), index=True)
    # None for the first transition: the project had no phase before it.
    from_phase: Mapped[Phase | None] = mapped_column(PhaseType)
    to_phase: Mapped[Phase] = mapped_column(PhaseType)
    actor: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(Text, default="", server_default="")
    rule_results: Mapped[list[dict[str, Any]]] = mapped_column(JsonDocument, default=list)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
