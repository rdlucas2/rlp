"""rlp pipeline report: builds and deployments sent to a fake platform, with no platform, and with an unreachable one."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml
from click.testing import CliRunner

from rlp_cli import pipeline, pipeline_report, platform_api, publish
from rlp_cli.cli import cli

URL = "http://platform.test"
TOKEN = "aaaaaaaaaaaaaaaaaaaa5678"
SHA = "c" * 40
DIGEST = "ghcr.io/acme/orders-api/demo-api@sha256:" + "1" * 64
GITHUB_ENV = {
    "GITHUB_REPOSITORY": "acme/orders-api",
    "GITHUB_SHA": SHA,
    "GITHUB_REF_NAME": "main",
    "GITHUB_RUN_ID": "4242",
    "GITHUB_RUN_ATTEMPT": "2",
    "GITHUB_SERVER_URL": "https://github.com",
}


class FakePlatform:
    """POST /builds and /deployments, keyed the way the platform keys them."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.builds: dict[tuple, dict] = {}
        self.deployments: dict[tuple, dict] = {}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"detail": "missing or wrong bearer token"})
        body = json.loads(request.content)
        if request.url.path == "/api/v1/builds":
            key = (body["repo"], body["commit"], body["run_id"], body["run_attempt"])
            build = self.builds.setdefault(key, {"id": len(self.builds) + 1, "stages": {}, "artifacts": []})
            build["stages"].update({r["stage"]: r for r in body["results"]})
            build["artifacts"] += [a for a in body["artifacts"] if a not in build["artifacts"]]
            failed = [s for s, r in build["stages"].items() if r["status"] == "failed"]
            build.update(
                repo=body["repo"], commit=body["commit"], run_id=body["run_id"], run_attempt=body["run_attempt"],
                passed=bool(build["stages"]) and not failed, failed_stages=failed,
            )
            return httpx.Response(201, json=build)
        if request.url.path == "/api/v1/deployments":
            if body["repo"] != "acme/orders-api":
                return httpx.Response(404, json={"detail": f"repo {body['repo']!r} not found"})
            key = (body["component"], body["environment"], body["artifact"]["reference"], body["deployed_at"])
            doc = self.deployments.setdefault(key, {"id": len(self.deployments) + 1})
            doc.update({k: body[k] for k in ("repo", "component", "environment", "artifact", "status")})
            return httpx.Response(201, json=doc)
        return httpx.Response(500, text="no such route")

    def posted(self, path: str) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.url.path == path]


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A GitHub Actions run with the platform configured; no config file."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in ("GITHUB_HEAD_REF", "RLP_REGISTRY", "RLP_DEPLOY_ENV"):
        monkeypatch.delenv(key, raising=False)
    for key, value in GITHUB_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(platform_api.URL_ENV, URL)
    monkeypatch.setenv(platform_api.TOKEN_ENV, TOKEN)
    monkeypatch.setenv("RLP_VERSION", "v1")
    return monkeypatch


