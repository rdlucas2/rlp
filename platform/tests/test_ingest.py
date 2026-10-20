"""Builds and deployments the pipeline reports: POST /api/v1/builds and /api/v1/deployments, against Postgres."""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rlp_platform import db, indexer
from rlp_platform.app import create_app
from rlp_platform.github import GitHub
from rlp_platform.models import Artifact, Build, Deployment
from rlp_platform.store import components, projects, repos

API_KEY = "test-only"
AUTH = {"Authorization": f"Bearer {API_KEY}"}
REPO = "acme/orders-api"
SHA = "a" * 40
DIGEST = "ghcr.io/acme/orders-api/orders-api@sha256:" + "1" * 64
DEPLOYED_AT = "2026-10-20T12:00:00Z"


@pytest.fixture
def api(settings, migrated_db):
    configured = dataclasses.replace(settings, database_url=migrated_db, api_token=API_KEY)
    with TestClient(create_app(configured), headers=AUTH) as client:
        yield client


@pytest.fixture
def indexed(migrated_db):
    """acme/orders-api, indexed, with one component: api."""
    engine = db.make_engine(migrated_db)
    with Session(engine) as s:
        project = projects.create(s, name="orders", owner="")
        repo = repos.create(s, full_name=REPO, project_id=project.id)
        components.create(
            s,
            repo_id=repo.id,
            name="api",
            language="python",
            artifact_kind="container-image",
            runtime_kind="container-service",
        )
        s.commit()
    engine.dispose()


def count(url: str, model) -> int:
    engine = db.make_engine(url)
    with Session(engine) as s:
        n = s.scalar(select(func.count()).select_from(model))
    engine.dispose()
    return n


def stage(name: str, status: str = "passed", **extra) -> dict:
    return {"stage": name, "status": status, "detail": "", "seconds": 1.0, "components": {}, "data": {}, **extra}


def report(**overrides) -> dict:
    body = {
        "repo": REPO,
        "commit": SHA,
        "branch": "main",
        "run_id": "123",
        "run_attempt": 1,
        "run_url": "https://github.com/acme/orders-api/actions/runs/123/attempts/1",
        "results": [stage("precommit"), stage("lint")],
    }
    return {**body, **overrides}


def test_a_build_is_recorded_once_per_run_attempt(api, indexed, migrated_db):
    first = api.post("/api/v1/builds", json=report())
    assert first.status_code == 201, first.text
    again = api.post("/api/v1/builds", json=report())
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    assert count(migrated_db, Build) == 1

    doc = again.json()
    assert doc["passed"] is True
    assert doc["failed_stages"] == []
    assert sorted(doc["stages"]) == ["lint", "precommit"]
    assert (doc["run_id"], doc["run_attempt"]) == ("123", 1)
    listed = api.get(f"/api/v1/repos/{REPO}/builds").json()
    assert [b["id"] for b in listed] == [doc["id"]]


def test_another_attempt_or_commit_is_another_build(api, indexed, migrated_db):
    api.post("/api/v1/builds", json=report())
    assert api.post("/api/v1/builds", json=report(run_attempt=2)).status_code == 201
    assert api.post("/api/v1/builds", json=report(commit="b" * 40)).status_code == 201
    assert count(migrated_db, Build) == 3


def test_a_failed_gate_records_a_failed_build_with_its_stages(api, indexed):
    body = report(results=[stage("precommit"), stage("lint"), stage("test", "failed", detail="2 tests failed")])
    doc = api.post("/api/v1/builds", json=body).json()
    assert doc["passed"] is False
    assert doc["failed_stages"] == ["test"]
    assert doc["stages"]["test"]["detail"] == "2 tests failed"


def test_later_reports_of_a_run_merge_their_stages(api, indexed):
    api.post("/api/v1/builds", json=report(results=[stage("lint"), stage("trivy")]))
    doc = api.post("/api/v1/builds", json=report(results=[stage("package")])).json()
    assert sorted(doc["stages"]) == ["lint", "package", "trivy"]
    assert doc["passed"] is True
    doc = api.post("/api/v1/builds", json=report(results=[stage("trivy", "failed")])).json()
    assert doc["passed"] is False
    assert doc["failed_stages"] == ["trivy"]


def test_a_build_with_no_stages_has_not_passed(api, indexed):
    assert api.post("/api/v1/builds", json=report(results=[])).json()["passed"] is False


def test_artifacts_are_recorded_once_with_the_build(api, indexed, migrated_db):
    artifact = {"component": "api", "kind": "container-image", "reference": DIGEST}
    api.post("/api/v1/builds", json=report(artifacts=[artifact]))
    doc = api.post("/api/v1/builds", json=report(results=[], artifacts=[artifact])).json()
    assert doc["artifacts"] == [artifact]
    assert count(migrated_db, Artifact) == 1


