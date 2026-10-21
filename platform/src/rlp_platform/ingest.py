"""Builds and deployments the golden pipeline reports (`rlp pipeline report`).

A build is one run attempt of the pipeline on a commit: (repo, commit, run id,
run attempt) identifies it, so reporting the same attempt again updates the one
build instead of adding another. Each report carries stage results from
`.rlp/pipeline/results.json`; they merge into the build by stage name, the way
the CLI merges them into the file, so the gates job and the publish job of one
run add up to one build. A build passed when it has stages and none failed.

A build may name a repo the indexer hasn't seen yet. It is kept under that name
and attached, with its artifacts, when the indexer first indexes the repo.

A deployment is one artifact of a component in an environment at a time:
(component, environment, artifact, deployed at) identifies it. It needs the
repo and the component to be indexed. Its artifact is the one its run's build
recorded for the component, when there is one, else the reference it names.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from rlp_platform.models import Artifact, Build, Deployment, Repo
from rlp_platform.store import NotFound, artifacts, builds, components, deployments, repos

FAILED = "failed"


@dataclass(frozen=True)
class ArtifactReport:
    component: str
    kind: str
    reference: str


@dataclass(frozen=True)
class Run:
    """One run attempt of a pipeline on a commit."""

    repo: str
    commit: str
    run_id: str
    run_attempt: int


def _find_repo(session: Session, full_name: str) -> Repo | None:
    try:
        return repos.get_by_full_name(session, full_name)
    except NotFound:
        return None


def _component_id(session: Session, repo: Repo | None, name: str) -> int | None:
    if repo is None:
        return None
    try:
        return components.get_by_name(session, repo.id, name).id
    except NotFound:
        return None


def failed_stages(stages: dict[str, Any]) -> list[str]:
    return [name for name, result in stages.items() if isinstance(result, dict) and result.get("status") == FAILED]


def record_build(
    session: Session,
    run: Run,
    *,
    branch: str,
    results: list[dict[str, Any]],
    artifact_reports: Sequence[ArtifactReport] = (),
    workflow_run_url: str | None = None,
    now: datetime | None = None,
) -> tuple[Build, bool]:
    """Create or update the run attempt's build; returns it and whether it is new. Flushes; the caller commits."""
    now = now or datetime.now(UTC)
    repo = _find_repo(session, run.repo)
    build = builds.find_run(session, run.repo, run.commit, run.run_id, run.run_attempt)
    created = build is None
    stages = {} if build is None else dict(build.stages)
    stages.update({r["stage"]: r for r in results})
    passed = bool(stages) and not failed_stages(stages)
    if build is None:
        build = builds.create(
            session,
            repo_id=repo.id if repo is not None else None,
            repo_full_name=run.repo,
            commit=run.commit,
            branch=branch,
            run_id=run.run_id,
            run_attempt=run.run_attempt,
            stages=stages,
            passed=passed,
            workflow_run_url=workflow_run_url,
            started_at=now,
            finished_at=now,
        )
    else:
        fields: dict[str, Any] = {"stages": stages, "passed": passed, "finished_at": now}
        if workflow_run_url:
            fields["workflow_run_url"] = workflow_run_url
        builds.update(session, build, **fields)
    known = {(a.component_name, a.kind, a.reference) for a in artifacts.list_(session, build_id=build.id)}
    for report in artifact_reports:
        if (report.component, report.kind, report.reference) in known:
            continue
        artifacts.create(
            session,
            build_id=build.id,
            component_id=_component_id(session, repo, report.component),
            component_name=report.component,
            kind=report.kind,
            reference=report.reference,
        )
        known.add((report.component, report.kind, report.reference))
    return build, created


def attach(session: Session, repo: Repo) -> int:
    """Link builds and artifacts reported before the repo or its components were indexed; returns the builds linked."""
    attached = builds.attach(session, repo)
    by_name = {c.name: c.id for c in components.list_(session, repo_id=repo.id)}
    for artifact in artifacts.unattached(session, repo_id=repo.id):
        if artifact.component_name in by_name:
            artifacts.update(session, artifact, component_id=by_name[artifact.component_name])
    return len(attached)


def _artifact_for(session: Session, run: Run | None, component_id: int, kind: str, reference: str) -> Artifact:
    build = builds.find_run(session, run.repo, run.commit, run.run_id, run.run_attempt) if run is not None else None
    if build is not None:
        recorded = [a for a in artifacts.list_(session, build_id=build.id, kind=kind) if a.component_id == component_id]
        if recorded:
            return recorded[0]
    found = artifacts.find(session, component_id=component_id, kind=kind, reference=reference)
    if found is not None:
        return found
    return artifacts.create(
        session,
        component_id=component_id,
        build_id=build.id if build is not None else None,
        kind=kind,
        reference=reference,
    )


def record_deployment(
    session: Session,
    *,
    repo: str,
    component: str,
    environment: str,
    kind: str,
    reference: str,
    status: str,
    deployed_at: datetime,
    run: Run | None = None,
) -> tuple[Deployment, bool]:
    """Create the deployment, or update the status of the one recorded before.

    NotFound when the repo or the component isn't indexed.
    """
    record = repos.get_by_full_name(session, repo)
    target = components.get_by_name(session, record.id, component)
    artifact = _artifact_for(session, run, target.id, kind, reference)
    existing = deployments.find(
        session, component_id=target.id, environment=environment, artifact_id=artifact.id, deployed_at=deployed_at
    )
    if existing is not None:
        if existing.status != status:
            deployments.update(session, existing, status=status)
        return existing, False
    deployment = deployments.create(
        session,
        component_id=target.id,
        environment=environment,
        artifact_id=artifact.id,
        status=status,
        deployed_at=deployed_at,
    )
    return deployment, True
