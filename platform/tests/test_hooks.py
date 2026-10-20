"""POST /api/v1/hooks/github and POST /api/v1/sync through the app, against Postgres; GitHub is recorded."""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import rlp_platform.__main__ as entrypoint
from rlp_platform.app import create_app

API_KEY = "test-only"
HOOK_KEY = "webhook-test-key"
GITHUB_AUTH = "gh-test-auth"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "github"
PUSH = json.loads((FIXTURES / "push.json").read_text(encoding="utf-8"))
RENAMED = json.loads((FIXTURES / "repository-renamed.json").read_text(encoding="utf-8"))
INVALID = (FIXTURES / "invalid-rlp.yaml").read_text(encoding="utf-8")
REPO = PUSH["repository"]["full_name"]
HEAD = PUSH["after"]
OLDER = "1" * 40


def sign(body: bytes, key: str = HOOK_KEY) -> str:
    return "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def app(settings, migrated_db, github):
    configured = dataclasses.replace(
        settings,
        database_url=migrated_db,
        api_token=API_KEY,
        github_webhook_secret=HOOK_KEY,
        github_token=GITHUB_AUTH,
        github_api_url="https://api.github.test",
    )
    app = create_app(configured)
    app.state.github_transport = github.transport
    return app


@pytest.fixture
def api(app):
    with TestClient(app, headers={"Authorization": f"Bearer {API_KEY}"}) as client:
        yield client


def deliver(api: TestClient, event: str, payload: object, *, signature: str | None = "sign", raw: bytes | None = None):
    body = raw if raw is not None else json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
    if signature == "sign":
        headers["X-Hub-Signature-256"] = sign(body)
    elif signature is not None:
        headers["X-Hub-Signature-256"] = signature
    # GitHub sends no bearer token; the signature is the only check.
    return api.post("/api/v1/hooks/github", content=body, headers={**headers, "Authorization": ""})