def test_a_build_for_an_unknown_repo_is_attached_when_it_is_indexed(api, migrated_db, github, platform_contract):
    name = "rdlucas2/rlp"
    artifact = {"component": "platform", "kind": "container-image", "reference": DIGEST}
    resp = api.post("/api/v1/builds", json=report(repo=name, artifacts=[artifact]))
    assert resp.status_code == 201, resp.text
    assert api.get("/api/v1/repos/rdlucas2/rlp/builds").status_code == 404  # not indexed yet

    github.add(name, "1" * 40, platform_contract)
    engine = db.make_engine(migrated_db)
    with Session(engine) as s, GitHub("https://api.github.test", "t", transport=github.transport) as gh:
        assert indexer.index_commit(s, gh, name, "1" * 40, "main").ok
        s.commit()
        stored = s.scalars(select(Artifact)).one()
        component = components.get_by_name(s, repos.get_by_full_name(s, name).id, "platform")
        assert stored.component_id == component.id
    engine.dispose()

    builds_ = api.get("/api/v1/repos/rdlucas2/rlp/builds").json()
    assert [b["id"] for b in builds_] == [resp.json()["id"]]
    assert builds_[0]["artifacts"] == [artifact]


@pytest.mark.parametrize(
    "change",
    [
        {"repo": "not-a-repo"},
        {"commit": ""},
        {"run_attempt": 0},
        {"results": [{"stage": "lint", "status": "maybe"}]},
        {"artifacts": [{"component": "api", "kind": "zip"}]},
        {"surprise": True},
    ],
    ids=["repo", "commit", "attempt", "status", "artifact", "extra"],
)
def test_a_malformed_build_report_is_refused(api, change):
    assert api.post("/api/v1/builds", json=report(**change)).status_code == 422


def deployment(**overrides) -> dict:
    body = {
        "repo": REPO,
        "component": "api",
        "environment": "dev",
        "artifact": {"kind": "container-image", "reference": "ghcr.io/acme/orders-api/orders-api-api:aaaaaaaaaaaa"},
        "status": "succeeded",
        "deployed_at": DEPLOYED_AT,
        "commit": SHA,
        "run_id": "123",
        "run_attempt": 1,
    }
    return {**body, **overrides}


def test_a_deployment_is_recorded_once(api, indexed, migrated_db):
    first = api.post("/api/v1/deployments", json=deployment())
    assert first.status_code == 201, first.text
    again = api.post("/api/v1/deployments", json=deployment())
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    assert count(migrated_db, Deployment) == 1
    assert count(migrated_db, Artifact) == 1
    assert api.get(f"/api/v1/repos/{REPO}/deployments").json()[0]["environment"] == "dev"


def test_a_repeated_deployment_takes_the_new_status(api, indexed, migrated_db):
    api.post("/api/v1/deployments", json=deployment(status="failed"))
    assert api.post("/api/v1/deployments", json=deployment(status="succeeded")).json()["status"] == "succeeded"
    assert count(migrated_db, Deployment) == 1


def test_a_later_deployment_is_another_record(api, indexed, migrated_db):
    api.post("/api/v1/deployments", json=deployment())
    api.post("/api/v1/deployments", json=deployment(deployed_at="2026-10-20T13:00:00Z"))
    api.post("/api/v1/deployments", json=deployment(environment="prod"))
    assert count(migrated_db, Deployment) == 3
    assert count(migrated_db, Artifact) == 1  # the same image each time


def test_a_deployment_uses_its_builds_artifact(api, indexed, migrated_db):
    """The deploy job knows the image tag; the build recorded its digest. The deployment links to the digest."""
    api.post(
        "/api/v1/builds",
        json=report(artifacts=[{"component": "api", "kind": "container-image", "reference": DIGEST}]),
    )
    doc = api.post("/api/v1/deployments", json=deployment()).json()
    assert doc["artifact"] == {"kind": "container-image", "reference": DIGEST}
    assert count(migrated_db, Artifact) == 1


def test_a_deployment_without_a_run_names_its_artifact(api, indexed):
    body = deployment(commit=None, run_id=None, run_attempt=None)
    doc = api.post("/api/v1/deployments", json=body).json()
    assert doc["artifact"]["reference"].endswith(":aaaaaaaaaaaa")


@pytest.mark.parametrize("change", [{"repo": "acme/unknown"}, {"component": "worker"}], ids=["repo", "component"])
def test_a_deployment_of_something_unindexed_is_not_found(api, indexed, migrated_db, change):
    resp = api.post("/api/v1/deployments", json=deployment(**change))
    assert resp.status_code == 404
    assert "indexer" in resp.json()["detail"]
    assert count(migrated_db, Artifact) == 0


def test_reports_need_the_token(settings, migrated_db):
    configured = dataclasses.replace(settings, database_url=migrated_db, api_token=API_KEY)
    client = TestClient(create_app(configured))
    assert client.post("/api/v1/builds", json=report()).status_code == 401
    assert client.post("/api/v1/deployments", json=deployment()).status_code == 401
