"""The quality loop (#48): rlp loop quality, and the PR a fixed run opens.

The seeded cases under tests/loops/quality/ run end to end: the real lint,
test and coverage stages on a copy of the pipeline fixture repo, a fake
`semgrep` for sast (Semgrep and its rules are large downloads; the
golden-selftest workflow runs the real one), and the fake agent provider with
each case's script. No test calls a real model.
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
import yaml
from jsonschema import Draft202012Validator

from rlp_cli import agent, baseline, escalate, loop, loop_pr, pipeline, quality, resources, sast, tools

REPO = Path(__file__).resolve().parents[2]
HELLO = REPO / "tests" / "pipeline" / "fixtures" / "hello"
CASES = REPO / "tests" / "loops" / "quality"
WORKFLOW = REPO / ".github" / "workflows" / "loop-quality.yml"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "rlp-tests",
    "GIT_AUTHOR_EMAIL": "rlp-tests@example.invalid",
    "GIT_COMMITTER_NAME": "rlp-tests",
    "GIT_COMMITTER_EMAIL": "rlp-tests@example.invalid",
}

FAKE_SEMGREP = '''#!{python}
"""Stands in for semgrep: reports each rule in $FAKE_SEMGREP_RULES whose pattern is still in its file."""
import json, os, sys
args = sys.argv[1:]
config = args[args.index("--config") + 1]
prefix = ".".join(p.strip("/\\\\:") for p in config.replace("\\\\", "/").split("/") if p.strip("/\\\\:")) + "."
results = []
for rule in json.loads(os.environ["FAKE_SEMGREP_RULES"]):
    try:
        text = open(rule["path"], encoding="utf-8").read()
    except OSError:
        continue
    if rule["pattern"] in text:
        line = text[: text.index(rule["pattern"])].count("\\n") + 1
        results.append({{"check_id": prefix + rule["rule"], "path": rule["path"], "start": {{"line": line}},
                        "extra": {{"severity": rule["severity"], "message": rule["message"]}}}})
json.dump({{"results": results, "errors": []}}, open(args[args.index("--json-output") + 1], "w"))
open(args[args.index("--sarif-output") + 1], "w").write('{{"version": "2.1.0", "runs": [{{"tool": {{"driver": {{"name": "Semgrep"}}}}, "results": []}}]}}')
'''


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env).stdout.strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def load_case(name: str) -> dict:
    return yaml.safe_load((CASES / name / "case.yaml").read_text(encoding="utf-8"))


def seed(repo: Path, name: str) -> None:
    """Copy a case's seed/ files into the repo, dropping the .fixture suffix."""
    src = CASES / name / "seed"
    for path in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = path.relative_to(src)
        target = repo / rel.parent / rel.name.removesuffix(".fixture")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


@pytest.fixture
def fake_semgrep(tmp_path: Path, monkeypatch) -> None:
    rules = tmp_path / "semgrep-rules"
    for rel in ["python/lang/security/insecure-hash-algorithm-md5.yaml", "yaml/github-actions/security/run-shell-injection.yaml"]:
        (rules / rel).parent.mkdir(parents=True, exist_ok=True)
        (rules / rel).write_text("rules: []\n", encoding="utf-8")
    monkeypatch.setenv("RLP_SEMGREP_RULES", str(rules))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "fake_semgrep.py"
    script.write_text(FAKE_SEMGREP.format(python=sys.executable), encoding="utf-8")
    if sys.platform == "win32":  # Windows runs .cmd files, not shebang scripts
        exe = bin_dir / "semgrep.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = script
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(sast, "find_semgrep", lambda: (exe, tools.SEMGREP_VERSION))
    monkeypatch.setenv("FAKE_SEMGREP_RULES", "[]")


@pytest.fixture
def fake(monkeypatch) -> agent.FakeProvider:
    provider = agent.FakeProvider()
    monkeypatch.setitem(agent.PROVIDERS, "fake", lambda env: provider)
    return provider