def stage(name: str, status: str = "passed", **extra) -> dict:
    return {"stage": name, "status": status, "detail": "", "seconds": 0.1, "components": {}, "data": {}, **extra}


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    doc = {
        "apiVersion": "rlp/v1alpha1",
        "kind": "Repo",
        "metadata": {"name": "demo", "project": "demo"},
        "components": [
            {
                "name": "api",
                "language": "python",
                "artifact": {"kind": "container-image"},
                "runtime": {"kind": "container-service", "port": 8000},
            },
            {
                "name": "nightly",
                "language": "python",
                "artifact": {"kind": "container-image"},
                "runtime": {"kind": "job"},
            },
        ],
    }
    (root / "rlp.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return root


def results(root: Path, *entries: dict) -> None:
    pipeline.save_results(root, {e["stage"]: e for e in entries})


def invoke(fake: FakePlatform | None, *args: str, transport: httpx.BaseTransport | None = None):
    obj = {"platform_transport": transport or (fake.transport() if fake else None)}
    return CliRunner().invoke(cli, ["pipeline", "report", *args], obj=obj)


# --- builds ---


def test_reporting_a_run_twice_records_one_build(env, repo):
    fake = FakePlatform()
    results(repo, stage("precommit"), stage("lint"))
    for _ in range(2):
        result = invoke(fake, "--path", str(repo))
        assert result.exit_code == 0, result.output
    assert len(fake.builds) == 1
    assert "build 1: acme/orders-api@cccccccccccc run 4242 attempt 2: passed" in result.output
    sent = fake.posted("/api/v1/builds")[0]
    assert sent["run_url"] == "https://github.com/acme/orders-api/actions/runs/4242/attempts/2"
    assert [r["stage"] for r in sent["results"]] == ["precommit", "lint"]
    assert sent["branch"] == "main"


def test_a_failed_gate_is_reported_as_a_failed_build(env, repo):
    fake = FakePlatform()
    results(repo, stage("precommit"), stage("test", "failed", detail="1 failed"))
    result = invoke(fake, "--path", str(repo), "-o", "json")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert doc["build"]["passed"] is False
    assert doc["build"]["failed_stages"] == ["test"]


def test_published_artifacts_go_with_the_build(env, repo):
    fake = FakePlatform()
    results(repo, stage("setup"), stage("package"))
    publish.save(
        repo / pipeline.PUBLISHED,
        [
            {"component": "api", "kind": "container-image", "status": "published", "detail": "x", "reference": DIGEST},
            {"component": "site", "kind": "static", "status": "skipped", "detail": "static sites publish with their deploy"},
        ],
    )
    assert invoke(fake, "--path", str(repo)).exit_code == 0
    assert fake.posted("/api/v1/builds")[0]["artifacts"] == [
        {"component": "api", "kind": "container-image", "reference": DIGEST}
    ]


def test_pull_requests_report_the_head_commit_and_branch(env, repo):
    env.setenv("GITHUB_HEAD_REF", "feat/x")
    fake = FakePlatform()
    results(repo, stage("lint"))
    assert invoke(fake, "--path", str(repo), "--commit", "d" * 40).exit_code == 0
    sent = fake.posted("/api/v1/builds")[0]
    assert (sent["commit"], sent["branch"]) == ("d" * 40, "feat/x")


def test_nothing_to_report_is_a_no_op(env, repo):
    fake = FakePlatform()
    result = invoke(fake, "--path", str(repo))
    assert result.exit_code == 0
    assert "nothing reported" in result.output
    assert fake.requests == []


# --- no platform, an unreachable one, a refusal ---


def test_without_a_platform_it_does_nothing_and_exits_0(env, repo):
    env.delenv(platform_api.URL_ENV)
    env.delenv(platform_api.TOKEN_ENV)
    results(repo, stage("lint"))
    fake = FakePlatform()
    for args in ([], ["--deployment"]):
        result = invoke(fake, "--path", str(repo), *args)
        assert result.exit_code == 0, result.output
        assert "no platform configured" in result.output
    assert fake.requests == []


def test_an_unreachable_platform_warns_and_exits_0(env, repo):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    results(repo, stage("lint"))
    result = invoke(None, "--path", str(repo), transport=httpx.MockTransport(down))
    assert result.exit_code == 0
    assert "warning: cannot reach the platform" in result.output


def test_a_platform_error_warns_and_exits_0(env, repo):
    results(repo, stage("lint"))
    transport = httpx.MockTransport(lambda r: httpx.Response(503, json={"detail": "no database"}))
    result = invoke(None, "--path", str(repo), transport=transport)
    assert result.exit_code == 0
    assert "warning: no database (503)" in result.output


def test_a_refused_token_exits_1(env, repo):
    env.setenv(platform_api.TOKEN_ENV, "wrong-token-wrong-token")
    results(repo, stage("lint"))
    result = invoke(FakePlatform(), "--path", str(repo))
    assert result.exit_code == 1
    assert "refused the token" in result.output


def test_an_unknown_repo_is_a_usage_error(env, repo, tmp_path):
    env.delenv("GITHUB_REPOSITORY")
    results(repo, stage("lint"))
    result = invoke(FakePlatform(), "--path", str(repo))
    assert result.exit_code == 2
    assert "no repo" in result.output


# --- deployments ---


def deploy(status: str = "passed", components: dict | None = None, **data) -> dict:
    per = {"api": status} if components is None else components
    data = {"env": "dev", "deployed_at": "2026-10-20T12:00:00+00:00", "checks": [], **data}
    return stage("deploy", status, components=per, data=data)


def test_a_deployment_is_reported_per_deployed_component(env, repo):
    env.setenv("RLP_REGISTRY", "ghcr.io/acme/orders-api")
    fake = FakePlatform()
    results(repo, stage("terraform"), deploy())
    for _ in range(2):
        result = invoke(fake, "--path", str(repo), "--deployment")
        assert result.exit_code == 0, result.output
    assert len(fake.deployments) == 1
    sent = fake.posted("/api/v1/deployments")[0]
    assert sent == {
        "repo": "acme/orders-api",
        "commit": SHA,
        "run_id": "4242",
        "run_attempt": 2,
        "component": "api",
        "environment": "dev",
        "artifact": {"kind": "container-image", "reference": "ghcr.io/acme/orders-api/demo-api:v1"},
        "status": "succeeded",
        "deployed_at": "2026-10-20T12:00:00+00:00",
    }
    assert "deployment 1: acme/orders-api api to dev: succeeded" in result.output


def test_a_deploy_that_failed_before_readiness_reports_failed(env, repo):
    fake = FakePlatform()
    entry = stage("deploy", "failed", detail="terraform apply failed")  # no components, no data
    results(repo, entry)
    assert invoke(fake, "--path", str(repo), "--deployment", "--deploy-env", "dev").exit_code == 0
    sent = fake.posted("/api/v1/deployments")
    assert [(d["component"], d["status"], d["environment"]) for d in sent] == [("api", "failed", "dev")]
    assert sent[0]["deployed_at"]  # from results.json's time


@pytest.mark.parametrize("entries", [[stage("terraform")], [deploy("skipped", {})]], ids=["not-run", "skipped"])
def test_no_deploy_stage_reports_nothing(env, repo, entries):
    fake = FakePlatform()
    results(repo, *entries)
    result = invoke(fake, "--path", str(repo), "--deployment")
    assert result.exit_code == 0
    assert "no deploy stage ran" in result.output
    assert fake.requests == []


def test_an_unindexed_repos_deployment_exits_1(env, repo):
    env.setenv("GITHUB_REPOSITORY", "acme/elsewhere")
    results(repo, deploy())
    result = invoke(FakePlatform(), "--path", str(repo), "--deployment")
    assert result.exit_code == 1
    assert "not found" in result.output


# --- run resolution ---


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/acme/orders-api.git", "acme/orders-api"),
        ("git@github.com:acme/orders-api.git", "acme/orders-api"),
        ("https://github.com/acme/orders-api", "acme/orders-api"),
        ("ssh://git@github.com/acme/orders-api/", "acme/orders-api"),
        ("not a url", ""),
    ],
)
def test_repo_from_remote(url, expected):
    assert pipeline_report.repo_from_remote(url) == expected


