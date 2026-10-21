"""What `rlp pipeline report` sends the platform: a build, or the deployments of a deploy stage.

A build is the run attempt's `.rlp/pipeline/results.json`, plus what `rlp
publish` recorded in `.rlp/pipeline/published.json`. The run attempt is
identified by the repo, the commit, GITHUB_RUN_ID and GITHUB_RUN_ATTEMPT
("local" and 1 outside Actions), so reporting it again updates the same build.

A deployment is one component of the deploy stage: its environment, the image
it deployed, whether its readiness check passed, and when the stage ran.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rlp_cli import artifacts, contract, infra, pipeline, publish

LOCAL_RUN = "local"
DEPLOY_STATUS = {"passed": "succeeded", "failed": "failed"}
# owner/name at the end of an https, ssh or scp-style remote URL.
REMOTE = re.compile(r"[:/](?P<owner>[^/:]+)/(?P<name>[^/]+?)(?:\.git)?/?$")


class ReportError(Exception):
    """The report can't be put together: the repo or commit is unknown, or results.json is unreadable."""


@dataclass(frozen=True)
class Run:
    repo: str
    commit: str
    branch: str
    run_id: str
    run_attempt: int
    run_url: str | None

    def key(self) -> dict[str, Any]:
        return {"repo": self.repo, "commit": self.commit, "run_id": self.run_id, "run_attempt": self.run_attempt}


def _git(root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    except OSError:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def repo_from_remote(url: str) -> str:
    """OWNER/NAME from a git remote URL; empty when it doesn't end in one."""
    match = REMOTE.search(url.strip())
    return f"{match['owner']}/{match['name']}" if match else ""


def resolve_run(
    root: Path,
    environ: Mapping[str, str] | None = None,
    *,
    repo: str = "",
    commit: str = "",
    branch: str = "",
) -> Run:
    """The run attempt to report: options first, then the GitHub Actions variables, then git."""
    env = os.environ if environ is None else environ
    repo = repo or env.get("GITHUB_REPOSITORY", "") or repo_from_remote(_git(root, "remote", "get-url", "origin"))
    if not repo:
        raise ReportError("no repo: pass --repo OWNER/NAME (or set GITHUB_REPOSITORY)")
    commit = commit or env.get("GITHUB_SHA", "") or _git(root, "rev-parse", "HEAD")
    if not commit:
        raise ReportError("no commit: pass --commit (or set GITHUB_SHA)")
    head = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    branch = branch or env.get("GITHUB_HEAD_REF", "") or env.get("GITHUB_REF_NAME", "") or (head if head != "HEAD" else "")
    if not branch:
        raise ReportError("no branch: pass --branch (or set GITHUB_REF_NAME)")
    run_id = env.get("GITHUB_RUN_ID", "") or LOCAL_RUN
    try:
        attempt = int(env.get("GITHUB_RUN_ATTEMPT", "") or 1)
    except ValueError as err:
        raise ReportError(f"GITHUB_RUN_ATTEMPT is not a number: {env['GITHUB_RUN_ATTEMPT']!r}") from err
    server = env.get("GITHUB_SERVER_URL", "")
    url = f"{server}/{repo}/actions/runs/{run_id}/attempts/{attempt}" if server and run_id != LOCAL_RUN else None
    return Run(repo=repo, commit=commit, branch=branch, run_id=run_id, run_attempt=attempt, run_url=url)


def _results(root: Path) -> dict[str, dict]:
    try:
        return pipeline.load_results(root)
    except (OSError, ValueError, KeyError, TypeError) as err:
        raise ReportError(f"cannot read {pipeline.RESULTS.as_posix()}: {err}") from err


def build(root: Path, run: Run) -> dict[str, Any] | None:
    """The POST /api/v1/builds body; None when there is nothing to report (no results and nothing published)."""
    results = list(_results(root).values())
    try:
        published = publish.load(root / pipeline.PUBLISHED)
    except (OSError, ValueError) as err:
        raise ReportError(f"cannot read {pipeline.PUBLISHED.as_posix()}: {err}") from err
    found = [
        {"component": r["component"], "kind": r["kind"], "reference": r["reference"]}
        for r in published
        if r.get("status") == "published" and r.get("reference") and r.get("component") and r.get("kind")
    ]
    if not results and not found:
        return None
    return {
        "repo": run.repo,
        "commit": run.commit,
        "branch": run.branch,
        "run_id": run.run_id,
        "run_attempt": run.run_attempt,
        "run_url": run.run_url,
        "results": results,
        "artifacts": found,
    }


def _reference(root: Path, doc: dict, component: dict, registry: str) -> str:
    """What the deploy stage deployed, as `rlp infra render` names it: <registry>/<image>:<version>."""
    image = f"{artifacts.image_name(doc, component)}:{artifacts.version(root)}"
    return f"{registry.rstrip('/')}/{image}" if registry else image


def deployments(
    root: Path,
    run: Run,
    environ: Mapping[str, str] | None = None,
    *,
    deploy_env: str = "",
    only: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """The POST /api/v1/deployments bodies for the recorded deploy stage; none when it didn't run or skipped."""
    env = os.environ if environ is None else environ
    stage = _results(root).get("deploy")
    if stage is None or stage.get("status") not in DEPLOY_STATUS:
        return []
    data = stage.get("data") or {}
    environment = data.get("env") or deploy_env or env.get("RLP_DEPLOY_ENV", "")
    if not environment:
        raise ReportError("no environment: the deploy stage didn't record one; pass --deploy-env")
    deployed_at = data.get("deployed_at") or datetime.fromtimestamp(
        pipeline.results_path(root).stat().st_mtime, UTC
    ).isoformat(timespec="seconds")
    try:
        doc = contract.load_repo(root)
        chosen, _ = infra.deployable(doc, only)
    except (contract.ContractError, infra.InfraError) as err:
        raise ReportError(str(err)) from err
    # A stage that failed before its readiness checks has no per-component results: every component failed.
    per = stage.get("components") or {c["name"]: stage["status"] for c in chosen}
    registry = env.get("RLP_REGISTRY", "")
    bodies = []
    for c in chosen:
        if c["name"] not in per:
            continue
        bodies.append(
            {
                **run.key(),
                "component": c["name"],
                "environment": environment,
                "artifact": {"kind": c["artifact"]["kind"], "reference": _reference(root, doc, c, registry)},
                "status": DEPLOY_STATUS.get(per[c["name"]], per[c["name"]]),
                "deployed_at": deployed_at,
            }
        )
    return bodies