def make_repo(tmp_path: Path, monkeypatch, case: str | None, loops: dict | None = None) -> Path:
    """The pipeline fixture repo with loops enabled, a baseline commit, then the case's seed commit."""
    repo = tmp_path / "hello"
    shutil.copytree(HELLO, repo)
    if loops is not None:
        doc = yaml.safe_load((repo / "rlp.yaml").read_text(encoding="utf-8"))
        doc["loops"] = loops
        (repo / "rlp.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    base = commit(repo, "baseline")
    if case:
        seed(repo, case)
        commit(repo, f"seed {case}")
        monkeypatch.setenv("FAKE_SEMGREP_RULES", json.dumps(load_case(case)["semgrep"]))
    monkeypatch.setenv(baseline.ENV, base)
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("SONAR_TOKEN", raising=False)
    for name in ("user.name", "user.email"):
        git(repo, "config", name, GIT_ENV["GIT_AUTHOR_NAME"] if name == "user.name" else GIT_ENV["GIT_AUTHOR_EMAIL"])
    return repo


def run_gates(repo: Path) -> dict[str, str]:
    """What the loop workflow does first: setup, then each verify stage on its own so every failure is recorded."""
    ctx = pipeline.Context(root=repo, stream=subprocess.DEVNULL)
    [setup] = pipeline.run(ctx, ["setup"], reset=True)
    assert setup.status == "passed", setup.detail
    statuses = {}
    for stage in quality.VERIFY_STAGES:
        [r] = pipeline.run(ctx, [stage])
        statuses[stage] = r.status
    return statuses


def run_loop(repo: Path, **kw) -> loop.Report:
    env = kw.pop("env", {})
    return loop.run(repo, "quality", loop.Options(provider="fake", env=env, **kw), stream=subprocess.DEVNULL)


def validate_state(path: Path) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    errors = [f"{list(e.absolute_path)}: {e.message}" for e in Draft202012Validator(resources.schema("LoopState")).iter_errors(doc)]
    assert errors == []
    return doc


def script(case: str) -> list[agent.FakeStep]:
    raw = json.loads((CASES / case / "agent.json").read_text(encoding="utf-8"))
    return [agent.FakeStep(files=s["files"], summary=s["summary"], cost_usd=s["costUsd"]) for s in raw]


# --- the seeded cases, end to end ---


@pytest.mark.parametrize("case", ["semgrep", "lint", "coverage"])
def test_seeded_failure_is_fixed_and_the_pr_is_green(tmp_path, monkeypatch, fake_semgrep, fake, case):
    expect = load_case(case)["expect"]
    repo = make_repo(tmp_path, monkeypatch, case, loops={"enabled": True})
    statuses = run_gates(repo)
    first_red = next(s for s in quality.VERIFY_STAGES if statuses[s] == "failed")
    assert first_red == expect["first-failing-stage"], statuses
    planned = script(case)
    fake.steps = list(planned)
    report = run_loop(repo, max_iterations=3, max_spend_usd=5)
    assert report.outcome == expect["outcome"] == "fixed", report.reason
    state = validate_state(repo / report.state_file)
    assert len(state["iterations"]) == expect["iterations"]
    assert all(f["stage"] == first_red for f in state["findings"] if f["stage"] in ("lint", "sast"))
    assert [g["status"] for g in state["iterations"][-1]["gates"]] == ["passed"] * 4
    # the fix lives on the loop branch only, committed as the agent's change
    branch = state["branch"]
    assert git(repo, "symbolic-ref", "--short", "HEAD") == "main"
    changed = git(repo, "diff", "--name-only", f"main..{branch}").splitlines()
    assert changed == sorted(planned[0].files)
    # the PR renders from the state file: green, so it can open
    fixed = loop_pr.load_fixed(repo / report.state_file)
    body = loop_pr.render(fixed, "https://example.invalid/run/1")
    assert loop_pr.labels(fixed) == ["agent:authored", "loop:quality"]
    assert "| lint | passed |" in body and "| sast | passed |" in body
    assert f"`{branch}`" in body and "https://example.invalid/run/1" in body


def test_semgrep_finding_reaches_the_prompt_with_file_line_and_rule(tmp_path, monkeypatch, fake_semgrep, fake):
    repo = make_repo(tmp_path, monkeypatch, "semgrep", loops={"enabled": True})
    run_gates(repo)
    [finding] = quality.discover(repo, yaml.safe_load((repo / "rlp.yaml").read_text(encoding="utf-8")))
    assert finding["path"] == "src/hello/digest.py" and finding["line"] == 7
    assert finding["rule"] == "python.lang.security.insecure-hash-algorithm-md5.insecure-hash-algorithm-md5"
    fake.steps = script("semgrep")
    run_loop(repo)
    prompt = fake.prompts[0]
    assert "src/hello/digest.py:7: Detected MD5" in prompt and "[python.lang.security" in prompt
    assert "Semgrep (no new ERROR or WARNING findings)" in prompt


def test_lint_output_reaches_the_prompt(tmp_path, monkeypatch, fake_semgrep, fake):
    repo = make_repo(tmp_path, monkeypatch, "lint", loops={"enabled": True})
    run_gates(repo)
    fake.steps = script("lint")
    run_loop(repo)
    prompt = fake.prompts[0]
    assert "the lint verb fails for component api (exit " in prompt
    assert "shout.py" in prompt and "SyntaxError" in prompt


def test_uncovered_changed_lines_reach_the_prompt(tmp_path, monkeypatch, fake_semgrep, fake):
    repo = make_repo(tmp_path, monkeypatch, "coverage", loops={"enabled": True})
    run_gates(repo)
    findings = quality.discover(repo, yaml.safe_load((repo / "rlp.yaml").read_text(encoding="utf-8")))
    new = [f for f in findings if f["rule"] == "new-code-coverage"]
    assert [f["path"] for f in new] == ["src/hello/farewell.py"]
    assert new[0]["lines"] and "changed lines not covered by any test" in new[0]["message"]
    fake.steps = script("coverage")
    run_loop(repo)
    assert "src/hello/farewell.py:" in fake.prompts[0] and "Coverage (overall" in fake.prompts[0]


def test_unfixable_case_escalates_within_the_cap_and_opens_no_pr(tmp_path, monkeypatch, fake_semgrep, fake):
    expect = load_case("unfixable")["expect"]
    repo = make_repo(tmp_path, monkeypatch, "unfixable", loops={"enabled": True})
    run_gates(repo)
    fake.steps = script("unfixable")
    report = run_loop(repo, max_iterations=3, max_spend_usd=5)
    assert report.outcome == expect["outcome"] == "escalated" and report.exit_code == 1
    state = validate_state(repo / report.state_file)
    assert len(state["iterations"]) == expect["iterations"] == len(fake.prompts)
    assert all(".github/workflows/triage.yml" in it["rejected"] and it["commit"] == "" for it in state["iterations"])
    assert git(repo, "rev-list", "--count", f"main..{state['branch']}") == "0"
    with pytest.raises(loop_pr.PullRequestError, match="only a fixed run"):
        loop_pr.load_fixed(repo / report.state_file)
    assert escalate.load_state(repo / report.state_file)["outcome"] == "escalated"


@pytest.mark.parametrize(("loops", "env"), [(None, {}), ({"enabled": True}, {"RLP_LOOPS": "off"}), ({"enabled": True, "kinds": ["vuln"]}, {})])
def test_a_failing_gate_starts_no_run_when_loops_are_off(tmp_path, monkeypatch, fake_semgrep, fake, loops, env):
    repo = make_repo(tmp_path, monkeypatch, "lint", loops=loops)
    run_gates(repo)
    fake.steps = script("lint")
    report = run_loop(repo, env=env)
    assert report.outcome == "disabled" and report.exit_code == 0
    assert fake.prompts == []
    assert not (repo / ".rlp" / "loops").exists()
    assert git(repo, "branch", "--list", "rlp/loop/*") == ""


def test_green_gates_have_no_findings(tmp_path, monkeypatch, fake_semgrep, fake):
    repo = make_repo(tmp_path, monkeypatch, None, loops={"enabled": True})
    assert set(run_gates(repo).values()) == {"passed"}
    report = run_loop(repo)
    assert report.outcome == "no-findings" and fake.prompts == []


# --- allowed paths ---


def tracked_repo(tmp_path: Path, files: list[str], components: list[dict]) -> tuple[Path, dict]:
    repo = tmp_path / "r"
    for rel in files:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("x\n", encoding="utf-8")
    git(repo, "init", "-q")
    commit(repo, "files")
    return repo, {"components": components}


def test_allowed_paths_are_code_directories_not_config(tmp_path):
    files = ["rlp.yaml", "Taskfile.yml", "pyproject.toml", ".coveragerc", "src/app/main.py", "tests/test_main.py",
             ".github/workflows/ci.yml", ".rlp/vex.yaml", "policy/x.rego", "infra/main.tf", "web/src/index.ts", "web/package.json"]
    repo, doc = tracked_repo(tmp_path, files, [{"name": "api"}, {"name": "web", "path": "web"}])
    assert quality.allowed_paths(repo, doc) == ["src", "tests", "web/src"]


def test_a_component_without_code_directories_is_an_error(tmp_path):
    repo, doc = tracked_repo(tmp_path, ["rlp.yaml", "main.go"], [{"name": "api"}])
    with pytest.raises(loop.LoopError, match="no tracked source directories"):
        quality.allowed_paths(repo, doc)


# --- discovery details ---


def write_results(repo: Path, results: list[dict]) -> None:
    path = repo / pipeline.RESULTS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results), encoding="utf-8")


