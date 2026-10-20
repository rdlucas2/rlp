"""The platform REST API, /api/v1: the project record and lifecycle moves.

Every route needs `Authorization: Bearer <token>`, compared in constant time
with RLP_API_TOKEN. rlp is single-user until slice 6, so one token stands in
for users and roles. RLP_API_TOKEN_DISABLED=true opens the API for local
development; without either, every request answers 401.

Each request gets its own database session. Read routes never write; the
write routes commit before they answer. platform/openapi.json is this module's
spec (`python -m rlp_platform openapi`); a test fails when they differ.
"""

from __future__ import annotations

import hmac
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from rlp_platform import lifecycle
from rlp_platform.config import Settings
from rlp_platform.models import Build, Component, Deployment, LifecycleTransition, Phase, Project, Repo
from rlp_platform.store import (
    NotFound,
    artifacts,
    builds,
    components,
    deployments,
    projects,
    repos,
    session_factory,
    transitions,
)

PREFIX = "/api/v1"
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000

bearer = HTTPBearer(auto_error=False, description="RLP_API_TOKEN, as `Authorization: Bearer <token>`.")


def require_token(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
) -> None:
    """401 unless the request carries the configured token, or the token is disabled."""
    settings: Settings = request.app.state.settings
    if settings.api_token_disabled:
        return
    expected = settings.api_token
    given = credentials.credentials if credentials is not None else ""
    # Fail closed: with no token configured, nothing matches.
    if not expected or not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="missing or wrong bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_session(request: Request) -> Iterator[Session]:
    factory: sessionmaker[Session] | None = request.app.state.session_factory
    if factory is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="no database: DATABASE_URL is not set")
    with factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]
Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT, description="Return at most this many, newest first.")]

router = APIRouter(prefix=PREFIX, dependencies=[Depends(require_token)])


def make_session_factory(settings: Settings) -> sessionmaker[Session] | None:
    return session_factory(settings.database_url) if settings.database_url else None


# --- schemas ---


class Problem(BaseModel):
    detail: str