def test_outside_actions_a_run_is_local(tmp_path):
    run = pipeline_report.resolve_run(tmp_path, {}, repo="acme/x", commit="abc", branch="main")
    assert (run.run_id, run.run_attempt, run.run_url) == ("local", 1, None)


def test_a_bad_run_attempt_is_an_error(tmp_path):
    with pytest.raises(pipeline_report.ReportError, match="GITHUB_RUN_ATTEMPT"):
        pipeline_report.resolve_run(tmp_path, {"GITHUB_RUN_ATTEMPT": "x"}, repo="a/b", commit="c", branch="d")


# --- publish references and the deploy stage's time ---


def test_publish_records_the_image_digest(monkeypatch, tmp_path):
    calls = []

    def run(args, cwd):
        calls.append(args)
        if args[:2] == ["docker", "push"]:
            return "The push refers to repository [...]\nv1: digest: sha256:" + "1" * 64 + " size: 1789\n"
        return ""

    monkeypatch.setattr(publish, "_run", run)
    doc = {"metadata": {"name": "demo"}, "components": [{"name": "api", "artifact": {"kind": "container-image"}}]}
    monkeypatch.setenv("RLP_VERSION", "v1")
    done = publish.publish(tmp_path, doc, "ghcr.io/acme/orders-api", None)
    assert done[0]["reference"] == DIGEST
    assert ["docker", "push", "ghcr.io/acme/orders-api/demo-api:v1"] in calls