def test_overall_coverage_below_minimum_is_a_finding(tmp_path):
    write_results(tmp_path, [{"stage": "coverage", "status": "failed", "detail": "", "data": {"results": [
        {"component": "api", "path": "reports/coverage.xml", "percent": 61.25, "minimum": 80, "ok": False, "error": None}], "new_code": []}}])
    [f] = quality.discover(tmp_path, {"components": [{"name": "api"}]}, env={})
    assert f["message"] == "line coverage of component api is 61.2%, below the minimum of 80%"


def test_sast_findings_fall_back_to_the_semgrep_report(tmp_path):
    write_results(tmp_path, [{"stage": "sast", "status": "failed", "detail": "1 new finding(s)", "data": {}}])
    report = tmp_path / pipeline.REPORTS / "semgrep.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"results": [{"check_id": "r.x", "path": "src/a.py", "start": {"line": 3}, "extra": {"severity": "ERROR", "message": "bad\nmore"}}]}))
    [f] = quality.discover(tmp_path, {"components": []}, env={})
    assert (f["path"], f["line"], f["rule"], f["message"]) == ("src/a.py", 3, "r.x", "bad")


def test_a_failed_stage_without_details_still_reports_its_detail(tmp_path):
    write_results(tmp_path, [{"stage": "sast", "status": "failed", "detail": "semgrep is not installed", "data": {}}])
    [f] = quality.discover(tmp_path, {"components": []}, env={})
    assert f["message"] == "the sast stage failed: semgrep is not installed"


