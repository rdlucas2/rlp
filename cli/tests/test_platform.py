"""rlp config, project, repo and lifecycle, against a fake platform API through httpx's MockTransport.

The fake answers the routes and documents platform/openapi.json describes,
including the 401 for a wrong token and the 409 for a rejected move.
"""

from __future__ import annotations

import json
import os
import stat

import httpx
import pytest
from click.testing import CliRunner

from rlp_cli import platform_api
from rlp_cli.cli import cli

URL = "http://platform.test"
KEY = "aaaaaaaaaaaaaaaaaaaa5678"
NOW = "2026-10-20T12:00:00Z"


def rule(rule_id: str, passed: bool, description: str = "", messages: list[str] | None = None) -> dict:
    return {"rule": rule_id, "description": description, "passed": passed, "messages": messages or []}


class FakePlatform:
    """An in-memory /api/v1 with two projects and one repo."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.projects = {
            "orders": self.project("orders", phase="in-dev", description="The orders API"),
            "billing": self.project("billing", owner="rdlucas2"),
        }
        self.repos = {
            "acme/orders-api": {
                "full_name": "acme/orders-api",
                "project": "orders",
                "default_branch": "main",
                "template_source": "gh:rdlucas2/rlp-template-python-fastapi",
                "template_version": "v1",
                "last_indexed_commit": None,
                "created_at": NOW,
                "updated_at": NOW,
            }
        }
        self.history: dict[str, list[dict]] = {"orders": [], "billing": []}

    @staticmethod
    def project(name: str, *, phase: str = "proposed", owner: str = "", description: str = "") -> dict:
        return {
            "name": name,
            "description": description,
            "owner": owner,
            "phase": phase,
            "dashboard_url": None,
            "created_at": NOW,
            "updated_at": NOW,
        }

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {KEY}":
            return httpx.Response(401, json={"detail": "missing or wrong bearer token"})
        parts = request.url.path.removeprefix("/api/v1/").split("/")
        method = request.method
        body = json.loads(request.content) if request.content else None
        if parts == ["projects"] and method == "GET":
            phase = request.url.params.get("phase")
            return httpx.Response(
                200, json=[p for _, p in sorted(self.projects.items()) if phase in (None, p["phase"])]
            )
        if parts == ["projects"] and method == "POST":
            if body["name"] in self.projects:
                return httpx.Response(409, json={"detail": f"project {body['name']!r} already exists"})
            if not body["name"].islower():
                return httpx.Response(422, json={"detail": [{"loc": ["body", "name"], "msg": "bad name"}]})
            project = self.project(body["name"], owner=body["owner"], description=body["description"])
            project["dashboard_url"] = body["dashboard_url"]
            self.projects[project["name"]] = project
            self.history[project["name"]] = []
            return httpx.Response(201, json=project)
        if parts[0] == "projects":
            project = self.projects.get(parts[1])
            if project is None:
                return httpx.Response(404, json={"detail": f"project {parts[1]!r} not found"})
            if len(parts) == 2 and method == "GET":
                return httpx.Response(200, json=project)
            if len(parts) == 2 and method == "PATCH":
                project.update(body)
                return httpx.Response(200, json=project)
            if parts[2:] == ["transitions"] and method == "GET":
                return httpx.Response(200, json=self.history[project["name"]])
            if parts[2:] == ["transitions"] and method == "POST":
                return self.move(project, body)
        if parts == ["repos"]:
            wanted = request.url.params.get("project")
            if wanted is not None and wanted not in self.projects:
                return httpx.Response(404, json={"detail": f"project {wanted!r} not found"})
            return httpx.Response(200, json=[r for r in self.repos.values() if wanted in (None, r["project"])])
        if parts[0] == "repos":
            repo = self.repos.get(f"{parts[1]}/{parts[2]}")
            if repo is None:
                return httpx.Response(404, json={"detail": f"repo '{parts[1]}/{parts[2]}' not found"})
            if parts[3:] == ["components"]:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "repo": repo["full_name"],
                            "name": "api",
                            "language": "python",
                            "artifact_kind": "container-image",
                            "runtime_kind": "container-service",
                        }
                    ],
                )
            return httpx.Response(200, json=repo)
        return httpx.Response(500, text="the fake has no such route")  # pragma: no cover

    def move(self, project: dict, body: dict) -> httpx.Response:
        to = body["to"]
        results = [rule("RLP-L101", True, "the move is allowed")]
        if to == "live":
            results.append(
                rule(
                    "RLP-L105",
                    bool(project["owner"]),
                    "live needs an owner",
                    [] if project["owner"] else ['[RLP-L105] rlp-transition.json project.owner: no owner. Fix: set it.'],
                )
            )
            results.append(rule("RLP-L106", bool(project["dashboard_url"]), "live needs a dashboard"))
        failures = [r for r in results if not r["passed"]]
        if failures:
            return httpx.Response(
                409,
                json={
                    "detail": f"{project['name']}: {project['phase']} -> {to} failed "
                    + ", ".join(r["rule"] for r in failures),
                    "project": project["name"],
                    "from_phase": project["phase"],
                    "to_phase": to,
                    "failures": failures,
                    "results": results,
                },
            )
        moved = {
            "from_phase": project["phase"],
            "to_phase": to,
            "actor": body["actor"],
            "reason": body["reason"],
            "rule_results": results,
            "at": NOW,
        }
        project["phase"] = to
        self.history[project["name"]].append(moved)
        return httpx.Response(201, json=moved)


@pytest.fixture
def fake() -> FakePlatform:
    return FakePlatform()


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No platform settings from the real environment or home directory."""
    monkeypatch.delenv(platform_api.URL_ENV, raising=False)
    monkeypatch.delenv(platform_api.TOKEN_ENV, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "rlp" / "config.yaml"


@pytest.fixture
def configured(clean_env, monkeypatch):
    monkeypatch.setenv(platform_api.URL_ENV, URL)
    monkeypatch.setenv(platform_api.TOKEN_ENV, KEY)


def run(fake: FakePlatform | None, *args: str):
    obj = {"platform_transport": fake.transport()} if fake else {}
    return CliRunner().invoke(cli, list(args), obj=obj)


def write_config(path, doc: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")  # JSON is YAML
    path.chmod(mode)


# --- config ---


def test_config_from_the_environment(configured, clean_env):
    config = platform_api.load_config()
    assert (config.url, config.token, config.path) == (URL, KEY, clean_env)
    assert config.sources == {"url": f"env {platform_api.URL_ENV}", "token": f"env {platform_api.TOKEN_ENV}"}
    assert KEY not in repr(config)


def test_config_from_the_file_with_env_overriding_one_value(clean_env, monkeypatch):
    write_config(clean_env, {"platform": {"url": "http://from-file", "token": KEY}})
    monkeypatch.setenv(platform_api.URL_ENV, URL)
    config = platform_api.load_config()
    assert (config.url, config.token) == (URL, KEY)
    assert config.sources == {"url": f"env {platform_api.URL_ENV}", "token": f"file {clean_env}"}


def test_config_defaults_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert platform_api.config_path() == tmp_path / ".config" / "rlp" / "config.yaml"


@pytest.mark.skipif(os.name != "posix", reason="mode bits are POSIX")
def test_config_refuses_a_token_others_can_read(clean_env):
    write_config(clean_env, {"platform": {"url": URL, "token": KEY}}, mode=0o644)
    with pytest.raises(platform_api.ConfigError, match="chmod 600"):
        platform_api.load_config()
    assert stat.S_IMODE(clean_env.stat().st_mode) == 0o644


@pytest.mark.parametrize(
    ("text", "message"),
    [("platform: [1, 2]", "must be a mapping"), ("platform: {url: [", "not valid YAML")],
)
def test_config_rejects_a_malformed_file(clean_env, text, message):
    clean_env.parent.mkdir(parents=True)
    clean_env.write_text(text, encoding="utf-8")
    clean_env.chmod(0o600)
    with pytest.raises(platform_api.ConfigError, match=message):
        platform_api.load_config()


def test_config_without_a_platform_section(clean_env):
    write_config(clean_env, {"other": 1})
    assert platform_api.load_config().sources == {"url": "unset", "token": "unset"}


def test_mask():
    assert platform_api.mask(None) is None
    assert platform_api.mask("short") == "****"
    assert platform_api.mask(KEY) == "****5678"


def test_config_show_masks_the_token(configured):
    result = run(None, "config", "show")
    assert result.exit_code == 0
    assert KEY not in result.output
    assert f"platform.url     {URL}  (env RLP_PLATFORM_URL)" in result.output
    assert "platform.token   ****5678  (env RLP_PLATFORM_TOKEN)" in result.output


def test_config_show_json(clean_env):
    result = run(None, "config", "show", "--output", "json")
    doc = json.loads(result.output)
    assert doc["ok"] is True
    assert doc["path"] == clean_env.as_posix()
    assert doc["platform"] == {"url": {"value": None, "source": "unset"}, "token": {"value": None, "source": "unset"}}


def test_config_show_reports_a_bad_file(clean_env):
    write_config(clean_env, {"platform": {"token": KEY}}, mode=0o640)
    result = run(None, "config", "show")
    assert result.exit_code == 2
    assert "chmod 600" in result.output


def test_commands_need_a_url_and_a_token(clean_env, monkeypatch, fake):
    result = run(fake, "project", "list")
    assert result.exit_code == 2
    assert "no platform URL: set RLP_PLATFORM_URL" in result.output
    monkeypatch.setenv(platform_api.URL_ENV, URL)
    result = run(fake, "project", "list", "-o", "json")
    assert result.exit_code == 2
    assert json.loads(result.output) == {
        "ok": False,
        "error": f"no platform token: set RLP_PLATFORM_TOKEN or platform.token in {clean_env}",
    }
    assert fake.requests == []


def test_a_wrong_token_is_a_setup_error(configured, monkeypatch, fake):
    monkeypatch.setenv(platform_api.TOKEN_ENV, "wrong")
    result = run(fake, "project", "list")
    assert result.exit_code == 2
    assert "refused the token (401)" in result.output


def test_an_unreachable_platform_is_a_setup_error(configured):
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    result = CliRunner().invoke(cli, ["project", "list"], obj={"platform_transport": httpx.MockTransport(refuse)})
    assert result.exit_code == 2
    assert f"cannot reach the platform at {URL}" in result.output


# --- project ---


def test_project_list(configured, fake):
    result = run(fake, "project", "list")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "NAME     PHASE     OWNER",
        "billing  proposed  rdlucas2",
        "orders   in-dev    -",
    ]
    assert fake.requests[0].headers["authorization"] == f"Bearer {KEY}"


def test_project_list_by_phase_json(configured, fake):
    result = run(fake, "project", "list", "--phase", "in-dev", "-o", "json")
    doc = json.loads(result.output)
    assert [p["name"] for p in doc["projects"]] == ["orders"]
    assert fake.requests[0].url.params["phase"] == "in-dev"


def test_project_show(configured, fake):
    result = run(fake, "project", "show", "orders")
    assert result.exit_code == 0
    assert "phase          in-dev" in result.output
    assert "owner          -" in result.output
    assert "repos          acme/orders-api" in result.output
    doc = json.loads(run(fake, "project", "show", "orders", "-o", "json").output)
    assert doc["project"]["name"] == "orders"
    assert [r["full_name"] for r in doc["repos"]] == ["acme/orders-api"]


def test_project_show_unknown_exits_1(configured, fake):
    result = run(fake, "project", "show", "nope")
    assert result.exit_code == 1
    assert "project 'nope' not found (404)" in result.output


def test_project_create(configured, fake):
    result = run(fake, "project", "create", "shop", "--owner", "rdlucas2", "--description", "A shop")
    assert result.exit_code == 0, result.output
    assert result.output.startswith("created project shop\n")
    assert json.loads(fake.requests[0].content) == {
        "name": "shop",
        "owner": "rdlucas2",
        "description": "A shop",
        "dashboard_url": None,
    }
    duplicate = run(fake, "project", "create", "shop", "-o", "json")
    assert duplicate.exit_code == 1
    assert json.loads(duplicate.output)["error"] == "project 'shop' already exists (409)"


def test_project_create_reports_a_validation_error(configured, fake):
    result = run(fake, "project", "create", "Shop")
    assert result.exit_code == 1
    assert "bad name" in result.output
    assert "(422)" in result.output


def test_project_update(configured, fake):
    result = run(fake, "project", "update", "orders", "--owner", "rdlucas2", "--dashboard-url", "https://d/o", "-o", "json")
    assert result.exit_code == 0, result.output
    assert json.loads(fake.requests[0].content) == {"owner": "rdlucas2", "dashboard_url": "https://d/o"}
    project = json.loads(result.output)["project"]
    assert (project["owner"], project["dashboard_url"]) == ("rdlucas2", "https://d/o")
    cleared = run(fake, "project", "update", "orders", "--dashboard-url", "")
    assert cleared.exit_code == 0
    assert json.loads(fake.requests[1].content) == {"dashboard_url": None}
    assert "updated project orders" in cleared.output


def test_project_update_needs_a_change(configured, fake):
    result = run(fake, "project", "update", "orders")
    assert result.exit_code == 2
    assert "nothing to change" in result.output
    assert fake.requests == []


# --- repo ---


def test_repo_list(configured, fake):
    result = run(fake, "repo", "list")
    assert result.output.splitlines() == ["REPO             PROJECT  BRANCH", "acme/orders-api  orders   main"]
    assert json.loads(run(fake, "repo", "list", "--project", "billing", "-o", "json").output) == {"ok": True, "repos": []}
    assert run(fake, "repo", "list", "--project", "nope").exit_code == 1


def test_repo_show(configured, fake):
    result = run(fake, "repo", "show", "acme/orders-api")
    assert result.exit_code == 0, result.output
    assert "template_version     v1" in result.output
    assert "api        python    container-image  container-service" in result.output
    doc = json.loads(run(fake, "repo", "show", "acme/orders-api", "-o", "json").output)
    assert doc["repo"]["project"] == "orders"
    assert [c["name"] for c in doc["components"]] == ["api"]


def test_repo_show_unknown_and_malformed(configured, fake):
    assert run(fake, "repo", "show", "acme/nope").exit_code == 1
    result = run(fake, "repo", "show", "no-slash")
    assert result.exit_code == 2
    assert "a repo is OWNER/NAME" in result.output
    assert fake.requests[-1].url.path == "/api/v1/repos/acme/nope"


# --- lifecycle ---


def test_lifecycle_move_to_live_without_an_owner_exits_1_with_the_rules(configured, fake):
    result = run(fake, "lifecycle", "move", "orders", "live", "--reason", "launch")
    assert result.exit_code == 1
    assert "orders: in-dev -> live rejected" in result.output
    assert "[RLP-L105] rlp-transition.json project.owner: no owner. Fix: set it." in result.output
    assert "[RLP-L106] live needs a dashboard" in result.output
    assert "RLP-L101" not in result.output
    assert fake.projects["orders"]["phase"] == "in-dev"


def test_lifecycle_move_rejected_json(configured, fake):
    result = run(fake, "lifecycle", "move", "orders", "live", "--reason", "launch", "-o", "json")
    assert result.exit_code == 1
    doc = json.loads(result.output)
    assert doc["ok"] is False
    assert [r["rule"] for r in doc["failures"]] == ["RLP-L105", "RLP-L106"]
    assert doc["error"] == "orders: in-dev -> live failed RLP-L105, RLP-L106"


def test_lifecycle_move_and_show(configured, fake, monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "ryan")
    run(fake, "project", "update", "orders", "--owner", "rdlucas2", "--dashboard-url", "https://d/o")
    result = run(fake, "lifecycle", "move", "orders", "live", "--reason", "launch")
    assert result.exit_code == 0, result.output
    assert result.output == "orders: in-dev -> live\n"
    assert json.loads(fake.requests[-1].content) == {"to": "live", "reason": "launch", "actor": "ryan"}

    shown = run(fake, "lifecycle", "show", "orders")
    assert shown.output.splitlines() == [
        "orders: live",
        "",
        "AT                    FROM    TO    BY    REASON",
        f"{NOW}  in-dev  live  ryan  launch",
    ]
    doc = json.loads(run(fake, "lifecycle", "show", "orders", "-o", "json").output)
    assert (doc["phase"], len(doc["history"])) == ("live", 1)


def test_lifecycle_move_json_and_show_without_history(configured, fake):
    result = run(fake, "lifecycle", "move", "billing", "designed", "--reason", "spec'd", "-o", "json")
    doc = json.loads(result.output)
    assert (doc["ok"], doc["transition"]["to_phase"]) == (True, "designed")
    assert run(fake, "lifecycle", "show", "orders").output == "orders: in-dev\n"


def test_lifecycle_move_needs_a_reason(configured, fake):
    result = run(fake, "lifecycle", "move", "orders", "live")
    assert result.exit_code == 2
    assert "--reason" in result.output


def test_lifecycle_move_unknown_project(configured, fake):
    result = run(fake, "lifecycle", "move", "nope", "live", "--reason", "x")
    assert result.exit_code == 1
    assert "project 'nope' not found" in result.output


# --- the client on its own ---


def test_client_reports_a_non_json_error():
    transport = httpx.MockTransport(lambda request: httpx.Response(502, text="bad gateway"))
    with platform_api.PlatformClient(URL, KEY, transport=transport) as client, pytest.raises(platform_api.PlatformError) as err:
        client.list_projects()
    assert (str(err.value), err.value.status) == ("bad gateway (502)", 502)


def test_client_passes_a_409_without_rules_through():
    transport = httpx.MockTransport(lambda request: httpx.Response(409, json={"detail": "busy"}))
    with platform_api.PlatformClient(URL + "/", KEY, transport=transport) as client:
        with pytest.raises(platform_api.PlatformError) as err:
            client.move("orders", "live")
        assert not isinstance(err.value, platform_api.TransitionRejected)
        assert client.url == URL


def test_client_quotes_path_segments(fake):
    with platform_api.PlatformClient(URL, KEY, transport=fake.transport()) as client:
        with pytest.raises(platform_api.PlatformError):
            client.get_project("a/b")
    assert fake.requests[0].url.raw_path == b"/api/v1/projects/a%2Fb"
