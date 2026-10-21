"""rlp lifecycle sweep, against a fake platform; and the nightly retirement-sweep workflow."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml
from click.testing import CliRunner

from rlp_cli import platform_api, resources
from rlp_cli.cli import cli, retirement_idle_days

URL = "http://platform.test"
KEY = "aaaaaaaaaaaaaaaaaaaa5678"
WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "retirement-sweep.yml"


def result(project: str, kind: str, requests: int | None, detail: str, **extra) -> dict:
    return {
        "project": project,
        "result": kind,
        "detail": detail,
        "environments": ["dev"],
        "requests": requests,
        "issue_url": None,
        "issue_error": None,
        **extra,
    }


def sweep_doc(results: list[dict], *, dry_run: bool = False) -> dict:
    return {
        "disabled": None,
        "dry_run": dry_run,
        "idle_days": 30,
        "window_start": "2026-09-21T03:00:00Z",
        "window_end": "2026-10-21T03:00:00Z",
        "run_id": None if dry_run else 4,
        "results": results,
    }


class FakePlatform:
    def __init__(self, answer: dict) -> None:
        self.answer = answer
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/v1/lifecycle/sweeps":
            return httpx.Response(200, json=[])
        assert request.url.path == "/api/v1/lifecycle/sweep" and request.method == "POST"
        return httpx.Response(200, json=self.answer)

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.method == "POST"]


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv(platform_api.URL_ENV, URL)
    monkeypatch.setenv(platform_api.TOKEN_ENV, KEY)
    monkeypatch.setenv("USER", "rdlucas2")
    monkeypatch.setenv("LOGNAME", "rdlucas2")
    monkeypatch.delenv("RLP_LOOPS", raising=False)


def run(fake: FakePlatform, *args: str):
    return CliRunner().invoke(cli, ["lifecycle", "sweep", *args], obj={"platform_transport": fake.transport()})


IDLE = result("orders", "idle", 0, "no requests in the last 30 days", issue_url="https://github.test/acme/o/issues/1")
ACTIVE = result("web", "active", 12, "12 request(s) in the last 30 days")
UNKNOWN = result("lab", "unknown", None, "no AWS deployment on record, so its traffic can't be judged")


def test_the_idle_window_is_read_from_the_platform_data():
    data = yaml.safe_load((resources.policy_dir() / "platform" / "data.yaml").read_text(encoding="utf-8"))
    assert retirement_idle_days() == data["retirement_idle_days"] == 30


def test_the_platform_default_matches_the_platform_data():
    sweep_py = Path(__file__).resolve().parents[2] / "platform" / "src" / "rlp_platform" / "sweep.py"
    assert f"DEFAULT_IDLE_DAYS = {retirement_idle_days()}\n" in sweep_py.read_text(encoding="utf-8")


def test_sweep_sends_the_window_and_lists_each_project(configured):
    fake = FakePlatform(sweep_doc([UNKNOWN, ACTIVE, IDLE]))
    out = run(fake)
    assert out.exit_code == 0, out.output
    assert fake.bodies() == [{"actor": "rdlucas2", "dry_run": False, "idle_days": 30}]
    lines = out.output.splitlines()
    assert "no traffic in 30 days makes a deprecated project idle" in lines[0]
    rows = [line.split()[:3] for line in lines[3:]]
    assert rows == [["orders", "idle", "0"], ["web", "active", "12"], ["lab", "unknown", "-"]]
    assert "https://github.test/acme/o/issues/1" in out.output


def test_dry_run(configured):
    fake = FakePlatform(sweep_doc([IDLE], dry_run=True))
    out = run(fake, "--dry-run")
    assert out.exit_code == 0
    assert fake.bodies()[0]["dry_run"] is True
    assert "dry run: nothing recorded" in out.output


def test_no_deprecated_projects(configured):
    out = run(FakePlatform(sweep_doc([])))
    assert out.exit_code == 0 and "no deprecated projects" in out.output


def test_a_failed_issue_exits_1(configured):
    failed = {**IDLE, "issue_url": None, "issue_error": "opening an issue in acme/o: GitHub answered 403"}
    out = run(FakePlatform(sweep_doc([failed])))
    assert out.exit_code == 1 and "issue not filed" in out.output
    out = run(FakePlatform(sweep_doc([failed])), "--output", "json")
    assert out.exit_code == 1 and json.loads(out.output)["ok"] is False


def test_json(configured):
    out = run(FakePlatform(sweep_doc([IDLE, ACTIVE])), "--output", "json")
    doc = json.loads(out.output)
    assert out.exit_code == 0 and doc["ok"] is True and doc["sweep"]["run_id"] == 4


def test_the_platform_kill_switch_is_reported(configured):
    doc = {**sweep_doc([]), "disabled": "RLP_LOOPS=off turns every loop off, the retirement sweep included"}
    out = run(FakePlatform(doc))
    assert out.exit_code == 0 and "retirement sweep disabled: RLP_LOOPS=off" in out.output


def test_the_local_kill_switch_calls_nothing(configured, monkeypatch):
    monkeypatch.setenv("RLP_LOOPS", "off")
    fake = FakePlatform(sweep_doc([IDLE]))
    out = run(fake)
    assert out.exit_code == 0 and "disabled" in out.output and fake.requests == []
    out = run(fake, "--output", "json")
    assert json.loads(out.output)["sweep"]["disabled"].startswith("RLP_LOOPS=off") and fake.requests == []


def test_client_lists_sweeps(configured):
    fake = FakePlatform(sweep_doc([]))
    with platform_api.PlatformClient.from_config(platform_api.load_config(), transport=fake.transport()) as client:
        assert client.list_sweeps(limit=5) == []
    assert fake.requests[0].url.params["limit"] == "5"


# --- the nightly workflow ---


def test_the_workflow_runs_nightly_and_obeys_the_kill_switch():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = doc[True]  # YAML 1.1 reads the `on` key as True
    assert triggers["schedule"][0]["cron"].split()[2:] == ["*", "*", "*"]  # every day
    assert "workflow_dispatch" in triggers
    job = doc["jobs"]["sweep"]
    assert "vars.RLP_LOOPS" in job["if"] and '"off"' in job["if"]
    assert job["permissions"] == {"contents": "read"}
    script = "\n".join(step.get("run", "") for step in job["steps"])
    assert "rlp lifecycle sweep" in script
    assert "rlp lifecycle retire" not in script  # the sweep only flags