class ProjectOut(BaseModel):
    name: str
    description: str
    owner: str
    phase: Phase
    dashboard_url: str | None
    created_at: datetime
    updated_at: datetime


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The same rule as metadata.project in rlp.yaml: a lowercase DNS label.
    name: str = Field(pattern=r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")
    owner: str = Field(default="", max_length=200)
    description: str = ""
    dashboard_url: str | None = None


class ProjectUpdate(BaseModel):
    """The fields to change; fields left out keep their value. Phase changes go through transitions."""

    model_config = ConfigDict(extra="forbid")

    owner: str | None = Field(default=None, max_length=200)
    description: str | None = None
    dashboard_url: str | None = None


class RepoOut(BaseModel):
    full_name: str
    project: str
    default_branch: str
    template_source: str | None
    template_version: str | None
    last_indexed_commit: str | None
    created_at: datetime
    updated_at: datetime


class ComponentOut(BaseModel):
    repo: str
    name: str
    language: str
    artifact_kind: str
    runtime_kind: str


class BuildOut(BaseModel):
    id: int
    repo: str
    commit: str
    branch: str
    passed: bool
    stages: dict[str, Any]
    workflow_run_url: str | None
    started_at: datetime
    finished_at: datetime | None


class ArtifactOut(BaseModel):
    kind: str
    reference: str


class DeploymentOut(BaseModel):
    id: int
    repo: str
    component: str
    environment: str
    status: str
    artifact: ArtifactOut
    deployed_at: datetime


class RuleResultOut(BaseModel):
    rule: str
    description: str
    passed: bool
    messages: list[str]


class TransitionOut(BaseModel):
    from_phase: Phase | None
    to_phase: Phase
    actor: str
    reason: str
    rule_results: list[RuleResultOut]
    at: datetime


class TransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: str = Field(min_length=1, description="The phase to move to.")
    reason: str = ""
    actor: str = Field(default="api", min_length=1, max_length=200, description="Who asked for the move.")


class TransitionRejected(BaseModel):
    detail: str
    project: str
    from_phase: Phase
    to_phase: str
    failures: list[RuleResultOut]
    results: list[RuleResultOut]


# --- converters ---


def project_out(p: Project) -> ProjectOut:
    return ProjectOut(
        name=p.name,
        description=p.description,
        owner=p.owner,
        phase=Phase(p.phase),
        dashboard_url=p.dashboard_url,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


def repo_out(r: Repo) -> RepoOut:
    return RepoOut(
        full_name=r.full_name,
        project=r.project.name,
        default_branch=r.default_branch,
        template_source=r.template_source,
        template_version=r.template_version,
        last_indexed_commit=r.last_indexed_commit,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def component_out(c: Component) -> ComponentOut:
    return ComponentOut(
        repo=c.repo.full_name,
        name=c.name,
        language=c.language,
        artifact_kind=c.artifact_kind,
        runtime_kind=c.runtime_kind,
    )


def build_out(session: Session, b: Build) -> BuildOut:
    return BuildOut(
        id=b.id,
        repo=repos.get(session, b.repo_id).full_name,
        commit=b.commit,
        branch=b.branch,
        passed=b.passed,
        stages=b.stages,
        workflow_run_url=b.workflow_run_url,
        started_at=b.started_at,
        finished_at=b.finished_at,
    )


def deployment_out(session: Session, d: Deployment) -> DeploymentOut:
    component = components.get(session, d.component_id)
    artifact = artifacts.get(session, d.artifact_id)
    return DeploymentOut(
        id=d.id,
        repo=component.repo.full_name,
        component=component.name,
        environment=d.environment,
        status=d.status,
        artifact=ArtifactOut(kind=artifact.kind, reference=artifact.reference),
        deployed_at=d.deployed_at,
    )


def transition_out(t: LifecycleTransition) -> TransitionOut:
    return TransitionOut(
        from_phase=Phase(t.from_phase) if t.from_phase is not None else None,
        to_phase=Phase(t.to_phase),
        actor=t.actor,
        reason=t.reason,
        rule_results=[RuleResultOut(**r) for r in t.rule_results],
        at=t.at,
    )


def rule_out(r: lifecycle.RuleResult) -> RuleResultOut:
    return RuleResultOut(**r.as_dict())


# --- lookups ---


def find_project(session: Session, name: str) -> Project:
    try:
        return projects.get_by_name(session, name)
    except NotFound as err:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(err)) from err


def find_repo(session: Session, owner: str, name: str) -> Repo:
    try:
        return repos.get_by_full_name(session, f"{owner}/{name}")
    except NotFound as err:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(err)) from err


NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": Problem, "description": "No such record"}}


# --- projects ---


@router.get("/projects", tags=["projects"])
def list_projects(
    session: SessionDep, phase: Annotated[Phase | None, Query(description="Only projects in this phase.")] = None
) -> list[ProjectOut]:
    return [project_out(p) for p in projects.list_(session, phase=phase)]


@router.post(
    "/projects",
    tags=["projects"],
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": Problem, "description": "A project with that name exists"}},
)
def create_project(session: SessionDep, body: ProjectCreate) -> ProjectOut:
    try:
        projects.get_by_name(session, body.name)
    except NotFound:
        pass
    else:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"project {body.name!r} already exists")
    project = projects.create(
        session, name=body.name, owner=body.owner, description=body.description, dashboard_url=body.dashboard_url
    )
    session.commit()
    session.refresh(project)
    return project_out(project)


@router.get("/projects/{name}", tags=["projects"], responses=NOT_FOUND)
def get_project(session: SessionDep, name: str) -> ProjectOut:
    return project_out(find_project(session, name))


@router.patch("/projects/{name}", tags=["projects"], responses=NOT_FOUND)
def update_project(session: SessionDep, name: str, body: ProjectUpdate) -> ProjectOut:
    project = find_project(session, name)
    fields = body.model_dump(exclude_unset=True)
    for key in ("owner", "description"):
        if key in fields and fields[key] is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"{key} may not be null")
    if fields:
        projects.update(session, project, **fields)
        session.commit()
        session.refresh(project)
    return project_out(project)


@router.get("/projects/{name}/repos", tags=["repos"], responses=NOT_FOUND)
def list_project_repos(session: SessionDep, name: str) -> list[RepoOut]:
    project = find_project(session, name)
    return [repo_out(r) for r in repos.list_(session, project_id=project.id)]


@router.get("/projects/{name}/components", tags=["components"], responses=NOT_FOUND)
def list_project_components(session: SessionDep, name: str) -> list[ComponentOut]:
    project = find_project(session, name)
    return [component_out(c) for c in components.list_(session, project_id=project.id)]


