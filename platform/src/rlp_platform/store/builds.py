"""Builds: pipeline run attempts on a repo commit."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from rlp_platform.models import Build, Repo
from rlp_platform.store import _base

UPDATABLE = frozenset({"repo_id", "workflow_run_url", "stages", "passed", "finished_at"})


def create(
    session: Session,
    *,
    commit: str,
    branch: str,
    passed: bool,
    started_at: datetime,
    repo_id: int | None = None,
    repo_full_name: str | None = None,
    run_id: str | None = None,
    run_attempt: int = 1,
    stages: dict[str, Any] | None = None,
    workflow_run_url: str | None = None,
    finished_at: datetime | None = None,
) -> Build:
    """Pass `repo_id`, `repo_full_name` or both; without a `run_id`, the build gets one of its own."""
    if repo_full_name is None:
        if repo_id is None:
            raise ValueError("a build needs repo_id or repo_full_name")
        repo_full_name = _base.get(session, Repo, repo_id).full_name
    return _base.add(
        session,
        Build(
            repo_id=repo_id,
            repo_full_name=repo_full_name,
            run_id=run_id or f"manual-{uuid.uuid4().hex}",
            run_attempt=run_attempt,
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


def find_run(session: Session, repo_full_name: str, commit: str, run_id: str, run_attempt: int) -> Build | None:
    """The build of one run attempt, if it was reported."""
    query = select(Build).where(
        Build.repo_full_name == repo_full_name,
        Build.commit == commit,
        Build.run_id == run_id,
        Build.run_attempt == run_attempt,
    )
    return session.scalars(query).one_or_none()


def attach(session: Session, repo: Repo) -> list[Build]:
    """Link the builds reported under the repo's name before it was indexed to the repo."""
    query = select(Build).where(Build.repo_full_name == repo.full_name, Build.repo_id.is_(None))
    pending = list(session.scalars(query))
    for build in pending:
        build.repo_id = repo.id
    session.flush()
    return pending


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
