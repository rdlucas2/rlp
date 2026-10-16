"""The commit that "new code" is measured against.

The sast stage reports only findings introduced since this commit, and the
coverage stage checks coverage of the lines changed since it. It is the
merge base of HEAD and:

- $RLP_BASELINE, when set: golden.yml sets it to the pull request's base
  commit, or to the commit before a push. Empty or all zeros (a new branch)
  means there is no baseline.
- otherwise the remote's default branch (origin/HEAD, then origin/main and
  origin/master), so a local run compares against what the PR would.

No baseline means the whole repo counts as new: the sast stage scans
everything and the new-code coverage check is skipped.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ENV = "RLP_BASELINE"


class BaselineError(Exception):
    """$RLP_BASELINE names a commit that isn't in this clone."""


def _git(root: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    except OSError:
        return None
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def resolve(root: Path) -> str | None:
    """The baseline commit SHA, or None when there is none."""
    if _git(root, "rev-parse", "--is-inside-work-tree") != "true":
        return None
    if ENV in os.environ:
        ref = os.environ[ENV].strip()
        if not ref or set(ref) == {"0"}:
            return None
        base = _git(root, "merge-base", "HEAD", ref)
        if base is None:
            raise BaselineError(f"{ENV}={ref} is not in this clone's history; check out with full history (fetch-depth: 0)")
        return base
    candidates = [_git(root, "rev-parse", "--abbrev-ref", "origin/HEAD"), "origin/main", "origin/master"]
    for ref in candidates:
        if ref:
            base = _git(root, "merge-base", "HEAD", ref)
            if base:
                return base
    return None