@router.get("/projects/{name}/builds", tags=["builds"], responses=NOT_FOUND)
def list_project_builds(session: SessionDep, name: str, limit: Limit = DEFAULT_LIMIT) -> list[BuildOut]:
    project = find_project(session, name)
    return [build_out(session, b) for b in builds.list_(session, project_id=project.id, limit=limit)]


@router.get("/projects/{name}/deployments", tags=["deployments"], responses=NOT_FOUND)
def list_project_deployments(session: SessionDep, name: str, limit: Limit = DEFAULT_LIMIT) -> list[DeploymentOut]:
    project = find_project(session, name)
    return [deployment_out(session, d) for d in deployments.list_(session, project_id=project.id, limit=limit)]


# --- lifecycle ---


@router.get("/projects/{name}/transitions", tags=["lifecycle"], responses=NOT_FOUND)
def list_transitions(session: SessionDep, name: str) -> list[TransitionOut]:
    """The project's phase history, oldest first."""
    project = find_project(session, name)
    return [transition_out(t) for t in transitions.list_(session, project_id=project.id)]


@router.post(
    "/projects/{name}/transitions",
    tags=["lifecycle"],
    status_code=status.HTTP_201_CREATED,
    response_model=TransitionOut,
    responses={
        **NOT_FOUND,
        409: {"model": TransitionRejected, "description": "An entry rule failed; nothing changed"},
        503: {"model": Problem, "description": "The entry rules could not be evaluated"},
    },
)
def move_project(request: Request, session: SessionDep, name: str, body: TransitionRequest) -> Any:
    """Move the project to another phase if every entry rule passes (see docs/lifecycle.md)."""
    project = find_project(session, name)
    try:
        outcome = lifecycle.transition(
            session, project, body.to, actor=body.actor, reason=body.reason, opa_path=request.app.state.opa_path
        )
    except lifecycle.LifecycleError as err:
        session.rollback()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(err)) from err
    if outcome.transition is None:
        session.rollback()
        failures = outcome.failures
        rejected = TransitionRejected(
            detail=f"{name}: {outcome.from_phase.value} -> {outcome.to_phase} failed "
            + ", ".join(r.rule for r in failures),
            project=name,
            from_phase=outcome.from_phase,
            to_phase=outcome.to_phase,
            failures=[rule_out(r) for r in failures],
            results=[rule_out(r) for r in outcome.results],
        )
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=rejected.model_dump(mode="json"))
    session.commit()
    session.refresh(outcome.transition)
    return transition_out(outcome.transition)


# --- repos ---


@router.get("/repos", tags=["repos"], responses=NOT_FOUND)
def list_repos(
    session: SessionDep, project: Annotated[str | None, Query(description="Only this project's repos.")] = None
) -> list[RepoOut]:
    project_id = find_project(session, project).id if project is not None else None
    return [repo_out(r) for r in repos.list_(session, project_id=project_id)]


@router.get("/repos/{owner}/{name}", tags=["repos"], responses=NOT_FOUND)
def get_repo(session: SessionDep, owner: str, name: str) -> RepoOut:
    return repo_out(find_repo(session, owner, name))


@router.get("/repos/{owner}/{name}/components", tags=["components"], responses=NOT_FOUND)
def list_repo_components(session: SessionDep, owner: str, name: str) -> list[ComponentOut]:
    repo = find_repo(session, owner, name)
    return [component_out(c) for c in components.list_(session, repo_id=repo.id)]


@router.get("/repos/{owner}/{name}/builds", tags=["builds"], responses=NOT_FOUND)
def list_repo_builds(session: SessionDep, owner: str, name: str, limit: Limit = DEFAULT_LIMIT) -> list[BuildOut]:
    repo = find_repo(session, owner, name)
    return [build_out(session, b) for b in builds.list_(session, repo_id=repo.id, limit=limit)]


@router.get("/repos/{owner}/{name}/deployments", tags=["deployments"], responses=NOT_FOUND)
def list_repo_deployments(
    session: SessionDep, owner: str, name: str, limit: Limit = DEFAULT_LIMIT
) -> list[DeploymentOut]:
    repo = find_repo(session, owner, name)
    return [deployment_out(session, d) for d in deployments.list_(session, repo_id=repo.id, limit=limit)]
