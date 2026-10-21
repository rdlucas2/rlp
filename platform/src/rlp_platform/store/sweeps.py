"""Retirement sweep runs, and the flags the sweep keeps on idle deprecated projects.

The retirement sweep (rlp_platform.sweep) writes here; the API reads the runs back.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from rlp_platform.models import RetirementFlag, SweepRun
from rlp_platform.store import _base

FLAG_UPDATABLE = frozenset({"flagged_at", "detail", "issue_repo", "issue_number", "issue_url", "cleared_at"})


def record_run(
    session: Session,
    *,
    actor: str,
    idle_days: int,
    window_start: datetime,
    window_end: datetime,
    results: list[dict[str, Any]],
    finished_at: datetime,
) -> SweepRun:
    return _base.add(
        session,
        SweepRun(
            actor=actor,
            idle_days=idle_days,
            window_start=window_start,
            window_end=window_end,
            results=results,
            finished_at=finished_at,
        ),
    )


def list_runs(session: Session, *, limit: int | None = None) -> list[SweepRun]:
    """Newest first; at most `limit` runs when it is set."""
    query = select(SweepRun).order_by(SweepRun.started_at.desc(), SweepRun.id.desc())
    if limit is not None:
        query = query.limit(limit)
    return list(session.scalars(query))


def flag_for(session: Session, project_id: int) -> RetirementFlag | None:
    return session.scalars(select(RetirementFlag).where(RetirementFlag.project_id == project_id)).one_or_none()


def open_flags(session: Session) -> list[RetirementFlag]:
    """Flags not cleared yet."""
    return list(session.scalars(select(RetirementFlag).where(RetirementFlag.cleared_at.is_(None))))


def create_flag(session: Session, *, project_id: int, flagged_at: datetime, detail: str) -> RetirementFlag:
    return _base.add(session, RetirementFlag(project_id=project_id, flagged_at=flagged_at, detail=detail))


def update_flag(session: Session, flag: RetirementFlag, **fields: Any) -> RetirementFlag:
    return _base.update(session, flag, FLAG_UPDATABLE, fields)