def test_passed_stages_and_stages_outside_the_loop_are_ignored(tmp_path):
    write_results(tmp_path, [{"stage": "lint", "status": "passed", "detail": ""}, {"stage": "trivy", "status": "failed", "detail": "CVE"}])
    assert quality.discover(tmp_path, {"components": []}, env={}) == []


SONAR_DOC = {"metadata": {"name": "hello", "project": "hello"}, "components": [{"name": "api"}]}


def test_sonar_issues_are_read_when_the_sonar_stage_failed_with_a_token(tmp_path):
    write_results(tmp_path, [{"stage": "sonar", "status": "failed", "detail": "quality gate failed"}])
    calls = []

    def fetch(url, headers):
        calls.append((url, headers))
        return {"issues": [{"key": "AX1", "component": "hello:src/a.py", "line": 9, "rule": "python:S1481", "severity": "MINOR", "message": "Remove the unused local variable."}]}

    env = {"SONAR_TOKEN": "t0k", "SONAR_HOST_URL": "http://sonar.local:9000"}
    findings = quality.sonar_findings(tmp_path, SONAR_DOC, env, fetch)
    assert findings == [{"id": "sonar-AX1", "stage": "sonar", "path": "src/a.py", "rule": "python:S1481", "severity": "MINOR", "message": "Remove the unused local variable.", "line": 9}]
    url, headers = calls[0]
    assert url.startswith("http://sonar.local:9000/api/issues/search?") and "componentKeys=hello" in url and "resolved=false" in url
    assert headers["Authorization"] == "Bearer t0k"
    # without SONAR_TOKEN the sonar stage never ran for real, so discovery leaves it alone
    assert quality.discover(tmp_path, SONAR_DOC, env={}) == []