def components(api: TestClient, repo: str = REPO, **params) -> list[dict]:
    resp = api.get(f"/api/v1/repos/{repo}/components", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_signed_push_to_the_default_branch_indexes_the_repo(api, github, platform_contract):
    github.add(REPO, HEAD, platform_contract)
    resp = deliver(api, "push", PUSH)
    assert resp.status_code == 200, resp.text
    doc = resp.json()
    assert (doc["event"], doc["action"]) == ("push", "indexed")
    assert doc["result"]["status"] == "indexed"
    repo = api.get(f"/api/v1/repos/{REPO}").json()
    assert repo["project"] == "rlp-platform"
    assert repo["template_source"] == "gh:rdlucas2/rlp-template-python-fastapi"
    assert repo["template_version"] == "d5f0037"
    assert repo["last_indexed_commit"] == HEAD
    assert repo["index_error"] is None
    [component] = components(api)
    assert component["name"] == "platform"
    assert component["removed_at"] is None
    assert api.get("/api/v1/projects/rlp-platform").json()["phase"] == "proposed"


def test_push_with_invalid_rlp_yaml_keeps_components_and_shows_the_error(api, github, platform_contract):
    github.add(REPO, OLDER, platform_contract)
    assert deliver(api, "push", {**PUSH, "after": OLDER}).json()["result"]["status"] == "indexed"
    github.add(REPO, HEAD, {**platform_contract, "rlp.yaml": INVALID})
    resp = deliver(api, "push", PUSH)
    assert resp.status_code == 200
    assert resp.json()["result"]["status"] == "invalid"
    repo = api.get(f"/api/v1/repos/{REPO}").json()
    assert repo["index_error"].startswith(f"at {HEAD}: rlp.yaml components.0")
    assert repo["last_indexed_commit"] == OLDER
    assert [(c["name"], c["runtime_kind"]) for c in components(api)] == [("platform", "container-service")]


@pytest.mark.parametrize(
    "signature",
    [None, "sha256=" + "0" * 64, "sign-with-another-secret", "sha1=0123"],
    ids=["unsigned", "wrong", "other-secret", "sha1"],
)
def test_a_bad_signature_answers_401_and_changes_nothing(api, github, platform_contract, signature):
    github.add(REPO, HEAD, platform_contract)
    body = json.dumps(PUSH).encode()
    if signature == "sign-with-another-secret":
        signature = sign(body, "not-the-secret")
    resp = deliver(api, "push", PUSH, signature=signature, raw=body)
    assert resp.status_code == 401
    assert github.requests == []
    assert api.get("/api/v1/repos").json() == []
    assert api.get("/api/v1/projects").json() == []


def test_without_a_configured_secret_every_delivery_is_refused(settings, migrated_db, github):
    app = create_app(dataclasses.replace(settings, database_url=migrated_db, api_token=API_KEY))
    app.state.github_transport = github.transport
    with TestClient(app) as client:
        assert deliver(client, "push", PUSH).status_code == 401
        assert deliver(client, "push", PUSH, signature=sign(b"")).status_code == 401


def test_the_signature_is_over_the_exact_bytes(api, github, platform_contract):
    github.add(REPO, HEAD, platform_contract)
    body = json.dumps(PUSH, indent=2).encode()
    assert deliver(api, "push", None, signature=sign(json.dumps(PUSH).encode()), raw=body).status_code == 401
    assert deliver(api, "push", None, signature=sign(body), raw=body).status_code == 200


def test_push_to_another_branch_is_ignored(api, github, platform_contract):
    github.add(REPO, HEAD, platform_contract)
    resp = deliver(api, "push", {**PUSH, "ref": "refs/heads/feature"})
    assert resp.status_code == 200
    assert resp.json() == {
        "event": "push",
        "action": "ignored",
        "reason": "refs/heads/feature is not the default branch (main)",
        "result": None,
    }
    assert github.requests == []
    assert api.get("/api/v1/repos").json() == []


@pytest.mark.parametrize(
    "changes",
    [{"ref": "refs/tags/v1.0.0"}, {"deleted": True, "after": "0" * 40}],
    ids=["tag", "deleted-branch"],
)
def test_tag_pushes_and_branch_deletions_are_ignored(api, github, changes):
    resp = deliver(api, "push", {**PUSH, **changes})
    assert resp.json()["action"] == "ignored"
    assert github.requests == []


def test_ping_and_other_events_are_ignored(api, github):
    assert deliver(api, "ping", {"zen": "Design for failure."}).json()["reason"] == "pong"
    resp = deliver(api, "issues", {"action": "opened"})
    assert resp.status_code == 200
    assert resp.json()["action"] == "ignored"
    assert github.requests == []


@pytest.mark.parametrize(
    ("raw", "detail"),
    [
        (b"not json", "the payload is not JSON"),
        (b"[1, 2]", "the payload is not a JSON object"),
        (b'{"ref": "refs/heads/main"}', "push payload lacks repository, ref or after"),
    ],
)
def test_a_malformed_push_answers_422(api, raw, detail):
    resp = deliver(api, "push", None, raw=raw)
    assert resp.status_code == 422
    assert resp.json()["detail"] == detail


def test_a_github_failure_answers_502_and_changes_nothing(api, github):
    github.broken.add(REPO)
    resp = deliver(api, "push", PUSH)
    assert resp.status_code == 502
    assert "GitHub answered 500" in resp.json()["detail"]
    assert api.get("/api/v1/repos").json() == []


def test_the_webhook_needs_a_database(settings):
    app = create_app(dataclasses.replace(settings, github_webhook_secret=HOOK_KEY))
    with TestClient(app) as client:
        assert deliver(client, "push", PUSH).status_code == 503


def test_repository_events_reindex_the_default_branch_head(api, github, platform_contract):
    github.add(REPO, HEAD, platform_contract)
    payload = {"action": "edited", "repository": PUSH["repository"]}
    resp = deliver(api, "repository", payload)
    assert resp.status_code == 200, resp.text
    assert (resp.json()["action"], resp.json()["reason"]) == ("indexed", "edited")
    assert api.get(f"/api/v1/repos/{REPO}").json()["last_indexed_commit"] == HEAD


def test_a_renamed_repo_keeps_its_record(api, github, platform_contract):
    github.add("rdlucas2/rlp-old", OLDER, platform_contract)
    old_push = {**PUSH, "after": OLDER, "repository": {**PUSH["repository"], "full_name": "rdlucas2/rlp-old"}}
    assert deliver(api, "push", old_push).status_code == 200
    github.add(REPO, HEAD, platform_contract)
    resp = deliver(api, "repository", RENAMED)
    assert resp.status_code == 200, resp.text
    assert [r["full_name"] for r in api.get("/api/v1/repos").json()] == [REPO]
    assert api.get(f"/api/v1/repos/{REPO}").json()["last_indexed_commit"] == HEAD


@pytest.mark.parametrize("action", ["deleted", "archived"])
def test_deleted_and_archived_repos_keep_their_index(api, github, action):
    resp = deliver(api, "repository", {"action": action, "repository": PUSH["repository"]})
    assert resp.json()["action"] == "ignored"
    assert github.requests == []


def test_a_repository_event_without_a_repository_answers_422(api):
    assert deliver(api, "repository", {"action": "edited"}).status_code == 422


def test_removed_components_are_listed_on_request(api, github, platform_contract):
    worker = platform_contract["rlp.yaml"].replace(
        "    telemetry:\n      enabled: true\n      serviceName: rlp-platform-platform\n",
        "    telemetry:\n      enabled: true\n      serviceName: rlp-platform-platform\n"
        "  - name: worker\n    language: python\n    artifact:\n      kind: container-image\n"
        "    runtime:\n      kind: job\n",
    )
    github.add(REPO, OLDER, {**platform_contract, "rlp.yaml": worker})
    deliver(api, "push", {**PUSH, "after": OLDER})
    github.add(REPO, HEAD, platform_contract)
    assert deliver(api, "push", PUSH).json()["result"]["removed"] == ["worker"]
    assert [c["name"] for c in components(api)] == ["platform"]
    listed = components(api, include_removed="true")
    assert [c["name"] for c in listed] == ["platform", "worker"]
    assert listed[1]["removed_at"] is not None
    project = api.get("/api/v1/projects/rlp-platform/components", params={"include_removed": "true"}).json()
    assert len(project) == 2


# --- sync ---


def test_sync_needs_the_bearer_token(app):
    with TestClient(app) as client:
        assert client.post("/api/v1/sync").status_code == 401


def test_sync_one_repo(api, github, platform_contract):
    github.add(REPO, HEAD, platform_contract)
    resp = api.post("/api/v1/sync", json={"repo": REPO})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert [r["status"] for r in resp.json()["results"]] == ["indexed"]
    assert api.post("/api/v1/sync", json={"repo": "not a repo"}).status_code == 422


def test_sync_every_installation_repo(api, github, platform_contract):
    github.installation = [REPO, "rdlucas2/broken"]
    github.add(REPO, HEAD, platform_contract)
    github.broken.add("rdlucas2/broken")
    resp = api.post("/api/v1/sync")
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["ok"] is False
    assert [(r["repo"], r["status"]) for r in doc["results"]] == [(REPO, "indexed"), ("rdlucas2/broken", "failed")]
    assert api.get(f"/api/v1/repos/{REPO}").json()["last_indexed_commit"] == HEAD


def test_sync_configured_repos(app, api, github, platform_contract):
    app.state.settings = dataclasses.replace(app.state.settings, github_repos=(REPO,))
    github.installation = ["rdlucas2/not-this-one"]
    github.add(REPO, HEAD, platform_contract)
    assert [r["repo"] for r in api.post("/api/v1/sync").json()["results"]] == [REPO]


def test_sync_without_a_token_or_repos_answers_502(app, api):
    app.state.settings = dataclasses.replace(app.state.settings, github_token=None)
    resp = api.post("/api/v1/sync")
    assert resp.status_code == 502
    assert "RLP_GITHUB_TOKEN" in resp.json()["detail"]


# --- python -m rlp_platform sync ---


def sync_output(out: str) -> dict:
    """The command's result line, among the JSON log lines on stdout."""
    [doc] = [d for d in map(json.loads, out.splitlines()) if "results" in d]
    return doc


def test_sync_command(monkeypatch, capsys, migrated_db, github, platform_contract):
    github.add(REPO, HEAD, platform_contract)
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv("RLP_GITHUB_REPOS", f"{REPO}, rdlucas2/missing")
    monkeypatch.setenv("RLP_GITHUB_API_URL", "https://api.github.test")
    real_sync = entrypoint.sync
    monkeypatch.setattr(entrypoint, "sync", lambda s, repo: real_sync(s, repo, transport=github.transport))
    assert entrypoint.main(["sync"]) == 1
    doc = sync_output(capsys.readouterr().out)
    assert [(r["repo"], r["status"]) for r in doc["results"]] == [(REPO, "indexed"), ("rdlucas2/missing", "failed")]
    assert entrypoint.main(["sync", REPO]) == 0
    assert sync_output(capsys.readouterr().out)["ok"] is True


def test_sync_command_errors(monkeypatch, settings, migrated_db, github):
    assert entrypoint.sync(settings, None) == 2  # no DATABASE_URL
    configured = dataclasses.replace(settings, database_url=migrated_db)
    assert entrypoint.sync(configured, "no-slash") == 2
    assert entrypoint.sync(configured, None, transport=github.transport) == 2  # no token, no repos