def test_publish_falls_back_to_the_tag_without_a_digest(monkeypatch, tmp_path):
    monkeypatch.setattr(publish, "_run", lambda args, cwd: "")
    doc = {"metadata": {"name": "demo"}, "components": [{"name": "api", "artifact": {"kind": "container-image"}}]}
    monkeypatch.setenv("RLP_VERSION", "v1")
    assert publish.publish(tmp_path, doc, "localhost:5000/acme", None)[0]["reference"] == "localhost:5000/acme/demo-api:v1"


def test_publish_records_the_release_asset_url(monkeypatch, tmp_path):
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "fn.zip").write_bytes(b"zip")
    monkeypatch.setattr(publish, "_run", lambda args, cwd: "")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/x")
    doc = {"metadata": {"name": "demo"}, "components": [{"name": "fn", "artifact": {"kind": "zip", "path": "dist/fn.zip"}}]}
    assert publish.publish(tmp_path, doc, None, "v1")[0]["reference"] == "https://github.com/acme/x/releases/download/v1/fn.zip"
    monkeypatch.delenv("GITHUB_REPOSITORY")
    assert publish.publish(tmp_path, doc, None, "v1")[0]["reference"] == "release v1: dist/fn.zip"


@pytest.mark.parametrize(
    ("remote", "expected"),
    [("ghcr.io/a/b:v1", "ghcr.io/a/b"), ("localhost:5000/b:v1", "localhost:5000/b"), ("localhost:5000/b", "localhost:5000/b")],
)
def test_repository_strips_only_the_tag(remote, expected):
    assert publish.repository(remote) == expected


def test_publish_command_writes_published_json(monkeypatch, repo):
    monkeypatch.setattr(publish, "_run", lambda args, cwd: "")
    monkeypatch.setenv("RLP_VERSION", "v1")
    result = CliRunner().invoke(cli, ["publish", "--path", str(repo), "--registry", "ghcr.io/acme/x", "-c", "api"])
    assert result.exit_code == 0, result.output
    saved = publish.load(repo / pipeline.PUBLISHED)
    assert [(r["component"], r["reference"]) for r in saved] == [("api", "ghcr.io/acme/x/demo-api:v1")]


def test_a_reset_run_forgets_what_was_published(repo):
    publish.save(repo / pipeline.PUBLISHED, [{"component": "api"}])
    pipeline.run(pipeline.Context(root=repo), [], reset=True)
    assert not (repo / pipeline.PUBLISHED).exists()


def test_golden_reports_only_with_a_platform_and_never_fails_the_pipeline():
    """The report steps are skipped without RLP_PLATFORM_URL (golden-selftest runs them that way) and can't fail a run."""
    workflow = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github" / "workflows" / "golden.yml").read_text())
    steps = {
        (job, s["name"]): s
        for job, spec in workflow["jobs"].items()
        for s in spec["steps"]
        if "rlp pipeline report" in s.get("run", "")
    }
    assert set(steps) == {
        ("gates", "Report the build to the platform"),
        ("publish", "Report the artifacts to the platform"),
        ("deploy", "Report the deployment to the platform"),
        ("destroy", "Report the teardown to the platform"),
    }
    for step in steps.values():
        assert step["continue-on-error"] is True
        assert step["if"].startswith("always() && ")
        assert "(inputs.platform-url || vars.RLP_PLATFORM_URL) != ''" in step["if"]
        assert step["env"]["RLP_PLATFORM_TOKEN"] == "${{ secrets.RLP_PLATFORM_TOKEN }}"
        assert 'if [ -z "$RLP_PLATFORM_TOKEN" ]' in step["run"]
    assert "--deployment" in steps[("deploy", "Report the deployment to the platform")]["run"]
    assert '--teardown "$status"' in steps[("destroy", "Report the teardown to the platform")]["run"]
    # The token reaches only the report steps, never a job's env (where the repo's own verbs would see it).
    for spec in workflow["jobs"].values():
        assert "RLP_PLATFORM_TOKEN" not in (spec.get("env") or {})
    secrets = workflow[True]["workflow_call"]["secrets"]  # PyYAML reads the `on:` key as True
    assert secrets["RLP_PLATFORM_TOKEN"]["required"] is False