def test_an_unreachable_sonar_is_one_finding(tmp_path):
    def fetch(url, headers):
        raise OSError("connection refused")

    [f] = quality.sonar_findings(tmp_path, SONAR_DOC, {"SONAR_TOKEN": "t"}, fetch)
    assert "could not be read" in f["message"] and "connection refused" in f["message"]


# --- the prompt ---


def test_prompt_groups_findings_by_gate_and_keeps_the_rules():
    p = loop.PromptInput("quality", 2, 3, [
        {"stage": "lint", "message": "the lint verb fails for component api (exit 1)", "output": "E1 ```bad```"},
        {"stage": "sast", "path": "src/a.py", "line": 4, "rule": "r.x", "message": "bad"},
    ], [{"stage": "lint", "status": "failed", "detail": "api exited 1"}], ["src", "tests"], rejected="it changed files outside the allowed paths: rlp.yaml")
    text = quality.build_prompt(p)
    assert "attempt 2 of 3" in text and "Change only files under: src, tests" in text
    assert "no suppression comments" in text and "You can't run commands" in text
    assert text.index("## Lint") < text.index("## Semgrep")
    assert "````text" in text and "E1 ```bad```" in text  # the fence outgrows the output's own backticks
    assert "- src/a.py:4: bad [r.x]" in text
    assert "thrown away: it changed files outside the allowed paths: rlp.yaml" in text
    assert "- lint: api exited 1" in text


# --- the pull request ---


def fixed_state(**over) -> dict:
    gates = [{"stage": s, "status": "passed", "detail": "ok"} for s in quality.VERIFY_STAGES]
    state = {
        "apiVersion": "rlp/v1alpha1", "kind": "LoopState", "loop": "quality", "runId": "20261019T000000Z-abc123",
        "branch": "rlp/loop/quality/20261019T000000Z-abc123", "base": "main", "provider": "fake",
        "limits": {"maxIterations": 3, "maxSpendUsd": 5.0}, "verifyStages": list(quality.VERIFY_STAGES), "allowedPaths": ["src", "tests"],
        "startedAt": "2026-10-19T00:00:00Z", "finishedAt": "2026-10-19T00:05:00Z",
        "findings": [{"id": "sast-x", "stage": "sast", "path": "src/a.py", "line": 4, "rule": "r.x", "message": "bad"}],
        "iterations": [{
            "number": 1, "findings": [], "rejected": "", "commit": "0123456789abcdef", "spentUsd": 0.1,
            "agent": {"provider": "fake", "ok": True, "summary": "fixed it", "changedFiles": ["src/a.py"], "outsideAllowed": [], "costUsd": 0.1,
                      "tokens": {"input": 10, "output": 5}, "overBudget": False},
            "diffStat": {"files": 1, "insertions": 1, "deletions": 1}, "gates": gates,
        }],
        "spend": {"spentUsd": 0.1, "calls": 1}, "outcome": "fixed", "reason": "passed",
    }
    state.update(over)
    return state


