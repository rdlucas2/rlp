"""The cost stage: Infracost's estimate of a plan, and the optional monthly budget.

Infracost's pricing API is never called here: tests replace cost.run with a
function that returns a report shaped like `infracost breakdown --format json`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from rlp_cli import cost, infra, pipeline, tools

def report(total: str | None, past: str | None = None, resources: list[tuple[str, str | None]] = (), unsupported: int = 0) -> dict:
    return {
        "version": "0.2",
        "currency": "USD",
        "totalMonthlyCost": total,
        "pastTotalMonthlyCost": past,
        "projects": [
            {
                "name": "plan.json",
                "breakdown": {"resources": [{"name": n, "monthlyCost": c} for n, c in resources]},
            }
        ],
        "summary": {"totalDetectedResources": len(resources) + unsupported + 4, "totalUnsupportedResources": unsupported, "totalNoPriceResources": 4},
    }


# --- parsing Infracost's JSON ---


def test_parse_totals_change_and_top_resources():
    est = cost.parse(report("15.50", "9.25", [("module.api.aws_cloudwatch_log_group.this", "0.5"), ("module.api.aws_ecs_service.this", "15.0"), ("x", None)], unsupported=1))
    assert est.monthly == 15.5
    assert est.past == 9.25
    assert est.change == pytest.approx(6.25)
    assert [r["name"] for r in est.resources] == ["module.api.aws_ecs_service.this", "module.api.aws_cloudwatch_log_group.this"]
    assert est.unsupported == 1
    assert est.free == 4
    text = est.text()
    assert text.startswith("$15.50/month (+$6.25 vs. now)")
    assert "1 resource(s) not priced" in text


def test_parse_without_a_baseline_has_no_change():
    est = cost.parse(report("3", None))
    assert est.past is None and est.change is None
    assert "vs. now" not in est.text()


def test_parse_a_cheaper_plan():
    assert "(-$2.00 vs. now)" in cost.parse(report("8", "10")).text()


def test_parse_rejects_output_without_projects():
    with pytest.raises(cost.CostError):
        cost.parse({"totalMonthlyCost": "1"})


def test_budget_comes_from_quality_cost():
    assert cost.budget({"quality": {"cost": {"monthlyBudget": 25}}}) == 25.0
    assert cost.budget({"quality": {"coverage": {"minimum": 80}}}) is None
    assert cost.budget({}) is None


def test_infracost_is_pinned_for_every_platform_trivy_is():
    assert set(tools.INFRACOST) == set(tools.TRIVY)
    assert all(len(sha) == 64 for _, sha, _ in tools.INFRACOST.values())


# --- the stage ---


def write_repo(root: Path, budget: float | None = None) -> Path:
    doc = {
        "apiVersion": "rlp/v1alpha1",
        "kind": "Repo",
        "metadata": {"name": "demo", "project": "demo"},
        "components": [
            {"name": "api", "language": "python", "artifact": {"kind": "container-image"}, "runtime": {"kind": "container-service", "port": 8000}}
        ],
    }
    if budget is not None:
        doc["quality"] = {"cost": {"monthlyBudget": budget}}
    root.mkdir(parents=True)
    (root / "rlp.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return root


def with_plan(root: Path, changes: int = 0) -> Path:
    plan = infra.stack_dir(root, "dev") / "policy" / "plan.json"
    plan.parent.mkdir(parents=True)
    rcs = [{"address": f"r{i}", "change": {"actions": ["create"]}} for i in range(changes)]
    plan.write_text(json.dumps({"resource_changes": rcs}), encoding="utf-8")
    return root


def run_cost(root: Path, **ctx) -> pipeline.StageResult:
    ctx.setdefault("deploy_env", "dev")
    return pipeline.run(pipeline.Context(root=root, **ctx), ["cost"], reset=True)[0]


@pytest.fixture
def fake_infracost(monkeypatch):
    calls = []

    def fake(plan_json: Path, out: Path) -> dict:
        calls.append(plan_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(fake.answer), encoding="utf-8")
        return fake.answer

    fake.answer = report("12.40", "0", [("module.api.aws_ecs_service.this", "12.40")])
    fake.calls = calls
    monkeypatch.setattr(cost, "run", fake)
    monkeypatch.setenv("INFRACOST_API_KEY", "ico-test")
    return fake


def test_skipped_without_a_deploy_env(tmp_path):
    assert run_cost(write_repo(tmp_path / "r"), deploy_env="").status == "skipped"


def test_skipped_without_a_key_or_budget(tmp_path, monkeypatch):
    monkeypatch.delenv("INFRACOST_API_KEY", raising=False)
    r = run_cost(with_plan(write_repo(tmp_path / "r")))
    assert r.status == "skipped"
    assert "INFRACOST_API_KEY" in r.detail


def test_a_declared_budget_needs_a_key(tmp_path, monkeypatch):
    monkeypatch.delenv("INFRACOST_API_KEY", raising=False)
    r = run_cost(with_plan(write_repo(tmp_path / "r", budget=20)))
    assert r.status == "failed"
    assert "INFRACOST_API_KEY is not set" in r.detail


def test_needs_the_terraform_stages_plan(tmp_path, fake_infracost):
    r = run_cost(write_repo(tmp_path / "r"))
    assert r.status == "failed"
    assert "run the terraform stage first" in r.detail
    assert fake_infracost.calls == []


def test_reports_the_estimate_without_a_budget(tmp_path, fake_infracost):
    root = with_plan(write_repo(tmp_path / "r"))
    r = run_cost(root)
    assert r.status == "passed", r.detail
    assert "$12.40/month (+$12.40 vs. now)" in r.detail
    assert "no budget declared" in r.detail
    assert r.data["estimate"]["monthly"] == 12.4
    assert fake_infracost.calls == [infra.stack_dir(root, "dev") / "policy" / "plan.json"]
    assert (root / ".rlp" / "pipeline" / "reports" / "infracost.json").is_file()


def test_within_budget_passes(tmp_path, fake_infracost):
    r = run_cost(with_plan(write_repo(tmp_path / "r", budget=20)))
    assert r.status == "passed"
    assert "within the monthly budget of $20.00" in r.detail


def test_over_budget_fails_before_deploy(tmp_path, fake_infracost):
    fake_infracost.answer = report("64.00", "12.40")
    root = with_plan(write_repo(tmp_path / "r", budget=50))
    results = pipeline.run(pipeline.Context(root=root, deploy_env="dev"), ["cost", "deploy"], reset=True)
    assert [(r.stage, r.status) for r in results] == [("cost", "failed")]
    assert "over the monthly budget of $50.00" in results[0].detail
    assert results[0].data["budget"] == 50.0


def test_cost_runs_between_terraform_and_deploy():
    names = pipeline.STAGE_NAMES
    assert names.index("terraform") < names.index("cost") < names.index("deploy")


def test_a_plan_infracost_found_nothing_in_fails(tmp_path, fake_infracost):
    fake_infracost.answer = {**report("0"), "summary": {"totalDetectedResources": 0}}
    r = run_cost(with_plan(write_repo(tmp_path / "r", budget=50), changes=3))
    assert r.status == "failed"
    assert "Infracost found none" in r.detail


class FakeProc:
    def __init__(self, code: int, stderr: str):
        self.returncode, self.stderr, self.stdout = code, stderr, ""


def test_run_fails_when_the_pricing_api_is_unreachable(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "ensure_infracost", lambda: tmp_path / "infracost")

    def fake_run(args, **kwargs):
        Path(args[args.index("--out-file") + 1]).write_text(json.dumps(report("0")), encoding="utf-8")
        return FakeProc(0, 'request failed error Post "https://pricing.api.infracost.io/graphql": Forbidden\ngiving up after 5 attempt(s)')

    monkeypatch.setattr(cost.subprocess, "run", fake_run)
    with pytest.raises(cost.CostError, match="pricing API"):
        cost.run(tmp_path / "plan.json", tmp_path / "out" / "infracost.json")


def test_run_returns_the_report(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "ensure_infracost", lambda: tmp_path / "infracost")
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"], seen["env"] = args, kwargs["env"]
        Path(args[args.index("--out-file") + 1]).write_text(json.dumps(report("7")), encoding="utf-8")
        return FakeProc(0, "")

    monkeypatch.setattr(cost.subprocess, "run", fake_run)
    out = cost.run(tmp_path / "plan.json", tmp_path / "out" / "infracost.json")
    assert out["totalMonthlyCost"] == "7"
    assert seen["args"][1:5] == ["breakdown", "--path", str(tmp_path / "plan.json"), "--format"]
    assert seen["env"]["INFRACOST_SKIP_UPDATE_CHECK"] == "true"
