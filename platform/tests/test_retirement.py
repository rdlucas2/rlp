"""The retirement workflow: teardown, archive and artifact expiry, with fakes for GitHub and the registry."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from rlp_platform import __main__ as entrypoint
from rlp_platform import db, lifecycle, retirement
from rlp_platform.app import create_app
from rlp_platform.config import Settings
from rlp_platform.github import GitHub
from rlp_platform.models import Artifact, LifecycleTransition, Phase
from rlp_platform.store import artifacts, components, deployments, projects, repos, retirements

API_KEY = "test-only"
AUTH = {"Authorization": f"Bearer {API_KEY}"}
LONG_AGO = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
IMAGE = "ghcr.io/acme/orders-api/orders-api@sha256:aaa"
WEB_IMAGE = "ghcr.io/acme/orders-web/orders-web:1.0.0"


class FakeGitHub:
    """The GitHub REST calls the retirement workflow makes, answered in memory. No network.

    Records workflow dispatches and archives; repos in `refuse_dispatch` or
    `refuse_archive` answer 403. `packages` maps (owner, package) to its
    versions; owners are users, so the organization endpoints answer 404.
    """

    def __init__(self) -> None:
        self.dispatches: list[tuple[str, str, str, dict]] = []
        self.archived: list[str] = []
        self.refuse_dispatch: set[str] = set()
        self.refuse_archive: set[str] = set()
        self.packages: dict[tuple[str, str], list[dict]] = {}
        self.releases: dict[tuple[str, str], list[dict]] = {}
        self.deleted: list[str] = []
        self.next_id = 100

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self) -> GitHub:
        return GitHub("https://api.github.test", "test-token", transport=self.transport)

    def add_version(self, owner: str, package: str, digest: str, tags: list[str]) -> None:
        self.next_id += 1
        self.packages.setdefault((owner, package), []).append(
            {"id": self.next_id, "name": digest, "metadata": {"container": {"tags": tags}}}
        )

    def add_asset(self, repo: str, tag: str, name: str) -> None:
        self.next_id += 1
        self.releases.setdefault((repo, tag), []).append({"id": self.next_id, "name": name})

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode().split("?")[0]
        parts = [httpx.URL("/" + p).path.strip("/") for p in path.strip("/").split("/")]
        method = request.method
        if parts[0] == "repos" and len(parts) >= 3:
            full_name = f"{parts[1]}/{parts[2]}"
            rest = parts[3:]
            if method == "POST" and rest[:2] == ["actions", "workflows"] and rest[3:] == ["dispatches"]:
                if full_name in self.refuse_dispatch:
                    return httpx.Response(403, json={"message": "Resource not accessible by integration"})
                body = json.loads(request.content)
                self.dispatches.append((full_name, rest[2], body["ref"], body["inputs"]))
                return httpx.Response(204)
            if method == "PATCH" and not rest:
                if full_name in self.refuse_archive:
                    return httpx.Response(403, json={"message": "Must have admin rights"})
                assert json.loads(request.content) == {"archived": True}
                self.archived.append(full_name)
                return httpx.Response(200, json={"full_name": full_name, "archived": True})
            if method == "GET" and rest[:2] == ["releases", "tags"]:
                assets = self.releases.get((full_name, rest[2]))
                if assets is None:
                    return httpx.Response(404, json={"message": "Not Found"})
                return httpx.Response(200, json={"tag_name": rest[2], "assets": assets})
            if method == "DELETE" and rest[:2] == ["releases", "assets"]:
                for (repo, tag), assets in self.releases.items():
                    for a in assets:
                        if repo == full_name and a["id"] == int(rest[2]):
                            assets.remove(a)
                            self.deleted.append(f"{repo}@{tag}/{a['name']}")
                            return httpx.Response(204)
                return httpx.Response(404, json={"message": "Not Found"})
        if parts[0] == "orgs":
            return httpx.Response(404, json={"message": "Not Found"})
        if parts[0] == "users" and parts[2:4] == ["packages", "container"]:
            owner, package = parts[1], parts[4]
            versions = self.packages.get((owner, package))
            if versions is None:
                return httpx.Response(404, json={"message": "Package not found"})
            if method == "GET" and parts[5:] == ["versions"]:
                return httpx.Response(200, json=versions)
            if method == "DELETE" and len(parts) == 7:
                for v in versions:
                    if v["id"] == int(parts[6]):
                        versions.remove(v)
                        self.deleted.append(f"{owner}/{package}@{v['name']}")
                        return httpx.Response(204)
                return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def api(settings, migrated_db, opa_path, github):
    configured = dataclasses.replace(settings, database_url=migrated_db, api_token=API_KEY, github_token=API_KEY)
    app = create_app(configured, opa_path=opa_path)
    app.state.github_transport = github.transport
    with TestClient(app, headers=AUTH) as client:
        yield client


def seed(s: Session, *, phase: Phase = Phase.DEPRECATED) -> None:
    """orders: acme/orders-api (api, deployed to dev and prod) and acme/orders-web (web, deployed to dev).

    billing: live, with nothing deployed.
    """
    orders = projects.create(s, name="orders", owner="rdlucas2", description="The orders API", phase=phase)
    projects.create(s, name="billing", owner="rdlucas2", phase=Phase.LIVE)
    for full_name, component, env_refs in (
        ("acme/orders-api", "api", [("dev", IMAGE), ("prod", IMAGE)]),
        ("acme/orders-web", "web", [("dev", WEB_IMAGE)]),
    ):
        repo = repos.create(s, full_name=full_name, project_id=orders.id)
        c = components.create(
            s,
            repo_id=repo.id,
            name=component,
            language="python",
            artifact_kind="container-image",
            runtime_kind="container-service",
        )
        for env, ref in env_refs:
            artifact = artifacts.find(s, component_id=c.id, kind="container-image", reference=ref)
            if artifact is None:
                artifact = artifacts.create(s, component_id=c.id, kind="container-image", reference=ref)
            deployments.create(
                s, component_id=c.id, environment=env, artifact_id=artifact.id, status="succeeded", deployed_at=LONG_AGO
            )
    s.commit()


@pytest.fixture
def record(migrated_db):
    engine = db.make_engine(migrated_db)
    with Session(engine) as s:
        seed(s)
    engine.dispose()


def report(api: TestClient, repo: str, component: str, env: str, status: str, *, at: datetime | None = None):
    at = at or datetime.now(UTC)
    return api.post(
        "/api/v1/deployments",
        json={
            "repo": repo,
            "component": component,
            "environment": env,
            "status": status,
            "deployed_at": at.isoformat(),
        },
    )


def destroy_all(api: TestClient, status: str = "destroyed") -> None:
    for repo, component, env in (
        ("acme/orders-api", "api", "dev"),
        ("acme/orders-api", "api", "prod"),
        ("acme/orders-web", "web", "dev"),
    ):
        assert report(api, repo, component, env, status).status_code == 201


def retire(api: TestClient, **body):
    return api.post("/api/v1/projects/orders/retirement", json={"actor": "rdlucas2", "reason": "unused", **body})


def steps(doc: dict) -> list[tuple[str, str | None, str | None, str]]:
    return [(s["kind"], s["repo"], s["environment"], s["status"]) for s in doc["steps"]]


def phase(api: TestClient, name: str = "orders") -> str:
    return api.get(f"/api/v1/projects/{name}").json()["phase"]


# --- plan and start ---


def test_dry_run_shows_the_plan_and_changes_nothing(api, record, github):
    resp = retire(api, dry_run=True)
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["state"] == "planned"
    assert steps(doc) == [
        ("teardown", "acme/orders-api", "dev", "pending"),
        ("teardown", "acme/orders-api", "prod", "pending"),
        ("teardown", "acme/orders-web", "dev", "pending"),
        ("archive", "acme/orders-api", None, "pending"),
        ("archive", "acme/orders-web", None, "pending"),
        ("expire", None, None, "pending"),
    ]
    assert github.dispatches == []
    assert api.get("/api/v1/projects/orders/retirement").status_code == 404


def test_a_project_not_deprecated_is_refused(api, record, github):
    for body in ({}, {"dry_run": True}):
        resp = api.post("/api/v1/projects/billing/retirement", json=body)
        assert resp.status_code == 409
        assert "billing" in resp.json()["detail"] and "live" in resp.json()["detail"]
    assert github.dispatches == []
    assert api.get("/api/v1/projects/billing/retirement").status_code == 404
    assert phase(api, "billing") == "live"
    assert api.post("/api/v1/projects/nope/retirement", json={}).status_code == 404


def test_start_dispatches_every_teardown_and_waits(api, record, github):
    doc = retire(api, workflow="golden-caller.yml").json()
    assert doc["state"] == "waiting"
    assert [s[3] for s in steps(doc)] == ["running"] * 3 + ["pending"] * 3
    assert github.dispatches == [
        ("acme/orders-api", "golden-caller.yml", "main", {"destroy-env": "dev"}),
        ("acme/orders-api", "golden-caller.yml", "main", {"destroy-env": "prod"}),
        ("acme/orders-web", "golden-caller.yml", "main", {"destroy-env": "dev"}),
    ]
    assert github.archived == []
    # Calling again while the teardowns run dispatches nothing new.
    assert retire(api).json()["state"] == "waiting"
    assert len(github.dispatches) == 3
    assert phase(api) == "deprecated"


# --- the happy path ---


def test_a_full_retirement(api, record, github, migrated_db):
    retire(api)
    destroy_all(api)
    status = api.get("/api/v1/projects/orders/retirement").json()
    assert [s[3] for s in steps(status)][:3] == ["succeeded"] * 3
    assert status["steps"][0]["detail"] == "destroyed api"

    before = datetime.now(UTC)
    doc = retire(api).json()
    assert doc["state"] == "done", doc
    assert doc["finished_at"] is not None
    assert {s[3] for s in steps(doc)} == {"succeeded"}
    assert github.archived == ["acme/orders-api", "acme/orders-web"]
    assert phase(api) == "retired"

    engine = db.make_engine(migrated_db)
    with Session(engine) as s:
        owned = list(s.scalars(select(Artifact)))
        assert len(owned) == 2
        for a in owned:
            assert a.expires_at is not None
            assert before + timedelta(days=30) <= a.expires_at <= datetime.now(UTC) + timedelta(days=30)
        moves = list(s.scalars(select(LifecycleTransition).where(LifecycleTransition.to_phase == Phase.RETIRED)))
        assert [(m.from_phase, m.accepted, m.actor) for m in moves] == [(Phase.DEPRECATED, True, "rdlucas2")]
        rules = {r["rule"]: r["passed"] for r in moves[0].rule_results}
        assert rules == {"RLP-L101": True, "RLP-L108": True}
    engine.dispose()

    # Done is done: calling again changes nothing.
    assert retire(api).json()["state"] == "done"
    assert github.archived == ["acme/orders-api", "acme/orders-web"]


def test_the_retention_comes_from_the_request(api, record, github, migrated_db):
    retire(api, artifact_retention_days=0)
    destroy_all(api)
    assert retire(api).json()["artifact_retention_days"] == 0
    engine = db.make_engine(migrated_db)
    with Session(engine) as s:
        assert all(a.expires_at <= datetime.now(UTC) for a in s.scalars(select(Artifact)))
    engine.dispose()


def test_a_project_with_nothing_deployed_retires_in_one_call(api, migrated_db, github):
    engine = db.make_engine(migrated_db)
    with Session(engine) as s:
        p = projects.create(s, name="orders", owner="o", phase=Phase.DEPRECATED)
        repos.create(s, full_name="acme/orders-api", project_id=p.id)
        s.commit()
    engine.dispose()
    doc = retire(api).json()
    assert doc["state"] == "done"
    assert steps(doc) == [("archive", "acme/orders-api", None, "succeeded"), ("expire", None, None, "succeeded")]
    assert github.dispatches == []
    assert phase(api) == "retired"


# --- failures and resuming ---


def test_a_failed_teardown_stops_before_archiving(api, record, github):
    retire(api)
    assert report(api, "acme/orders-api", "api", "prod", "destroy-failed").status_code == 201
    destroy_all_but_prod = [("acme/orders-api", "api", "dev"), ("acme/orders-web", "web", "dev")]
    for repo, component, env in destroy_all_but_prod:
        report(api, repo, component, env, "destroyed")
    status = api.get("/api/v1/projects/orders/retirement").json()
    assert status["state"] == "failed"
    failed = [s for s in status["steps"] if s["status"] == "failed"]
    assert [(s["repo"], s["environment"]) for s in failed] == [("acme/orders-api", "prod")]
    assert "destroy-failed for api" in failed[0]["detail"]
    assert github.archived == []
    assert phase(api) == "deprecated"


def test_resume_after_a_failed_teardown(api, record, github):
    retire(api)
    report(api, "acme/orders-api", "api", "prod", "destroy-failed")
    report(api, "acme/orders-api", "api", "dev", "destroyed")
    report(api, "acme/orders-web", "web", "dev", "destroyed")
    # Re-running retries the failed teardown only, and still archives nothing.
    doc = retire(api).json()
    assert doc["state"] == "waiting", doc
    assert github.dispatches[3:] == [("acme/orders-api", "pipeline.yml", "main", {"destroy-env": "prod"})]
    assert github.archived == []
    # A report from before the retry doesn't count; one after it does.
    report(api, "acme/orders-api", "api", "prod", "destroyed", at=datetime.now(UTC) - timedelta(hours=1))
    assert api.get("/api/v1/projects/orders/retirement").json()["state"] == "waiting"
    report(api, "acme/orders-api", "api", "prod", "destroyed")
    assert retire(api).json()["state"] == "done"
    assert github.archived == ["acme/orders-api", "acme/orders-web"]


def test_a_refused_dispatch_fails_the_step(api, record, github):
    github.refuse_dispatch.add("acme/orders-web")
    doc = retire(api).json()
    assert doc["state"] == "failed"
    web = doc["steps"][2]
    assert web["status"] == "failed" and "403" in web["detail"]
    destroy_all(api)
    assert github.archived == []
    github.refuse_dispatch.clear()
    assert retire(api).json()["state"] == "waiting"
    report(api, "acme/orders-web", "web", "dev", "destroyed")
    assert retire(api).json()["state"] == "done"


def test_a_failed_archive_stops_and_resumes(api, record, github):
    github.refuse_archive.add("acme/orders-web")
    retire(api)
    destroy_all(api)
    doc = retire(api).json()
    assert doc["state"] == "failed"
    assert steps(doc)[3:] == [
        ("archive", "acme/orders-api", None, "succeeded"),
        ("archive", "acme/orders-web", None, "failed"),
        ("expire", None, None, "pending"),
    ]
    assert phase(api) == "deprecated"
    github.refuse_archive.clear()
    assert retire(api).json()["state"] == "done"
    assert github.archived == ["acme/orders-api", "acme/orders-web"]


def test_a_retirement_stops_if_the_project_leaves_deprecated(api, record, github, migrated_db):
    retire(api)
    engine = db.make_engine(migrated_db)
    with Session(engine) as s:
        projects.update(s, projects.get_by_name(s, "orders"), phase=Phase.LIVE)
        s.commit()
    engine.dispose()
    resp = retire(api)
    assert resp.status_code == 409
    assert "live" in resp.json()["detail"]


# --- deployment reports ---


def test_a_report_without_an_artifact_needs_an_earlier_deployment(api, record):
    resp = report(api, "acme/orders-web", "web", "prod", "destroyed")
    assert resp.status_code == 422
    assert "never deployed to prod" in resp.json()["detail"]
    resp = report(api, "acme/orders-web", "web", "dev", "destroyed")
    assert resp.status_code == 201
    assert resp.json()["artifact"]["reference"] == WEB_IMAGE


def test_an_ordinary_deployment_leaves_a_teardown_running(api, record):
    retire(api)
    report(api, "acme/orders-web", "web", "dev", "succeeded")
    assert api.get("/api/v1/projects/orders/retirement").json()["steps"][2]["status"] == "running"


# --- the lifecycle rule's input ---


def test_the_teardown_record_reaches_the_lifecycle_rules(session, github):
    seed(session)
    project = projects.get_by_name(session, "orders")
    assert lifecycle.build_input(session, project, Phase.RETIRED)["teardown"] is None
    record = retirement.start(session, project, actor="a", retention_days=30)
    for step in record.steps:
        if step.kind == "teardown":
            retirements.update_step(session, step, status="succeeded")
    teardown = lifecycle.build_input(session, project, Phase.RETIRED)["teardown"]
    assert teardown["environments"] == [
        {"repo": "acme/orders-api", "environment": "dev"},
        {"repo": "acme/orders-api", "environment": "prod"},
        {"repo": "acme/orders-web", "environment": "dev"},
    ]


def test_advance_needs_a_retirement(session, github):
    seed(session)
    with pytest.raises(retirement.RetirementError, match="no retirement"):
        retirement.advance(session, projects.get_by_name(session, "orders"), github.client(), actor="a")
    with pytest.raises(retirement.RetirementError, match="negative"):
        retirement.start(session, projects.get_by_name(session, "orders"), actor="a", retention_days=-1)


# --- expiring artifacts ---


class FakeRegistry:
    def __init__(self) -> None:
        self.present = {IMAGE, WEB_IMAGE}
        self.deletes: list[str] = []
        self.broken: set[str] = set()

    def delete(self, reference: str) -> bool:
        if reference in self.broken:
            raise retirement.RegistryError("registry down")
        if reference.startswith("123.dkr.ecr"):
            raise retirement.Unsupported("not ours")
        if reference in self.present:
            self.present.remove(reference)
            self.deletes.append(reference)
            return True
        return False


def expire_all(session: Session, at: datetime) -> None:
    for a in session.scalars(select(Artifact)):
        artifacts.update(session, a, expires_at=at)


def test_expire_artifacts_twice_deletes_each_once(session):
    seed(session)
    now = datetime.now(UTC)
    expire_all(session, now - timedelta(minutes=1))
    registry = FakeRegistry()
    first = retirement.expire_artifacts(session, registry, now=now)
    assert sorted((r.reference, r.status) for r in first) == [(IMAGE, "deleted"), (WEB_IMAGE, "deleted")]
    assert retirement.expire_artifacts(session, registry, now=now + timedelta(minutes=1)) == []
    assert sorted(registry.deletes) == [IMAGE, WEB_IMAGE]
    assert all(a.expired_at == now for a in session.scalars(select(Artifact)))


def test_expire_artifacts_leaves_future_and_failed_ones(session):
    seed(session)
    now = datetime.now(UTC)
    web = artifacts.find(
        session,
        component_id=components.get_by_name(session, repos.get_by_full_name(session, "acme/orders-web").id, "web").id,
        kind="container-image",
        reference=WEB_IMAGE,
    )
    api_image = next(a for a in session.scalars(select(Artifact)) if a.reference == IMAGE)
    artifacts.update(session, api_image, expires_at=now + timedelta(days=1))
    artifacts.update(session, web, expires_at=now - timedelta(days=1))
    ecr = artifacts.create(
        session, component_id=web.component_id, kind="container-image", reference="123.dkr.ecr.x/web:1"
    )
    artifacts.update(session, ecr, expires_at=now - timedelta(days=1))
    registry = FakeRegistry()
    registry.broken.add(WEB_IMAGE)
    results = {r.reference: r.status for r in retirement.expire_artifacts(session, registry, now=now)}
    assert results == {WEB_IMAGE: "failed", "123.dkr.ecr.x/web:1": "unsupported"}
    assert web.expired_at is None and ecr.expired_at is None and api_image.expired_at is None
    registry.broken.clear()
    results = {r.reference: r.status for r in retirement.expire_artifacts(session, registry, now=now)}
    assert results == {WEB_IMAGE: "deleted", "123.dkr.ecr.x/web:1": "unsupported"}
    assert web.expired_at == now


def test_github_registry_deletes_ghcr_versions_and_release_assets(github):
    github.add_version("acme", "orders-api/orders-api", "sha256:aaa", ["1.0.0"])
    github.add_version("acme", "orders-web/orders-web", "sha256:bbb", ["1.0.0", "latest"])
    github.add_asset("acme/lambda", "v1", "fn.zip")
    registry = retirement.GitHubRegistry(github.client())
    assert registry.delete(IMAGE) is True
    assert registry.delete(IMAGE) is False  # already gone
    assert registry.delete(WEB_IMAGE) is True  # by tag
    assert registry.delete("ghcr.io/acme/never/there:1") is False
    assert registry.delete("https://github.com/acme/lambda/releases/download/v1/fn.zip") is True
    assert registry.delete("https://github.com/acme/lambda/releases/download/v1/fn.zip") is False
    assert registry.delete("https://github.com/acme/lambda/releases/download/v9/fn.zip") is False
    assert github.deleted == [
        "acme/orders-api/orders-api@sha256:aaa",
        "acme/orders-web/orders-web@sha256:bbb",
        "acme/lambda@v1/fn.zip",
    ]
    with pytest.raises(retirement.Unsupported, match=r"123\.dkr\.ecr"):
        registry.delete("123.dkr.ecr.us-east-1.amazonaws.com/web:1")
    with pytest.raises(retirement.Unsupported, match="not an image reference"):
        registry.delete("orders")


def test_github_registry_reports_a_refusal():
    refuse = httpx.MockTransport(lambda request: httpx.Response(403, json={"message": "no"}))
    registry = retirement.GitHubRegistry(GitHub("https://api.github.test", "t", transport=refuse))
    with pytest.raises(retirement.RegistryError, match="403"):
        registry.delete(IMAGE)


@pytest.mark.parametrize(
    ("reference", "parsed"),
    [
        ("ghcr.io/acme/a/b@sha256:1", ("ghcr.io", "acme", "a/b", "sha256:1")),
        ("ghcr.io/acme/a:2.0", ("ghcr.io", "acme", "a", "2.0")),
        ("ghcr.io/acme/a", ("ghcr.io", "acme", "a", "latest")),
        ("localhost:5000/acme/a:1", ("localhost:5000", "acme", "a", "1")),
    ],
)
def test_parse_image(reference, parsed):
    assert retirement.parse_image(reference) == parsed


def test_expire_artifacts_command(migrated_db, github, capsys):
    engine = db.make_engine(migrated_db)
    with Session(engine) as s:
        seed(s)
        expire_all(s, datetime.now(UTC) - timedelta(days=1))
        s.commit()
    engine.dispose()
    github.add_version("acme", "orders-api/orders-api", "sha256:aaa", [])
    github.add_version("acme", "orders-web/orders-web", "sha256:bbb", ["1.0.0"])
    settings = Settings.from_env({"DATABASE_URL": migrated_db, "RLP_GITHUB_TOKEN": "t"})
    assert entrypoint.expire_artifacts(settings, transport=github.transport) == 0
    first = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert first["ok"] is True
    assert sorted(r["status"] for r in first["results"]) == ["deleted", "deleted"]
    assert entrypoint.expire_artifacts(settings, transport=github.transport) == 0
    second = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert second == {"ok": True, "results": []}
    assert len(github.deleted) == 2


def test_expire_artifacts_command_fails_when_a_delete_fails(migrated_db, capsys):
    engine = db.make_engine(migrated_db)
    with Session(engine) as s:
        seed(s)
        expire_all(s, datetime.now(UTC) - timedelta(days=1))
        s.commit()
    engine.dispose()
    refuse = httpx.MockTransport(lambda request: httpx.Response(500, json={"message": "down"}))
    settings = Settings.from_env({"DATABASE_URL": migrated_db})
    assert entrypoint.expire_artifacts(settings, transport=refuse) == 1
    doc = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert doc["ok"] is False


def test_expire_artifacts_command_needs_a_database():
    assert entrypoint.expire_artifacts(Settings.from_env({})) == 2


def test_main_runs_expire_artifacts(monkeypatch):
    calls = []
    monkeypatch.setattr(entrypoint, "expire_artifacts", lambda s: calls.append(s) or 0)
    monkeypatch.setattr(entrypoint.logs, "configure", lambda level: None)
    assert entrypoint.main(["expire-artifacts"]) == 0
    assert len(calls) == 1
