"""Artifacts: what a component's package verb produced."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from rlp_platform.models import Artifact
from rlp_platform.store import _base

UPDATABLE = frozenset({"expires_at"})


def create(
    session: Session, *, component_id: int, kind: str, reference: str, expires_at: datetime | None = None
) -> Artifact:
    return _base.add(
        session, Artifact(component_id=component_id, kind=kind, reference=reference, expires_at=expires_at)
    )


def get(session: Session, artifact_id: int) -> Artifact:
    return _base.get(session, Artifact, artifact_id)


def list_(session: Session, *, component_id: int | None = None) -> list[Artifact]:
    """Newest first."""
    query = select(Artifact).order_by(Artifact.created_at.desc(), Artifact.id.desc())
    if component_id is not None:
        query = query.where(Artifact.component_id == component_id)
    return list(session.scalars(query))


def update(session: Session, artifact: Artifact, **fields: Any) -> Artifact:
    return _base.update(session, artifact, UPDATABLE, fields)
