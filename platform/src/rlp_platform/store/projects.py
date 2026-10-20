"""Projects: the root of the record."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from rlp_platform.models import Phase, Project
from rlp_platform.store import _base

UPDATABLE = frozenset({"description", "owner", "phase", "dashboard_url"})


def create(
    session: Session,
    *,
    name: str,
    owner: str,
    description: str = "",
    phase: Phase = Phase.PROPOSED,
    dashboard_url: str | None = None,
) -> Project:
    return _base.add(
        session,
        Project(name=name, owner=owner, description=description, phase=phase, dashboard_url=dashboard_url),
    )


def get(session: Session, project_id: int) -> Project:
    return _base.get(session, Project, project_id)


def get_by_name(session: Session, name: str) -> Project:
    project = session.scalars(select(Project).where(Project.name == name)).one_or_none()
    if project is None:
        raise _base.NotFound(f"project {name!r} not found")
    return project


def list_(session: Session, *, phase: Phase | None = None) -> list[Project]:
    query = select(Project).order_by(Project.name)
    if phase is not None:
        query = query.where(Project.phase == phase)
    return list(session.scalars(query))


def update(session: Session, project: Project, **fields: Any) -> Project:
    """Change the project's details. The lifecycle sets `phase` when it records a transition."""
    return _base.update(session, project, UPDATABLE, fields)
