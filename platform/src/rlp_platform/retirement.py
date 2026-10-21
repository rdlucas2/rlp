"""Retirement: take a deprecated project to retired, one recorded step at a time.

A retirement has one row per step, run in order:

1. teardown, one per linked repo and environment the project has deployments
   in: dispatch a `destroy` run of the repo's pipeline (workflow_dispatch with
   the golden pipeline's `destroy-env` input). The platform never holds AWS
   credentials; the pipeline runs `rlp infra destroy` with its own deploy role
   and reports each component back as a deployment with status `destroyed`
   (or `destroy-failed`). A teardown is running until every component it
   deployed in that environment has reported.
2. archive, one per linked repo, once every teardown succeeded.
3. expire: set `expires_at` on every artifact of the project to now plus the
   retention. `python -m rlp_platform expire-artifacts` deletes them later.

When every step succeeded, the project moves to retired through the
lifecycle transition; the rlp.lifecycle rule RLP-L108 reads the teardown
record this module keeps. `advance()` stops at the first step that has not
succeeded, so a failed teardown leaves every repo unarchived; calling it again
resumes from there and retries a failed step. Like the store, nothing here
commits: the caller owns the transaction. See docs/lifecycle.md.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy.orm import Session

from rlp_platform import lifecycle
from rlp_platform.github import GitHub, GitHubError
from rlp_platform.models import Artifact, Deployment, Phase, Project, Retirement, RetirementStep
from rlp_platform.store import artifacts, components, deployments, repos, retirements

TEARDOWN, ARCHIVE, EXPIRE = "teardown", "archive", "expire"
PENDING, RUNNING, SUCCEEDED, FAILED = "pending", "running", "succeeded", "failed"
# The deployment statuses a destroy run of the golden pipeline reports.
DESTROYED, DESTROY_FAILED = "destroyed", "destroy-failed"
DEFAULT_RETENTION_DAYS = 30
DEFAULT_WORKFLOW = "pipeline.yml"
DESTROY_INPUT = "destroy-env"


class RetirementError(ValueError):
    """The project can't be retired now: it isn't deprecated, or has no retirement to show."""


@dataclass(frozen=True)
class PlannedStep:
    kind: str
    repo: str | None = None
    environment: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "repo": self.repo, "environment": self.environment}


@dataclass(frozen=True)
class Advance:
    """What one `advance()` call came to: the retirement, and the lifecycle outcome when it tried to retire."""

    retirement: Retirement
    outcome: lifecycle.Outcome | None = None


def plan(session: Session, project: Project) -> list[PlannedStep]:
    """The steps a retirement of `project` would run, in order."""
    linked = sorted(repos.list_(session, project_id=project.id), key=lambda r: r.full_name)
    steps: list[PlannedStep] = []
    for repo in linked:
        envs = sorted({d.environment for d in deployments.list_(session, repo_id=repo.id)})
        steps.extend(PlannedStep(TEARDOWN, repo.full_name, env) for env in envs)
    steps.extend(PlannedStep(ARCHIVE, repo.full_name) for repo in linked)
    steps.append(PlannedStep(EXPIRE))
    return steps


def start(session: Session, project: Project, *, actor: str, reason: str = "", retention_days: int) -> Retirement:
    """The project's retirement: the one on record (to resume), or a new one with its steps. Needs `deprecated`."""
    existing = retirements.for_project(session, project.id)
    if existing is not None and (existing.finished_at is not None or Phase(project.phase) == Phase.DEPRECATED):
        return existing
    if Phase(project.phase) != Phase.DEPRECATED:
        raise RetirementError(
            f"project {project.name!r} is {Phase(project.phase).value}; only a deprecated project can be retired"
        )
    if retention_days < 0:
        raise RetirementError("artifact_retention_days may not be negative")
    record = retirements.create(
        session, project_id=project.id, actor=actor, reason=reason, artifact_retention_days=retention_days
    )
    for step in plan(session, project):
        retirements.add_step(session, record, kind=step.kind, repo_full_name=step.repo, environment=step.environment)
    return record


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _teardown_components(session: Session, step: RetirementStep) -> list[tuple[int, str]]:
    """(id, name) of each declared component of the step's repo that has a deployment in its environment."""
    try:
        repo = repos.get_by_full_name(session, step.repo_full_name or "")
    except LookupError:
        return []
    found = []
    for c in components.list_(session, repo_id=repo.id, include_removed=False):
        if deployments.list_(session, component_id=c.id, environment=step.environment, limit=1):
            found.append((c.id, c.name))
    return found


