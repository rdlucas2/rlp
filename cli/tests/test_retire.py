"""rlp lifecycle retire and rlp pipeline report --teardown, against fake platforms; and the golden destroy job."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml
from click.testing import CliRunner

from rlp_cli import pipeline_report, platform_api, resources
from rlp_cli.cli import artifact_retention_days, cli

URL = "http://platform.test"
KEY = "aaaaaaaaaaaaaaaaaaaa5678"
NOW = "2026-10-21T12:00:00Z"
GOLDEN = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "golden.yml"


def step(kind: str, status: str = "pending", repo: str | None = None, env: str | None = None, detail: str = "") -> dict:
    return {
        "kind": kind,
        "repo": repo,
        "environment": env,
        "status": status,
        "detail": detail,
        "started_at": None,
        "updated_at": None,
    }


def retirement(state: str, steps: list[dict], *, rejected: dict | None = None) -> dict:
    return {
        "project": "orders",
        "state": state,
        "actor": "rdlucas2",
        "reason": "",
        "artifact_retention_days": 30,
        "started_at": None if state == "planned" else NOW,
        "finished_at": NOW if state == "done" else None,
        "steps": steps,
        "rejected": rejected,
    }


class FakePlatform:
    """GET and POST /projects/{name}/retirement, answering what `answer` holds."""

    def __init__(self, answer: dict | None = None, status: int = 200) -> None:
        self.answer = answer
        self.status = status
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.path == "/api/v1/projects/orders/retirement"
        if self.answer is None:
            return httpx.Response(404, json={"detail": "project 'orders' has no retirement"})
        return httpx.Response(self.status, json=self.answer)

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.method == "POST"]


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv(platform_api.URL_ENV, URL)
    monkeypatch.setenv(platform_api.TOKEN_ENV, KEY)
    monkeypatch.setenv("USER", "rdlucas2")
    monkeypatch.setenv("LOGNAME", "rdlucas2")


def run(fake: FakePlatform, *args: str):
    return CliRunner().invoke(cli, ["lifecycle", "retire", *args], obj={"platform_transport": fake.transport()})


PLAN = [
    step("teardown", repo="acme/orders-api", env="dev"),
    step("archive", repo="acme/orders-api"),
    step("expire"),
]


def test_the_retention_is_read_from_the_platform_data():
    data = yaml.safe_load((resources.policy_dir() / "platform" / "data.yaml").read_text(encoding="utf-8"))
    assert artifact_retention_days() == data["artifact_retention_days"] == 30


def test_without_yes_it_shows_the_plan(configured):
    fake = FakePlatform(retirement("planned", PLAN))
    result = run(fake, "orders", "--reason", "nobody uses it")
    assert result.exit_code == 0, result.output
    assert fake.bodies() == [
        {"actor": "rdlucas2", "reason": "nobody uses it", "dry_run": True, "artifact_retention_days": 30}
    ]
    assert "orders: retirement planned; artifacts kept 30 days" in result.output
    assert "teardown  acme/orders-api  dev" in result.output
    assert "run it again with --yes to start" in result.output


def test_yes_starts_it_and_says_what_to_wait_for(configured):
    steps = [step("teardown", "running", "acme/orders-api", "dev", "dispatched pipeline.yml"), *PLAN[1:]]
    fake = FakePlatform(retirement("waiting", steps))
    result = run(fake, "orders", "--yes", "--workflow", "golden-caller.yml")
    assert result.exit_code == 0, result.output
    body = fake.bodies()[0]
    assert body["dry_run"] is False
    assert body["workflow"] == "golden-caller.yml"
    assert "running" in result.output and "once they have reported" in result.output


def test_a_failed_step_exits_1(configured):
    steps = [step("teardown", "failed", "acme/orders-api", "dev", "the pipeline reported destroy-failed for api")]
    result = run(FakePlatform(retirement("failed", steps)), "orders", "--yes")
    assert result.exit_code == 1
    assert "destroy-failed for api" in result.output
    assert "run this again with --yes to retry" in result.output


def test_done(configured):
    steps = [{**s, "status": "succeeded"} for s in PLAN]
    result = run(FakePlatform(retirement("done", steps)), "orders", "--yes", "--output", "json")
    assert result.exit_code == 0
    doc = json.loads(result.output)
    assert doc["ok"] is True and doc["retirement"]["state"] == "done"


def test_a_rejected_move_to_retired_exits_1_with_the_rules(configured):
    rejected = {
        "detail": "orders: deprecated -> retired failed RLP-L108",
        "project": "orders",
        "from_phase": "deprecated",
        "to_phase": "retired",
        "failures": [{"rule": "RLP-L108", "description": "retired needs a teardown", "passed": False, "messages": []}],
        "results": [],
    }
    steps = [{**s, "status": "succeeded"} for s in PLAN]
    result = run(FakePlatform(retirement("pending", steps, rejected=rejected)), "orders", "--yes")
    assert result.exit_code == 1
    assert "deprecated -> retired rejected" in result.output
    assert "[RLP-L108] retired needs a teardown" in result.output


def test_status_reads_and_changes_nothing(configured):
    fake = FakePlatform(retirement("waiting", PLAN))
    result = run(fake, "orders", "--status")
    assert result.exit_code == 0, result.output
    assert [r.method for r in fake.requests] == ["GET"]
    assert run(FakePlatform(None), "orders", "--status").exit_code == 1


def test_status_and_yes_together_is_a_usage_error(configured):
    fake = FakePlatform(retirement("waiting", PLAN))
    result = run(fake, "orders", "--status", "--yes")
    assert result.exit_code == 2
    assert fake.requests == []


def test_a_project_that_is_not_deprecated_exits_1(configured):
    fake = FakePlatform({"detail": "project 'orders' is live; only a deprecated project can be retired"}, status=409)
    result = run(fake, "orders", "--yes")
    assert result.exit_code == 1
    assert "only a deprecated project can be retired" in result.output


# --- rlp pipeline report --teardown ---


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for key, value in {
        "GITHUB_REPOSITORY": "acme/orders-api",
        "GITHUB_SHA": "c" * 40,
        "GITHUB_REF_NAME": "main",
        "GITHUB_RUN_ID": "77",
        "GITHUB_RUN_ATTEMPT": "1",
        platform_api.URL_ENV: URL,
        platform_api.TOKEN_ENV: KEY,
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("RLP_DEPLOY_ENV", raising=False)
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


def deployments_platform(sent: list[dict]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        assert request.url.path == "/api/v1/deployments"
        doc = {"id": len(sent), "artifact": {"kind": "container-image", "reference": "ghcr.io/x@sha256:1"}}
        return httpx.Response(201, json={**doc, **{k: body[k] for k in ("repo", "component", "environment", "status")}})

    return httpx.MockTransport(handle)


@pytest.mark.parametrize("status", ["destroyed", "destroy-failed"])
def test_report_teardown_sends_each_aws_component_without_an_artifact(repo, status):
    sent: list[dict] = []
    result = CliRunner().invoke(
        cli,
        ["pipeline", "report", "--path", str(repo), "--teardown", status, "--deploy-env", "dev"],
        obj={"platform_transport": deployments_platform(sent)},
    )
    assert result.exit_code == 0, result.output
    assert len(sent) == 1  # the job doesn't deploy to AWS
    body = sent[0]
    assert "artifact" not in body
    assert {k: body[k] for k in ("repo", "component", "environment", "status", "run_id")} == {
        "repo": "acme/orders-api",
        "component": "api",
        "environment": "dev",
        "status": status,
        "run_id": "77",
    }
    assert body["deployed_at"]
    assert f"acme/orders-api api to dev: {status}" in result.output


def test_report_teardown_needs_an_environment(repo):
    result = CliRunner().invoke(
        cli, ["pipeline", "report", "--path", str(repo), "--teardown", "destroyed"], obj={"platform_transport": None}
    )
    assert result.exit_code == 2
    assert "no environment" in result.output


def test_teardowns_reject_another_status(repo):
    run_ = pipeline_report.resolve_run(repo)
    with pytest.raises(pipeline_report.ReportError, match="destroyed or destroy-failed"):
        pipeline_report.teardowns(repo, run_, "succeeded", deploy_env="dev")


def test_report_teardown_with_nothing_on_aws(repo):
    doc = yaml.safe_load((repo / "rlp.yaml").read_text())
    doc["components"] = doc["components"][1:]
    (repo / "rlp.yaml").write_text(yaml.safe_dump(doc))
    result = CliRunner().invoke(
        cli,
        ["pipeline", "report", "--path", str(repo), "--teardown", "destroyed", "--deploy-env", "dev"],
        obj={"platform_transport": deployments_platform([])},
    )
    assert result.exit_code == 0
    assert "nothing deploys to AWS; nothing reported" in result.output


# --- the golden pipeline's destroy job ---


def test_golden_destroys_only_from_a_dispatch_on_the_default_branch():
    workflow = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
    inputs = workflow[True]["workflow_call"]["inputs"]  # PyYAML reads the `on:` key as True
    assert inputs["destroy-env"] == {"description": inputs["destroy-env"]["description"], "type": "string", "default": ""}
    jobs = workflow["jobs"]
    # A teardown run runs nothing else: gates is skipped, and publish, deploy and gate-review need gates.
    assert jobs["gates"]["if"] == "inputs.destroy-env == ''"
    for job in ("gate-review", "publish", "deploy"):
        assert "gates" in jobs[job]["needs"]
    destroy = jobs["destroy"]
    assert "inputs.destroy-env != ''" in destroy["if"]
    assert "github.event_name == 'workflow_dispatch'" in destroy["if"]
    assert "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)" in destroy["if"]
    assert destroy["concurrency"]["group"] == "rlp-deploy-${{ github.repository }}-${{ inputs.destroy-env }}"
    steps = {s.get("name"): s for s in destroy["steps"]}
    assert 'rlp infra destroy --env "$RLP_DESTROY_ENV"' in steps["destroy"]["run"]
    assert "--auto-approve" in steps["destroy"]["run"]
    assert destroy["env"]["DEPLOY_ROLE"] == "${{ inputs.aws-deploy-role-arn }}"
    report = steps["Report the teardown to the platform"]
    assert report["env"]["DESTROY_OUTCOME"] == "${{ steps.destroy.outcome }}"
    assert 'status=destroyed; else status=destroy-failed' in report["run"]
    # Same Terraform as the deploy job.
    assert destroy["env"]["TERRAFORM_VERSION"] == jobs["deploy"]["env"]["TERRAFORM_VERSION"]


def test_rlps_own_pipeline_accepts_teardowns_and_reports():
    """rlp is project #1: its caller takes destroy-env from a dispatch and passes the platform token on (#113)."""
    caller = yaml.safe_load((GOLDEN.parent / "pipeline.yml").read_text(encoding="utf-8"))
    dispatch = caller[True]["workflow_dispatch"]["inputs"]["destroy-env"]  # PyYAML reads `on:` as True
    assert (dispatch["type"], dispatch["default"]) == ("string", "")
    golden = caller["jobs"]["golden"]
    assert golden["with"]["destroy-env"] == "${{ inputs.destroy-env || '' }}"
    assert golden["secrets"]["RLP_PLATFORM_TOKEN"] == "${{ secrets.RLP_PLATFORM_TOKEN }}"
    assert golden["permissions"]["id-token"] == "write"  # the destroy job assumes the deploy role
