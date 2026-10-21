"""Retirements: one per project, with its steps in the order they run.

The retirement workflow (rlp_platform.retirement) creates the record and moves
its steps along; nothing else writes here.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from rlp_platform.models import Retirement, RetirementStep
from rlp_platform.store import _base

STEP_UPDATABLE = frozenset({"status", "detail", "started_at"})


def create(session: Session, *, project_id: int, actor: str, reason: str, artifact_retention_days: int) -> Retirement:
    return _base.add(
        session,
        Retirement(project_id=project_id, actor=actor, reason=reason, artifact_retention_days=artifact_retention_days),
    )


def for_project(session: Session, project_id: int) -> Retirement | None:
    return session.scalars(select(Retirement).where(Retirement.project_id == project_id)).one_or_none()


def add_step(
    session: Session,
    retirement: Retirement,
    *,
    kind: str,
    repo_full_name: str | None = None,
    environment: str | None = None,
) -> RetirementStep:
    step = _base.add(
        session,
        RetirementStep(
            retirement_id=retirement.id,
            position=len(retirement.steps),
            kind=kind,
            repo_full_name=repo_full_name,
            environment=environment,
            status="pending",
        ),
    )
    retirement.steps.append(step)
    return step


def running_teardowns(session: Session, repo_full_name: str, environment: str) -> list[RetirementStep]:
    """Teardown steps waiting on this repo's environment to report back."""
    query = select(RetirementStep).where(
        RetirementStep.kind == "teardown",
        RetirementStep.status == "running",
        RetirementStep.repo_full_name == repo_full_name,
        RetirementStep.environment == environment,
    )
    return list(session.scalars(query))


def update_step(session: Session, step: RetirementStep, **fields: Any) -> RetirementStep:
    return _base.update(session, step, STEP_UPDATABLE, fields)


def finish(session: Session, retirement: Retirement, finished_at: Any) -> Retirement:
    retirement.finished_at = finished_at
    session.flush()
    return retirement