def _latest_destroy(session: Session, component_id: int, environment: str, since: datetime) -> Deployment | None:
    for d in deployments.list_(session, component_id=component_id, environment=environment):  # newest first
        if d.deployed_at < since:
            return None
        if d.status in (DESTROYED, DESTROY_FAILED):
            return d
    return None


def refresh_teardown(session: Session, step: RetirementStep) -> RetirementStep:
    """Settle a running teardown from the deployments its pipeline reported since it was dispatched."""
    if step.kind != TEARDOWN or step.status != RUNNING or step.started_at is None:
        return step
    targets = _teardown_components(session, step)
    if not targets:
        return retirements.update_step(session, step, status=SUCCEEDED, detail="nothing deployed is left to destroy")
    waiting, failed = [], []
    for component_id, name in targets:
        report = _latest_destroy(session, component_id, step.environment or "", step.started_at)
        if report is None:
            waiting.append(name)
        elif report.status == DESTROY_FAILED:
            failed.append(name)
    if failed:
        return retirements.update_step(
            session, step, status=FAILED, detail=f"the pipeline reported destroy-failed for {', '.join(failed)}"
        )
    if waiting:
        return step
    names = ", ".join(name for _, name in targets)
    return retirements.update_step(session, step, status=SUCCEEDED, detail=f"destroyed {names}")


def on_deployment(session: Session, deployment: Deployment) -> list[RetirementStep]:
    """A deployment was reported: settle the running teardowns of its repo and environment. Returns them."""
    if deployment.status not in (DESTROYED, DESTROY_FAILED):
        return []
    component = components.get(session, deployment.component_id)
    steps = retirements.running_teardowns(session, component.repo.full_name, deployment.environment)
    return [refresh_teardown(session, s) for s in steps]


def _dispatch(gh: GitHub, session: Session, step: RetirementStep, workflow: str, now: datetime) -> None:
    repo = repos.get_by_full_name(session, step.repo_full_name or "")
    try:
        gh.dispatch_workflow(repo.full_name, workflow, repo.default_branch, {DESTROY_INPUT: step.environment or ""})
    except GitHubError as err:
        retirements.update_step(session, step, status=FAILED, detail=f"could not dispatch {workflow}: {err}")
        return
    retirements.update_step(
        session,
        step,
        status=RUNNING,
        started_at=now,
        detail=f"dispatched {workflow} on {repo.default_branch} with {DESTROY_INPUT}={step.environment}",
    )


def _expire(session: Session, project: Project, step: RetirementStep, days: int, now: datetime) -> None:
    at = now + timedelta(days=days)
    owned = artifacts.for_project(session, project.id)
    for artifact in owned:
        # Keep an earlier expiry; never push one out.
        if artifact.expired_at is None and (artifact.expires_at is None or artifact.expires_at > at):
            artifacts.update(session, artifact, expires_at=at)
    detail = f"{len(owned)} artifact(s) expire by {at.isoformat(timespec='seconds')}"
    retirements.update_step(session, step, status=SUCCEEDED, started_at=now, detail=detail)


def advance(
    session: Session,
    project: Project,
    gh: GitHub,
    *,
    actor: str,
    reason: str = "",
    workflow: str = DEFAULT_WORKFLOW,
    opa_path: str | None = None,
    now: datetime | None = None,
) -> Advance:
    """Run the retirement from its first incomplete step until a step has to wait or fails, or the project retires."""
    record = retirements.for_project(session, project.id)
    if record is None:
        raise RetirementError(f"project {project.name!r} has no retirement; start one first")
    if record.finished_at is not None:
        return Advance(record)
    if Phase(project.phase) != Phase.DEPRECATED:
        raise RetirementError(f"project {project.name!r} is {Phase(project.phase).value}, no longer deprecated")
    at = _now(now)
    steps = list(record.steps)
    teardowns = [s for s in steps if s.kind == TEARDOWN]
    for step in teardowns:
        refresh_teardown(session, step)
    for step in teardowns:
        if step.status in (PENDING, FAILED):
            _dispatch(gh, session, step, workflow, at)
    if any(s.status != SUCCEEDED for s in teardowns):
        return Advance(record)
    for step in steps:
        if step.status == SUCCEEDED or step.kind == TEARDOWN:
            continue
        if step.kind == ARCHIVE:
            try:
                gh.archive(step.repo_full_name or "")
            except GitHubError as err:
                retirements.update_step(session, step, status=FAILED, started_at=at, detail=f"could not archive: {err}")
                return Advance(record)
            retirements.update_step(session, step, status=SUCCEEDED, started_at=at, detail="archived")
        elif step.kind == EXPIRE:
            _expire(session, project, step, record.artifact_retention_days, at)
    outcome = lifecycle.transition(
        session, project, Phase.RETIRED, actor=actor, reason=reason or record.reason, opa_path=opa_path
    )
    if outcome.allowed:
        retirements.finish(session, record, at)
    return Advance(record, outcome)


