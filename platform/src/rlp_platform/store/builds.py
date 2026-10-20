"""Builds: pipeline runs on a repo commit."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from rlp_platform.models import Build, Repo
from rlp_platform.store import _base

UPDATABLE = frozenset({"workflow_run_url", "stages", "passed", "finished_at"})


def create(
    session: Session,
    *,
    repo_id: int,
    commit: str,
    branch: str,
    passed: bool,
    started_at: datetime,
    stages: dict[str, Any] | None = None,
    workflow_run_url: str | None = None,
    finished_at: datetime | None = None,
) -> Build:
    return _base.add(
        session,
        Build(
            repo_id=repo_id,
            commit=commit,
            branch=branch,
            passed=passed,
            started_at=started_at,
            stages=stages or {},
            workflow_run_url=workflow_run_url,
            finished_at=finished_at,
        ),
    )


def get(session: Session, build_id: int) -> Build:
    return _base.get(session, Build, build_id)


def list_(
    session: Session,
    *,
    repo_id: int | None = None,
    project_id: int | None = None,
    branch: str | None = None,
    limit: int | None = None,
) -> list[Build]:
    """Newest first; at most `limit` builds when it is set."""
    query = select(Build).order_by(Build.started_at.desc(), Build.id.desc())
    if repo_id is not None:
        query = query.where(Build.repo_id == repo_id)
    if project_id is not None:
        query = query.join(Repo, Build.repo_id == Repo.id).where(Repo.project_id == project_id)
    if branch is not None:
        query = query.where(Build.branch == branch)
    if limit is not None:
        query = query.limit(limit)
    return list(session.scalars(query))


def update(session: Session, build: Build, **fields: Any) -> Build:
    return _base.update(session, build, UPDATABLE, fields)
