"""New code: the baseline commit, the sast stage (Opengrep) and coverage of changed lines.

Opengrep and its rules are large downloads, so the sast tests use a fake
`opengrep` that replays a report and a small fake rule tree; the
golden-selftest workflow runs the real one. diff-cover is a Python dependency
of rlp, so the new-code coverage tests run it for real on fixture repos.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from rlp_cli import baseline, pipeline, sast, tools

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "hello"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "rlp-tests",
    "GIT_AUTHOR_EMAIL": "rlp-tests@example.invalid",
    "GIT_COMMITTER_NAME": "rlp-tests",
    "GIT_COMMITTER_EMAIL": "rlp-tests@example.invalid",
}

FAKE_OPENGREP = '''#!{python}
"""Stands in for opengrep: logs its arguments, writes $FAKE_OPENGREP_RESULTS as the JSON report."""
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_OPENGREP_LOG"], "w") as log:
    json.dump(args, log)
config = args[args.index("--config") + 1]
prefix = ".".join(p.strip("/\\\\:") for p in config.replace("\\\\", "/").split("/") if p.strip("/\\\\:")) + "."
results = json.loads(os.environ["FAKE_OPENGREP_RESULTS"])
for r in results:
    r["check_id"] = prefix + r["check_id"]
json.dump({{"results": results, "errors": []}}, open(args[args.index("--json-output") + 1], "w"))
open(args[args.index("--sarif-output") + 1], "w").write('{{"version": "2.1.0", "runs": [{{"tool": {{"driver": {{"name": "Opengrep"}}}}, "results": []}}]}}')
'''

MD5_FINDING = {
    "check_id": "python.lang.security.insecure-hash-algorithm-md5",
    "path": "src/hello/digest.py",
    "start": {"line": 5},
    "extra": {"severity": "WARNING", "message": "Detected MD5 hash algorithm which is considered insecure.\nUse SHA256."},
}


def git_env() -> dict:
    env = {**os.environ, **GIT_ENV}
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    return env


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=git_env()).stdout.strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    dest = tmp_path / "hello"
    shutil.copytree(FIXTURE, dest)
    git(dest, "init", "-q")
    commit(dest, "fixture")
    monkeypatch.delenv(baseline.ENV, raising=False)
    return dest


def run_stage(repo: Path, *stages: str) -> list[pipeline.StageResult]:
    return pipeline.run(pipeline.Context(root=repo, stream=subprocess.DEVNULL), list(stages), reset=True)


# --- baseline ---


def test_baseline_is_the_merge_base_with_rlp_baseline(repo, monkeypatch):
    base = git(repo, "rev-parse", "HEAD")
    (repo / "new.txt").write_text("x\n", encoding="utf-8")
    commit(repo, "more")
    monkeypatch.setenv(baseline.ENV, base)
    assert baseline.resolve(repo) == base


@pytest.mark.parametrize("value", ["", "0" * 40])
def test_empty_or_zero_baseline_means_none(repo, monkeypatch, value):
    monkeypatch.setenv(baseline.ENV, value)
    assert baseline.resolve(repo) is None


def test_unknown_baseline_is_an_error(repo, monkeypatch):
    monkeypatch.setenv(baseline.ENV, "1234567890abcdef1234567890abcdef12345678")
    with pytest.raises(baseline.BaselineError, match="fetch-depth: 0"):
        baseline.resolve(repo)


def test_default_baseline_is_origin_default_branch(repo, tmp_path):
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True, env=git_env())
    base = git(clone, "rev-parse", "HEAD")
    (clone / "new.txt").write_text("x\n", encoding="utf-8")
    commit(clone, "feature work")
    assert baseline.resolve(clone) == base


def test_no_git_no_baseline(tmp_path):
    assert baseline.resolve(tmp_path) is None


# --- sast ---


@pytest.fixture
def fake_rules(tmp_path: Path, monkeypatch) -> Path:
    rules = tmp_path / "opengrep-rules"
    for rel in [
        "python/lang/security/insecure-hash-algorithm-md5.yaml",
        "python/lang/security/audit/dangerous-subprocess-use-audit.yaml",
        "python/lang/security/insecure-hash-algorithm-md5.test.yaml",
        "javascript/browser/security/eval-detected.yaml",
        "typescript/react/security/dangerous-html.yaml",
        "dockerfile/security/missing-user.yaml",
        "yaml/github-actions/security/run-shell-injection.yaml",
        "go/lang/security/bad-tmp.yaml",
    ]:
        path = rules / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("rules: []\n", encoding="utf-8")
    monkeypatch.setenv("RLP_OPENGREP_RULES", str(rules))
    return rules


@pytest.fixture
def fake_opengrep(tmp_path: Path, monkeypatch, fake_rules) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "fake_opengrep.py"
    script.write_text(FAKE_OPENGREP.format(python=sys.executable), encoding="utf-8")
    if sys.platform == "win32":  # Windows runs .cmd files, not shebang scripts
        exe = bin_dir / "opengrep.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = script
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "opengrep-args.json"
    monkeypatch.setattr(tools, "ensure_opengrep", lambda dest=None: exe)
    monkeypatch.setenv("FAKE_OPENGREP_LOG", str(log))
    monkeypatch.setenv("FAKE_OPENGREP_RESULTS", "[]")
    return {"log": log}


def test_rule_dirs_follow_component_languages(fake_rules):
    doc = {"components": [{"name": "api", "language": "python"}, {"name": "web", "language": "typescript"}, {"name": "x", "language": "cobol"}]}
    assert sast.rule_dirs(fake_rules, doc) == ["python", "typescript", "javascript", "dockerfile", "yaml/github-actions"]
    assert sast.rule_dirs(fake_rules, doc, only=("api",)) == ["python", "dockerfile", "yaml/github-actions"]


def test_select_rules_leaves_out_audit_rules_and_rule_tests(fake_rules, tmp_path):
    dest = tmp_path / "selected"
    assert sast.select_rules(fake_rules, ["python"], dest) == 1
    assert [p.relative_to(dest).as_posix() for p in dest.rglob("*.yaml")] == ["python/lang/security/insecure-hash-algorithm-md5.yaml"]


def test_sast_passes_without_findings_and_writes_sarif(repo, fake_opengrep):
    [result] = run_stage(repo, "sast")
    assert result.status == "passed", result.detail
    assert "whole repo (no baseline commit)" in result.detail
    args = json.loads(fake_opengrep["log"].read_text(encoding="utf-8"))
    assert "--baseline-commit" not in args
    assert args[args.index("--severity") + 1] == "ERROR" and "WARNING" in args
    doc = json.loads((repo / result.data["sarif"]).read_text(encoding="utf-8"))
    assert doc["runs"][0]["automationDetails"]["id"] == "rlp/opengrep/"


def test_sast_new_finding_fails_with_a_readable_line(repo, fake_opengrep, monkeypatch):
    base = git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv(baseline.ENV, base)
    monkeypatch.setenv("FAKE_OPENGREP_RESULTS", json.dumps([MD5_FINDING]))
    [result] = run_stage(repo, "sast")
    assert result.status == "failed"
    assert f"1 new finding(s) since {base[:12]}" in result.detail
    assert "src/hello/digest.py:5 python.lang.security.insecure-hash-algorithm-md5 (WARNING)" in result.detail
    assert result.data["findings"][0]["message"] == "Detected MD5 hash algorithm which is considered insecure."
    args = json.loads(fake_opengrep["log"].read_text(encoding="utf-8"))
    assert args[args.index("--baseline-commit") + 1] == base


def test_sast_without_rules_for_the_repo_fails(repo, fake_opengrep, tmp_path, monkeypatch):
    empty = tmp_path / "no-rules"
    empty.mkdir()
    monkeypatch.setenv("RLP_OPENGREP_RULES", str(empty))
    [result] = run_stage(repo, "sast")
    assert result.status == "failed"
    assert "no Opengrep rules" in result.detail


# --- coverage on new code (real diff-cover) ---

BIG = "\n\n".join(f"def f{i}(x):\n    y = x + {i}\n    return y * 2" for i in range(12)) + "\n"
BIG_TEST = "from hello.big import *  # noqa: F403\n\n\ndef test_big():\n" + "".join(f"    assert f{i}(1) == {2 * (1 + i)}\n" for i in range(12))
PARTLY = "\n\ndef partly(x):\n    if x:\n        return 1\n    return 2\n"
PARTLY_TEST = "\n\ndef test_partly():\n    from hello.big import partly\n\n    assert partly(True) == 1\n"


def set_minimum(repo: Path, value: int) -> None:
    path = repo / "rlp.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("minimum: 80", f"minimum: {value}"), encoding="utf-8")


def seed_partly_covered_change(repo: Path) -> str:
    """Commit a well-covered module (the baseline), then a change whose new lines are 75% covered."""
    (repo / "src" / "hello" / "big.py").write_text(BIG, encoding="utf-8")
    (repo / "tests" / "test_big.py").write_text(BIG_TEST, encoding="utf-8")
    base = commit(repo, "well-covered module")
    with (repo / "src" / "hello" / "big.py").open("a", encoding="utf-8") as f:
        f.write(PARTLY)
    with (repo / "tests" / "test_big.py").open("a", encoding="utf-8") as f:
        f.write(PARTLY_TEST)
    commit(repo, "partly covered change")
    return base


def run_test_and_coverage(repo: Path, monkeypatch) -> pipeline.StageResult:
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""))
    results = run_stage(repo, "test", "coverage")
    assert results[0].status == "passed", results[0].detail
    return results[1]


def test_new_code_below_minimum_fails_while_overall_passes(repo, monkeypatch):
    base = seed_partly_covered_change(repo)
    monkeypatch.setenv(baseline.ENV, base)
    result = run_test_and_coverage(repo, monkeypatch)
    assert result.status == "failed", result.detail
    [overall] = result.data["results"]
    assert overall["ok"] and overall["percent"] >= 80
    [fresh] = result.data["new_code"]
    assert fresh["percent"] == 75.0 and fresh["lines"] == 4 and not fresh["ok"]
    assert "new code: api 75.0% of 4 changed line(s) (minimum 80%)" in result.detail
    uncovered = (repo / "src" / "hello" / "big.py").read_text(encoding="utf-8").splitlines().index("    return 2") + 1
    assert list(fresh["missing"].values()) == [[uncovered]]


def test_new_code_above_a_lower_minimum_passes(repo, monkeypatch):
    set_minimum(repo, 70)
    commit(repo, "minimum 70")
    base = seed_partly_covered_change(repo)
    monkeypatch.setenv(baseline.ENV, base)
    result = run_test_and_coverage(repo, monkeypatch)
    assert result.status == "passed", result.detail
    assert result.data["new_code"][0]["percent"] == 75.0


def test_new_code_is_skipped_without_a_baseline(repo, monkeypatch):
    result = run_test_and_coverage(repo, monkeypatch)
    assert result.status == "passed", result.detail
    assert "new code: api skipped (no baseline commit)" in result.detail