def state(record: Retirement) -> str:
    """done, failed (a step failed; run it again to retry), waiting (a teardown is running) or pending."""
    if record.finished_at is not None:
        return "done"
    statuses = {s.status for s in record.steps}
    if FAILED in statuses:
        return "failed"
    if RUNNING in statuses:
        return "waiting"
    return "pending"


# --- artifact expiry ---


class RegistryError(RuntimeError):
    """The registry refused or could not be reached; the artifact stays for the next run."""


class Unsupported(RegistryError):
    """rlp can't delete an artifact with this reference; delete it by hand."""


class Registry(Protocol):
    def delete(self, reference: str) -> bool:
        """Delete what `reference` names; False when it is already gone."""
        ...


GHCR_HOST = "ghcr.io"
RELEASE_ASSET = re.compile(
    r"^https://github\.com/(?P<repo>[^/]+/[^/]+)/releases/download/(?P<tag>[^/]+)/(?P<name>[^/]+)$"
)


def parse_image(reference: str) -> tuple[str, str, str, str]:
    """(host, owner, package, version) of host/owner/package[@digest|:tag]; the version defaults to latest."""
    host, _, path = reference.partition("/")
    version = "latest"
    if "@" in path:
        path, _, version = path.partition("@")
    elif ":" in path.rsplit("/", 1)[-1]:
        path, _, version = path.rpartition(":")
    owner, _, package = path.partition("/")
    if not host or not owner or not package or not version:
        raise Unsupported(f"{reference!r} is not an image reference")
    return host, owner, package, version


class GitHubRegistry:
    """Deletes GHCR image versions and GitHub release assets, where rlp publishes artifacts (`rlp publish`)."""

    def __init__(self, gh: GitHub) -> None:
        self.gh = gh

    def delete(self, reference: str) -> bool:
        asset = RELEASE_ASSET.match(reference)
        try:
            if asset:
                return self.gh.delete_release_asset(asset["repo"], asset["tag"], asset["name"])
            host, owner, package, version = parse_image(reference)
            if host != GHCR_HOST:
                raise Unsupported(f"{host} is not a registry rlp deletes from (only {GHCR_HOST} and GitHub releases)")
            return self.gh.delete_container_version(owner, package, version)
        except GitHubError as err:
            raise RegistryError(str(err)) from err


@dataclass(frozen=True)
class Expiry:
    reference: str
    status: str  # deleted, gone (already deleted), unsupported or failed
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"reference": self.reference, "status": self.status, "detail": self.detail}


def expire_artifacts(
    session: Session, registry: Registry, *, now: datetime | None = None, commit: Callable[[], None] | None = None
) -> list[Expiry]:
    """Delete every artifact past its expires_at from its registry and mark it expired. Safe to run repeatedly.

    An artifact is marked expired once it is deleted or found already gone, so
    a second run deletes nothing again. One the registry refused, or rlp can't
    delete, stays unmarked and is reported. `commit`, when given, runs after
    each artifact, so a run that stops halfway keeps what it did.
    """
    at = _now(now)
    results = []
    due: list[Artifact] = artifacts.due(session, at)
    for artifact in due:
        try:
            deleted = registry.delete(artifact.reference)
        except Unsupported as err:
            results.append(Expiry(artifact.reference, "unsupported", str(err)))
            continue
        except RegistryError as err:
            results.append(Expiry(artifact.reference, "failed", str(err)))
            continue
        artifacts.update(session, artifact, expired_at=at)
        results.append(Expiry(artifact.reference, "deleted" if deleted else "gone"))
        if commit is not None:
            commit()
    return results