def write_state(tmp_path: Path, state: dict) -> Path:
    errors = [e.message for e in Draft202012Validator(resources.schema("LoopState")).iter_errors(state)]
    assert errors == []
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def test_no_pr_while_a_verify_stage_is_red(tmp_path):
    state = fixed_state()
    state["iterations"][0]["gates"][3]["status"] = "failed"
    with pytest.raises(loop_pr.PullRequestError, match="not all green on the last commit: sast"):
        loop_pr.load_fixed(write_state(tmp_path, state))


def test_no_pr_when_a_verify_stage_did_not_run(tmp_path):
    state = fixed_state()
    state["iterations"][0]["gates"] = state["iterations"][0]["gates"][:2]
    with pytest.raises(loop_pr.PullRequestError, match=r"coverage \(not run\)"):
        loop_pr.load_fixed(write_state(tmp_path, state))


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith("/pulls"):
            return {"number": 7, "html_url": "https://github.com/o/r/pull/7"}
        return []


def test_post_opens_the_pr_into_the_base_and_labels_it(tmp_path):
    state = loop_pr.load_fixed(write_state(tmp_path, fixed_state()))
    client = FakeClient()
    posted = loop_pr.post(client, "o/r", state, "https://run")
    assert posted.as_dict() == {"action": "opened", "number": 7, "url": "https://github.com/o/r/pull/7"}
    (m1, p1, b1), (m2, p2, b2) = client.calls
    assert (m1, p1) == ("POST", "/repos/o/r/pulls")
    assert b1["head"] == state["branch"] and b1["base"] == "main"
    assert b1["title"] == "fix(quality): rlp loop fixes 1 finding on main"
    assert "`src/a.py:4`: bad [r.x]" in b1["body"]
    assert (m2, p2, b2) == ("POST", "/repos/o/r/issues/7/labels", {"labels": ["agent:authored", "loop:quality"]})


def test_cli_renders_the_pr_and_refuses_an_escalated_run(tmp_path, rlp):
    path = write_state(tmp_path, fixed_state())
    shown = rlp("loop", "pr", str(path), "--trigger", "https://run", "--output", "json", env={"GITHUB_RUN_ID": ""})
    assert shown.code == 0, shown.stderr
    assert shown.json["head"] == "rlp/loop/quality/20261019T000000Z-abc123" and shown.json["labels"] == ["agent:authored", "loop:quality"]
    escalated = write_state(tmp_path, fixed_state(outcome="escalated"))
    refused = rlp("loop", "pr", str(escalated))
    assert refused.code == 2 and "only a fixed run" in refused.stderr


def test_quality_is_registered_with_its_verify_stages():
    kind = loop.KINDS["quality"]
    assert kind.verify_stages == ("lint", "test", "coverage", "sast")
    assert kind.build_prompt is quality.build_prompt


# --- the workflow ---


def test_workflow_respects_the_toggle_and_opens_a_pr_only_when_fixed():
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert "workflow_call" in wf[True]
    [job] = wf["jobs"].values()
    assert "vars.RLP_LOOPS" in job["if"]
    assert job["env"]["RLP_LOOPS"] == "${{ vars.RLP_LOOPS }}"
    steps = {s.get("id") or s.get("name"): s for s in job["steps"]}
    assert "rlp loop status" in steps["toggle"]["run"]
    after = job["steps"][job["steps"].index(steps["toggle"]) + 1:]
    assert after and all("steps.toggle.outputs.enabled == 'true'" in s.get("if", "") for s in after if s.get("name") != "Upload loop state")
    pr = steps["pr"]
    assert "steps.loop.outputs.outcome == 'fixed'" in pr["if"] and "rlp loop pr" in pr["run"]
    assert "rlp loop escalate" in steps["escalate"]["run"]
    for s in job["steps"]:
        uses = s.get("uses", "")
        if uses:
            assert "@" in uses and len(uses.split("@")[1].split()[0]) == 40, f"{uses} is not pinned to a commit"
