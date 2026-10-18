"""The rlp.agent anti-cheat rules (#50), on agent PR fixtures.

Each test copies the hello fixture into a git repo, commits it as the baseline,
makes one change as a PR would, and runs the policy stage with RLP_BASELINE set.
RLP_PR_LABELS stands in for the pull request's labels: agent:authored marks the
change as an agent's; an empty value makes it a human's.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_pipeline import FIXTURE, commit, env, git, rlp

AGENT = {"RLP_PR_LABELS": "agent:authored", "RLP_PR_AUTHOR": "rdlucas2"}
HUMAN = {"RLP_PR_LABELS": "", "RLP_PR_AUTHOR": "rdlucas2"}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    dest = tmp_path / "hello"
    shutil.copytree(FIXTURE, dest)
    git(dest, "init", "-q")
    commit(dest, "baseline")
    return dest


def head(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True, env=env()).stdout.strip()


def policy_stage(repo: Path, base: str, who: dict) -> tuple[int, dict]:
    proc = rlp(repo, "pipeline", "run", "--reset", "--stage", "policy", "-o", "json", extra_env={"RLP_BASELINE": base, **who})
    report = json.loads(proc.stdout)
    (stage,) = report["stages"]
    return proc.returncode, stage


def rules(stage: dict) -> set[str]:
    return {v["rule"] for v in stage["data"].get("violations", [])}


def add_nosemgrep(repo: Path) -> None:
    src = next((repo / "src").rglob("*.py"))
    src.write_text(src.read_text(encoding="utf-8") + "\nRETRIES = 3  # nosemgrep\n", encoding="utf-8")
    commit(repo, "silence a finding")


def lower_minimum(repo: Path) -> None:
    contract = repo / "rlp.yaml"
    contract.write_text(contract.read_text(encoding="utf-8").replace("minimum: 80", "minimum: 75"), encoding="utf-8")
    commit(repo, "lower the coverage minimum")


def remove_a_test(repo: Path) -> None:
    test_file = next(p for p in (repo / "tests").rglob("test_*.py") if "def test_" in p.read_text(encoding="utf-8"))
    lines = test_file.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("def test_"))
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("def ")), len(lines))
    test_file.write_text("\n".join(lines[:start] + lines[end:]) + "\n", encoding="utf-8")
    commit(repo, "drop a test")


CHANGES = {
    "nosemgrep": (add_nosemgrep, {"RLP-A004"}),
    "coverage-minimum": (lower_minimum, {"RLP-A002"}),
    "removed-test": (remove_a_test, {"RLP-A007"}),
}


@pytest.mark.parametrize("name", CHANGES)
def test_agent_change_fails_the_policy_stage(repo, name):
    make, expected = CHANGES[name]
    base = head(repo)
    make(repo)
    code, stage = policy_stage(repo, base, AGENT)
    assert code == 1, stage
    assert stage["status"] == "failed"
    assert rules(stage) == expected


@pytest.mark.parametrize("name", CHANGES)
def test_same_change_from_a_human_passes(repo, name):
    make, _ = CHANGES[name]
    base = head(repo)
    make(repo)
    code, stage = policy_stage(repo, base, HUMAN)
    assert code == 0, stage
    assert stage["status"] == "passed"


def test_clean_agent_change_passes(repo):
    base = head(repo)
    (repo / "NOTES.md").write_text("A note.\n", encoding="utf-8")
    commit(repo, "add a note")
    code, stage = policy_stage(repo, base, AGENT)
    assert code == 0, stage


def test_uncommitted_and_untracked_changes_count(repo):
    base = head(repo)
    (repo / "src" / "extra.py").write_text("X = 1  # noqa\n", encoding="utf-8")
    code, stage = policy_stage(repo, base, AGENT)
    assert code == 1
    assert rules(stage) == {"RLP-A004"}


def test_labels_come_from_the_github_event(repo, tmp_path):
    base = head(repo)
    add_nosemgrep(repo)
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"user": {"login": "someone"}, "labels": [{"name": "agent:authored"}]}}), encoding="utf-8")
    proc = rlp(repo, "policy", "check", ".", "-o", "json", extra_env={"RLP_BASELINE": base, "GITHUB_EVENT_PATH": str(event)})
    assert proc.returncode == 1, proc.stderr
    assert {v["rule"] for v in json.loads(proc.stdout)["violations"]} == {"RLP-A004"}


def results(tests: int, percent: float) -> list[dict]:
    return [
        {"stage": "test", "status": "passed", "data": {"tests": {"api": tests}}},
        {"stage": "coverage", "status": "passed", "data": {"results": [{"component": "api", "percent": percent}]}},
    ]


def test_test_count_and_coverage_drop_fail(repo, tmp_path):
    base_file, head_file = tmp_path / "base.json", tmp_path / "head.json"
    base_file.write_text(json.dumps(results(5, 92.0)), encoding="utf-8")
    head_file.write_text(json.dumps(results(4, 88.5)), encoding="utf-8")
    args = ["policy", "check", ".", "-o", "json", "--base-results", str(base_file), "--head-results", str(head_file)]
    proc = rlp(repo, *args, extra_env={"RLP_BASELINE": head(repo), **AGENT})
    assert proc.returncode == 1, proc.stderr
    assert {v["rule"] for v in json.loads(proc.stdout)["violations"]} == {"RLP-A005", "RLP-A006"}
    assert rlp(repo, *args, extra_env={"RLP_BASELINE": head(repo), **HUMAN}).returncode == 0


def test_test_stage_records_test_counts(repo):
    proc = rlp(repo, "pipeline", "run", "--reset", "--stage", "setup", "--stage", "test", "-o", "json")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    recorded = json.loads((repo / ".rlp" / "pipeline" / "results.json").read_text(encoding="utf-8"))
    tests = next(r for r in recorded if r["stage"] == "test")["data"]["tests"]
    assert tests["api"] > 0


def test_deleted_test_file_and_gate_file_fail(repo):
    base = head(repo)
    (repo / "tests" / "test_greet.py").unlink()
    (repo / ".coveragerc").unlink()
    commit(repo, "delete the tests and the coverage settings")
    code, stage = policy_stage(repo, base, AGENT)
    assert code == 1
    assert rules(stage) == {"RLP-A001", "RLP-A007"}
    assert any("deletes a protected gate file" in v["message"] for v in stage["data"]["violations"])
