"""Artifacts: what a component's package verb produced."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from rlp_platform.models import Artifact, Build, Component, Repo
from rlp_platform.store import _base

UPDATABLE = frozenset({"component_id", "expires_at", "expired_at"})


def create(
    session: Session,
    *,
    kind: str,
    reference: str,
    component_id: int | None = None,
    component_name: str | None = None,
    build_id: int | None = None,
    expires_at: datetime | None = None,
) -> Artifact:
    """Pass `component_id`, or `component_name` for a component the indexer hasn't seen yet."""
    if component_name is None:
        if component_id is None:
            raise ValueError("an artifact needs component_id or component_name")
        component_name = _base.get(session, Component, component_id).name
    return _base.add(
        session,
        Artifact(
            component_id=component_id,
            component_name=component_name,
            build_id=build_id,
            kind=kind,
            reference=reference,
            expires_at=expires_at,
        ),
    )


def get(session: Session, artifact_id: int) -> Artifact:
    return _base.get(session, Artifact, artifact_id)


def list_(
    session: Session, *, component_id: int | None = None, build_id: int | None = None, kind: str | None = None
) -> list[Artifact]:
    """Newest first."""
    query = select(Artifact).order_by(Artifact.created_at.desc(), Artifact.id.desc())
    if component_id is not None:
        query = query.where(Artifact.component_id == component_id)
    if build_id is not None:
        query = query.where(Artifact.build_id == build_id)
    if kind is not None:
        query = query.where(Artifact.kind == kind)
    return list(session.scalars(query))


def unattached(session: Session, *, repo_id: int) -> list[Artifact]:
    """Artifacts of the repo's builds whose component wasn't indexed when they were reported."""
    query = (
        select(Artifact)
        .join(Build, Artifact.build_id == Build.id)
        .where(Build.repo_id == repo_id, Artifact.component_id.is_(None))
        .order_by(Artifact.id)
    )
    return list(session.scalars(query))


def find(session: Session, *, component_id: int, kind: str, reference: str) -> Artifact | None:
    """The newest artifact of the component with exactly this reference."""
    query = (
        select(Artifact)
        .where(Artifact.component_id == component_id, Artifact.kind == kind, Artifact.reference == reference)
        .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        .limit(1)
    )
    return session.scalars(query).first()


def update(session: Session, artifact: Artifact, **fields: Any) -> Artifact:
    return _base.update(session, artifact, UPDATABLE, fields)


def for_project(session: Session, project_id: int) -> list[Artifact]:
    """Every artifact of the project's repos: by its component, or by its build when the component isn't indexed."""
    components = select(Component.id).join(Repo, Component.repo_id == Repo.id).where(Repo.project_id == project_id)
    project_builds = select(Build.id).join(Repo, Build.repo_id == Repo.id).where(Repo.project_id == project_id)
    query = (
        select(Artifact)
        .where(or_(Artifact.component_id.in_(components), Artifact.build_id.in_(project_builds)))
        .order_by(Artifact.id)
    )
    return list(session.scalars(query))


def due(session: Session, now: datetime) -> list[Artifact]:
    """Artifacts past their expires_at that are not deleted yet, oldest first."""
    query = (
        select(Artifact)
        .where(Artifact.expires_at.is_not(None), Artifact.expires_at <= now, Artifact.expired_at.is_(None))
        .order_by(Artifact.expires_at, Artifact.id)
    )
    return list(session.scalars(query))
