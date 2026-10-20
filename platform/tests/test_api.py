"""The /api/v1 routes against Postgres, through the app with a bearer token."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from rlp_platform import db
from rlp_platform.app import create_app, openapi_json
from rlp_platform.models import Phase
from rlp_platform.store import artifacts, builds, components, deployments, projects, repos

API_KEY = "test-only"
AUTH = {"Authorization": f"Bearer {API_KEY}"}
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
OPENAPI = Path(__file__).resolve().parents[1] / "openapi.json"


@pytest.fixture
def api(settings, migrated_db, opa_path):
    configured = dataclasses.replace(settings, database_url=migrated_db, api_token=API_KEY)
    with TestClient(create_app(configured, opa_path=opa_path), headers=AUTH) as client:
        yield client


@pytest.fixture
def record(migrated_db):
    """Two projects: orders (in-dev, one repo with a component, builds and a deployment) and billing (proposed)."""
    engine = db.make_engine(migrated_db)
    with Session(engine) as s:
        orders = projects.create(s, name="orders", owner="", description="The orders API", phase=Phase.IN_DEV)
        projects.create(s, name="billing", owner="rdlucas2")
        repo = repos.create(s, full_name="acme/orders-api", project_id=orders.id, template_version="v1")
        component = components.create(
            s,
            repo_id=repo.id,
            name="api",
            language="python",
            artifact_kind="container-image",
            runtime_kind="container-service",
        )
        builds.create(s, repo_id=repo.id, commit="aaa", branch="main", passed=False, started_at=NOW - timedelta(1))
        builds.create(s, repo_id=repo.id, commit="bbb", branch="main", passed=True, started_at=NOW, stages={"lint": 1})
        artifact = artifacts.create(s, component_id=component.id, kind="container-image", reference="ghcr.io/a@sha")
        deployments.create(s, component_id=component.id, environment="dev", artifact_id=artifact.id, status="ok")
        s.commit()
    engine.dispose()


# --- auth ---


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Basic {API_KEY}"},
        {"Authorization": API_KEY},
    ],
    ids=["missing", "wrong", "not-bearer", "no-scheme"],
)
def test_api_needs_the_right_token(settings, headers):
    client = TestClient(create_app(dataclasses.replace(settings, api_token=API_KEY)))
    for path in ("/api/v1/projects", "/api/v1/repos/acme/x", "/api/v1/projects/nope/transitions"):
        resp = client.get(path, headers=headers)
        assert resp.status_code == 401, path
        assert resp.headers["www-authenticate"] == "Bearer"
    assert client.post("/api/v1/projects", json={"name": "x"}, headers=headers).status_code == 401


def test_health_stays_open(settings):
    client = TestClient(create_app(dataclasses.replace(settings, api_token=API_KEY)))
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200


def test_right_token_without_database_answers_503(settings):
    client = TestClient(create_app(dataclasses.replace(settings, api_token=API_KEY)))
    resp = client.get("/api/v1/projects", headers=AUTH)
    assert resp.status_code == 503
    assert "DATABASE_URL" in resp.json()["detail"]


def test_no_token_configured_rejects_everything(settings):
    client = TestClient(create_app(settings))
    assert client.get("/api/v1/projects").status_code == 401
    assert client.get("/api/v1/projects", headers={"Authorization": "Bearer "}).status_code == 401


def test_disabled_token_opens_the_api(settings, migrated_db):
    open_settings = dataclasses.replace(settings, database_url=migrated_db, api_token_disabled=True)
    client = TestClient(create_app(open_settings))
    assert client.get("/api/v1/projects").json() == []


def test_right_token(api):
    assert api.get("/api/v1/projects").status_code == 200


# --- projects ---


def test_list_projects_and_filter_by_phase(api, record):
    assert [p["name"] for p in api.get("/api/v1/projects").json()] == ["billing", "orders"]
    in_dev = api.get("/api/v1/projects", params={"phase": "in-dev"}).json()
    assert [(p["name"], p["phase"]) for p in in_dev] == [("orders", "in-dev")]
    assert api.get("/api/v1/projects", params={"phase": "launched"}).status_code == 422


def test_create_get_and_update_a_project(api):
    resp = api.post("/api/v1/projects", json={"name": "shop", "owner": "rdlucas2", "description": "A shop"})
    assert resp.status_code == 201
    created = resp.json()
    assert (created["name"], created["phase"], created["owner"], created["dashboard_url"]) == (
        "shop",
        "proposed",
        "rdlucas2",
        None,
    )
    assert api.get("/api/v1/projects/shop").json() == created

    resp = api.patch("/api/v1/projects/shop", json={"dashboard_url": "https://grafana/d/shop", "owner": "team"})
    assert resp.status_code == 200
    updated = resp.json()
    assert (updated["owner"], updated["description"], updated["dashboard_url"]) == (
        "team",
        "A shop",
        "https://grafana/d/shop",
    )
    assert api.patch("/api/v1/projects/shop", json={"dashboard_url": None}).json()["dashboard_url"] is None
    assert api.patch("/api/v1/projects/shop", json={}).json()["owner"] == "team"


def test_create_rejects_a_duplicate_and_a_bad_name(api, record):
    resp = api.post("/api/v1/projects", json={"name": "orders"})
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]
    assert api.post("/api/v1/projects", json={"name": "Not A Label"}).status_code == 422
    assert api.post("/api/v1/projects", json={"name": "x", "phase": "live"}).status_code == 422


def test_update_cannot_set_the_phase_or_null_the_owner(api, record):
    assert api.patch("/api/v1/projects/billing", json={"phase": "live"}).status_code == 422
    resp = api.patch("/api/v1/projects/billing", json={"owner": None})
    assert resp.status_code == 422
    assert "owner may not be null" in resp.json()["detail"]


def test_unknown_project_answers_404(api):
    for path in ("", "/repos", "/components", "/builds", "/deployments", "/transitions"):
        resp = api.get(f"/api/v1/projects/nope{path}")
        assert resp.status_code == 404, path
        assert resp.json() == {"detail": "project 'nope' not found"}
    assert api.patch("/api/v1/projects/nope", json={"owner": "x"}).status_code == 404
    assert api.post("/api/v1/projects/nope/transitions", json={"to": "designed"}).status_code == 404


# --- repos, components, builds, deployments ---


def test_repos(api, record):
    listed = api.get("/api/v1/repos").json()
    assert [(r["full_name"], r["project"], r["default_branch"]) for r in listed] == [
        ("acme/orders-api", "orders", "main")
    ]
    assert api.get("/api/v1/repos", params={"project": "billing"}).json() == []
    assert api.get("/api/v1/repos", params={"project": "nope"}).status_code == 404
    assert api.get("/api/v1/projects/orders/repos").json() == listed
    repo = api.get("/api/v1/repos/acme/orders-api").json()
    assert repo["template_version"] == "v1"
    assert api.get("/api/v1/repos/acme/nope").status_code == 404


def test_components(api, record):
    expected = [
        {
            "repo": "acme/orders-api",
            "name": "api",
            "language": "python",
            "artifact_kind": "container-image",
            "runtime_kind": "container-service",
        }
    ]
    assert api.get("/api/v1/repos/acme/orders-api/components").json() == expected
    assert api.get("/api/v1/projects/orders/components").json() == expected
    assert api.get("/api/v1/projects/billing/components").json() == []


def test_builds_newest_first_with_a_limit(api, record):
    by_repo = api.get("/api/v1/repos/acme/orders-api/builds").json()
    assert [(b["commit"], b["passed"], b["repo"]) for b in by_repo] == [
        ("bbb", True, "acme/orders-api"),
        ("aaa", False, "acme/orders-api"),
    ]
    assert by_repo[0]["stages"] == {"lint": 1}
    assert api.get("/api/v1/projects/orders/builds").json() == by_repo
    assert [b["commit"] for b in api.get("/api/v1/projects/orders/builds", params={"limit": 1}).json()] == ["bbb"]
    assert api.get("/api/v1/projects/orders/builds", params={"limit": 0}).status_code == 422
    assert api.get("/api/v1/projects/billing/builds").json() == []


def test_deployments(api, record):
    by_repo = api.get("/api/v1/repos/acme/orders-api/deployments").json()
    assert [(d["component"], d["environment"], d["status"], d["artifact"]) for d in by_repo] == [
        ("api", "dev", "ok", {"kind": "container-image", "reference": "ghcr.io/a@sha"})
    ]
    assert api.get("/api/v1/projects/orders/deployments").json() == by_repo
    assert api.get("/api/v1/repos/acme/orders-api/deployments", params={"limit": 1}).json() == by_repo
    assert api.get("/api/v1/projects/billing/deployments").json() == []
    assert api.get("/api/v1/repos/acme/nope/deployments").status_code == 404


# --- lifecycle ---


def test_move_to_live_without_an_owner_answers_409_with_the_rules(api, record):
    resp = api.post("/api/v1/projects/orders/transitions", json={"to": "live", "reason": "launch"})
    assert resp.status_code == 409
    body = resp.json()
    assert [r["rule"] for r in body["failures"]] == ["RLP-L105", "RLP-L106"]
    assert (body["project"], body["from_phase"], body["to_phase"]) == ("orders", "in-dev", "live")
    assert [r["rule"] for r in body["results"]] == ["RLP-L101", "RLP-L105", "RLP-L106", "RLP-L107"]
    assert body["detail"] == "orders: in-dev -> live failed RLP-L105, RLP-L106"
    # Nothing changed.
    assert api.get("/api/v1/projects/orders").json()["phase"] == "in-dev"
    assert api.get("/api/v1/projects/orders/transitions").json() == []


def test_move_records_the_transition(api, record):
    api.patch("/api/v1/projects/orders", json={"owner": "rdlucas2", "dashboard_url": "https://d/orders"})
    resp = api.post("/api/v1/projects/orders/transitions", json={"to": "live", "reason": "launch", "actor": "rdlucas2"})
    assert resp.status_code == 201, resp.text
    moved = resp.json()
    assert (moved["from_phase"], moved["to_phase"], moved["actor"], moved["reason"]) == (
        "in-dev",
        "live",
        "rdlucas2",
        "launch",
    )
    assert all(r["passed"] for r in moved["rule_results"])
    assert api.get("/api/v1/projects/orders").json()["phase"] == "live"
    assert api.get("/api/v1/projects/orders/transitions").json() == [moved]


def test_move_to_an_unknown_phase_fails_rule_l101(api, record):
    resp = api.post("/api/v1/projects/billing/transitions", json={"to": "launched"})
    assert resp.status_code == 409
    assert [r["rule"] for r in resp.json()["failures"]] == ["RLP-L101"]


def test_move_without_opa_answers_503(settings, migrated_db, record, tmp_path):
    configured = dataclasses.replace(settings, database_url=migrated_db, api_token=API_KEY)
    client = TestClient(create_app(configured, opa_path=str(tmp_path / "no-opa")), headers=AUTH)
    resp = client.post("/api/v1/projects/billing/transitions", json={"to": "designed"})
    assert resp.status_code == 503
    assert "could not run opa" in resp.json()["detail"]
    assert client.get("/api/v1/projects/billing").json()["phase"] == "proposed"


# --- the committed spec ---


def test_committed_openapi_matches_the_app(settings):
    committed = json.loads(OPENAPI.read_text(encoding="utf-8"))
    assert committed == create_app(settings).openapi(), (
        "platform/openapi.json is stale; run: uv run python -m rlp_platform openapi > openapi.json"
    )
    assert OPENAPI.read_text(encoding="utf-8") == openapi_json(create_app(settings))


def test_spec_declares_bearer_auth_on_api_routes_only(settings):
    spec = create_app(settings).openapi()
    assert spec["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert "security" in spec["paths"]["/api/v1/projects"]["get"]
    assert "security" not in spec["paths"]["/healthz"]["get"]
