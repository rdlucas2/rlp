"""Typed access to the project record. Later issues use these functions, not raw sessions.

Open a session with `session_factory(url)` and pass it to the functions in each
module. The functions flush, so generated ids and defaults are set on return,
but never commit: the caller owns the transaction.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from rlp_platform.db import make_engine
from rlp_platform.store import artifacts, builds, components, deployments, projects, repos, transitions
from rlp_platform.store._base import NotFound, UnknownField


def session_factory(url: str) -> sessionmaker[Session]:
    return sessionmaker(make_engine(url), expire_on_commit=False)


__all__ = [
    "NotFound",
    "UnknownField",
    "artifacts",
    "builds",
    "components",
    "deployments",
    "projects",
    "repos",
    "session_factory",
    "transitions",
]
