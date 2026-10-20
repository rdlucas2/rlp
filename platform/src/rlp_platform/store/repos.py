"""Repos: git repositories, each linked to one project."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from rlp_platform.models import Repo
from rlp_platform.store import _base

UPDATABLE = frozenset({"project_id", "default_branch", "template_source", "template_version", "last_indexed_commit"})


def create(
    session: Session,
    *,
    full_name: str,
    project_id: int,
    default_branch: str = "main",
    template_source: str | None = None,
    template_version: str | None = None,
    last_indexed_commit: str | None = None,
) -> Repo:
    return _base.add(
        session,
        Repo(
            full_name=full_name,
            project_id=project_id,
            default_branch=default_branch,
            template_source=template_source,
            template_version=template_version,
            last_indexed_commit=last_indexed_commit,
        ),
    )


def get(session: Session, repo_id: int) -> Repo:
    return _base.get(session, Repo, repo_id)


def get_by_full_name(session: Session, full_name: str) -> Repo:
    repo = session.scalars(select(Repo).where(Repo.full_name == full_name)).one_or_none()
    if repo is None:
        raise _base.NotFound(f"repo {full_name!r} not found")
    return repo


def list_(session: Session, *, project_id: int | None = None) -> list[Repo]:
    query = select(Repo).order_by(Repo.full_name)
    if project_id is not None:
        query = query.where(Repo.project_id == project_id)
    return list(session.scalars(query))


def update(session: Session, repo: Repo, **fields: Any) -> Repo:
    return _base.update(session, repo, UPDATABLE, fields)
