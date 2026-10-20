"""Deployments: an artifact of a component in an environment."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from rlp_platform.models import Component, Deployment, Repo
from rlp_platform.store import _base

UPDATABLE = frozenset({"status"})


def create(session: Session, *, component_id: int, environment: str, artifact_id: int, status: str) -> Deployment:
    return _base.add(
        session,
        Deployment(component_id=component_id, environment=environment, artifact_id=artifact_id, status=status),
    )


def get(session: Session, deployment_id: int) -> Deployment:
    return _base.get(session, Deployment, deployment_id)


def list_(
    session: Session,
    *,
    component_id: int | None = None,
    repo_id: int | None = None,
    project_id: int | None = None,
    environment: str | None = None,
    limit: int | None = None,
) -> list[Deployment]:
    """Newest first; at most `limit` deployments when it is set."""
    query = select(Deployment).order_by(Deployment.deployed_at.desc(), Deployment.id.desc())
    if component_id is not None:
        query = query.where(Deployment.component_id == component_id)
    if repo_id is not None or project_id is not None:
        query = query.join(Component, Deployment.component_id == Component.id)
    if repo_id is not None:
        query = query.where(Component.repo_id == repo_id)
    if project_id is not None:
        query = query.join(Repo, Component.repo_id == Repo.id).where(Repo.project_id == project_id)
    if environment is not None:
        query = query.where(Deployment.environment == environment)
    if limit is not None:
        query = query.limit(limit)
    return list(session.scalars(query))


def update(session: Session, deployment: Deployment, **fields: Any) -> Deployment:
    return _base.update(session, deployment, UPDATABLE, fields)
