"""Lifecycle transitions: an append-only history of each project's phase changes and rejected attempts.

Recording a transition doesn't move the project; the lifecycle checks the entry
rules, records the transition and sets the phase together. A rejected attempt
is recorded with accepted=False and leaves the phase alone. Transitions are
never updated.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from rlp_platform.models import LifecycleTransition, Phase
from rlp_platform.store import _base


def create(
    session: Session,
    *,
    project_id: int,
    from_phase: Phase | None,
    to_phase: Phase,
    actor: str,
    reason: str = "",
    rule_results: list[dict[str, Any]] | None = None,
    accepted: bool = True,
) -> LifecycleTransition:
    return _base.add(
        session,
        LifecycleTransition(
            project_id=project_id,
            from_phase=from_phase,
            to_phase=to_phase,
            actor=actor,
            reason=reason,
            rule_results=rule_results or [],
            accepted=accepted,
        ),
    )


def get(session: Session, transition_id: int) -> LifecycleTransition:
    return _base.get(session, LifecycleTransition, transition_id)


def list_(session: Session, *, project_id: int, include_rejected: bool = False) -> list[LifecycleTransition]:
    """Oldest first: the project's history in order. Rejected attempts only when asked for."""
    query = select(LifecycleTransition).where(LifecycleTransition.project_id == project_id)
    if not include_rejected:
        query = query.where(LifecycleTransition.accepted.is_(True))
    return list(session.scalars(query.order_by(LifecycleTransition.at, LifecycleTransition.id)))
