"""Components: the deployable units a repo's rlp.yaml declares."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from rlp_platform.models import Component, Repo
from rlp_platform.store import _base

UPDATABLE = frozenset({"language", "artifact_kind", "runtime_kind", "removed_at"})


def create(
    session: Session, *, repo_id: int, name: str, language: str, artifact_kind: str, runtime_kind: str
) -> Component:
    return _base.add(
        session,
        Component(
            repo_id=repo_id, name=name, language=language, artifact_kind=artifact_kind, runtime_kind=runtime_kind
        ),
    )


def get(session: Session, component_id: int) -> Component:
    return _base.get(session, Component, component_id)


def get_by_name(session: Session, repo_id: int, name: str) -> Component:
    query = select(Component).where(Component.repo_id == repo_id, Component.name == name)
    component = session.scalars(query).one_or_none()
    if component is None:
        raise _base.NotFound(f"component {name!r} not found in repo {repo_id}")
    return component


def list_(
    session: Session, *, repo_id: int | None = None, project_id: int | None = None, include_removed: bool = True
) -> list[Component]:
    """By repo, then name. `include_removed=False` leaves out components that left their rlp.yaml."""
    query = select(Component).order_by(Component.repo_id, Component.name)
    if not include_removed:
        query = query.where(Component.removed_at.is_(None))
    if repo_id is not None:
        query = query.where(Component.repo_id == repo_id)
    if project_id is not None:
        query = query.join(Repo, Component.repo_id == Repo.id).where(Repo.project_id == project_id)
    return list(session.scalars(query))


def update(session: Session, component: Component, **fields: Any) -> Component:
    return _base.update(session, component, UPDATABLE, fields)
