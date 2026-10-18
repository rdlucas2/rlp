"""The remediation loop runner (#46): rlp loop <kind>.

Every test uses the fake agent provider; none calls a real model. The loop
kind under test is registered here: it finds a finding while src/app.py lacks
the word "fixed", and verifies with the real `lint` pipeline stage, whose verb
override fails until the word is there.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from rlp_cli import agent, loop, resources

GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@example.com"]
LINT = f'"{Path(sys.executable).as_posix()}" -c "import sys; sys.exit(0 if \'fixed\' in open(\'src/app.py\').read() else 1)"'


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def make_repo(root: Path, loops: dict | None = None, app: str = "broken\n") -> Path:
    doc = {
        "apiVersion": "rlp/v1alpha1",
        "kind": "Repo",
        "metadata": {"name": "looped", "project": "looped"},
        "components": [{
            "name": "app",
            "language": "python",
            "artifact": {"kind": "container-image"},
            "runtime": {"kind": "container-service", "port": 8000},
            "verbs": {"lint": LINT},
        }],
    }
    if loops is not None:
        doc["loops"] = loops
    (root / "src").mkdir(parents=True)
    (root / "policy").mkdir()
    (root / "rlp.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    (root / "src" / "app.py").write_text(app, encoding="utf-8")
    (root / "policy" / "gate.rego").write_text("package gate\n", encoding="utf-8")
    (root / ".gitignore").write_text(".rlp/\n", encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, *GIT_ID, "commit", "-q", "-m", "init")
    return root


def discover(root: Path, doc: dict) -> list[dict]:
    text = (root / "src" / "app.py").read_text(encoding="utf-8")
    if "fixed" in text:
        return []
    return [{"id": "app-broken", "message": f"src/app.py is broken: {text.strip()}", "path": "src/app.py", "line": 1}]


@pytest.fixture
def kind(monkeypatch):
    k = loop.LoopKind(
        name="quality",
        description="Test kind.",
        discover=discover,
        verify_stages=("lint",),
        allowed_paths=lambda root, doc: ["src"],
    )
    monkeypatch.setitem(loop.KINDS, "quality", k)
    return k


@pytest.fixture
def fake(monkeypatch):
    """Install a fake provider whose script the test sets; it records every prompt."""
    provider = agent.FakeProvider()
    monkeypatch.setitem(agent.PROVIDERS, "fake", lambda env: provider)
    return provider


def steps(*contents: str, cost: float = 0.1) -> list[agent.FakeStep]:
    return [agent.FakeStep(files={"src/app.py": c}, cost_usd=cost, summary=f"wrote {c.strip()}") for c in contents]


ENABLED = {"enabled": True}
ENV = {"PATH": ""}  # no RLP_LOOPS


def run(repo: Path, **kw) -> loop.Report:
    env = kw.pop("env", ENV)
    return loop.run(repo, "quality", loop.Options(provider="fake", env=env, **kw))


def validate_state(path: Path) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    errors = [f"{list(e.absolute_path)}: {e.message}" for e in Draft202012Validator(resources.schema("LoopState")).iter_errors(doc)]
    assert errors == []
    return doc


# --- the cycle ---


def test_fixed_on_iteration_two_on_its_own_branch(tmp_path, kind, fake):
    repo = make_repo(tmp_path / "repo", ENABLED)
    main_head = git(repo, "rev-parse", "main")
    fake.steps = steps("still broken\n", "fixed\n")
    report = run(repo, max_iterations=3, max_spend_usd=5)
    assert report.outcome == "fixed" and report.exit_code == 0
    state = validate_state(repo / report.state_file)
    assert [i["number"] for i in state["iterations"]] == [1, 2]
    assert [i["gates"][0]["status"] for i in state["iterations"]] == ["failed", "passed"]
    assert state["iterations"][1]["diffStat"] == {"files": 1, "insertions": 1, "deletions": 1}
    assert state["spend"] == {"spentUsd": pytest.approx(0.2), "calls": 2}
    branch = state["branch"]
    assert branch.startswith("rlp/loop/quality/") and state["base"] == "main"
    # the base branch is untouched and checked out again; the fix lives on the loop branch
    assert git(repo, "rev-parse", "main") == main_head
    assert git(repo, "symbolic-ref", "--short", "HEAD") == "main"
    assert (repo / "src" / "app.py").read_text() == "broken\n"
    assert git(repo, "show", f"{branch}:src/app.py") == "fixed"
    assert git(repo, "rev-list", "--count", f"main..{branch}") == "2"
    assert state["iterations"][1]["commit"] == git(repo, "rev-parse", branch)


def test_never_fixed_stops_at_the_iteration_cap(tmp_path, kind, fake):
    repo = make_repo(tmp_path / "repo", ENABLED)
    fake.steps = steps(*[f"broken {n}\n" for n in range(5)])
    report = run(repo, max_iterations=2, max_spend_usd=5)
    assert report.outcome == "escalated" and report.exit_code == 1
    assert "iteration cap of 2" in report.reason
    assert len(fake.prompts) == 2
    assert len(validate_state(repo / report.state_file)["iterations"]) == 2


def test_a_spend_cap_below_one_iteration_stops_before_a_second(tmp_path, kind, fake):
    repo = make_repo(tmp_path / "repo", ENABLED)
    fake.steps = steps("broken a\n", "broken b\n", cost=1.0)
    report = run(repo, max_iterations=3, max_spend_usd=0.5)
    assert report.outcome == "escalated" and "spend cap of $0.50" in report.reason
    assert len(fake.prompts) == 1
    state = validate_state(repo / report.state_file)
    assert len(state["iterations"]) == 1 and state["iterations"][0]["agent"]["overBudget"] is True


def test_no_findings_calls_no_agent_and_makes_no_branch(tmp_path, kind, fake):
    repo = make_repo(tmp_path / "repo", ENABLED, app="fixed\n")
    report = run(repo)
    assert report.outcome == "no-findings" and report.exit_code == 0
    assert fake.prompts == []
    assert validate_state(repo / report.state_file)["branch"] == ""
    assert git(repo, "branch", "--list", "rlp/loop/*") == ""


def test_each_iteration_gets_a_fresh_prompt_from_current_gate_output(tmp_path, kind, fake):
    repo = make_repo(tmp_path / "repo", ENABLED)
    fake.steps = steps("half done\n", "fixed\n")
    run(repo)
    first, second = fake.prompts
    assert "attempt 1 of 3" in first and "src/app.py is broken: broken" in first
    assert "still fails" not in first
    assert "attempt 2 of 3" in second and "src/app.py is broken: half done" in second
    assert "- lint: " in second and "exited 1" in second


def test_changes_outside_allowed_paths_are_thrown_away(tmp_path, kind, fake):
    repo = make_repo(tmp_path / "repo", ENABLED)
    fake.steps = [
        agent.FakeStep(files={"src/app.py": "fixed\n", "policy/gate.rego": "package gate\n# weakened\n", "new.txt": "x"}),
        *steps("fixed\n"),
    ]
    report = run(repo)
    assert report.outcome == "fixed"
    state = validate_state(repo / report.state_file)
    first, second = state["iterations"]
    assert "policy/gate.rego" in first["rejected"] and first["gates"] == [] and first["commit"] == ""
    assert "thrown away" in fake.prompts[1] and "policy/gate.rego" in fake.prompts[1]
    branch = state["branch"]
    assert git(repo, "show", f"{branch}:policy/gate.rego") == "package gate"
    assert git(repo, "diff", "--name-only", f"main..{branch}") == "src/app.py"
    assert second["gates"][0]["status"] == "passed"


def test_dry_run_plans_without_calling_the_agent(tmp_path, kind, fake):
    repo = make_repo(tmp_path / "repo", ENABLED)
    report = run(repo, dry_run=True)
    assert report.outcome == "dry-run" and report.exit_code == 0
    assert report.payload["findings"][0]["id"] == "app-broken"
    assert report.payload["verifyStages"] == ["lint"] and report.payload["allowedPaths"] == ["src"]
    assert "attempt 1 of 3" in report.payload["firstPrompt"]
    assert fake.prompts == []
    assert git(repo, "branch", "--list", "rlp/loop/*") == ""
    assert not (repo / ".rlp" / "loops").exists()


def test_a_dirty_work_tree_is_refused(tmp_path, kind, fake):
    repo = make_repo(tmp_path / "repo", ENABLED)
    (repo / "policy" / "gate.rego").write_text("package gate\n# local edit\n")
    with pytest.raises(loop.LoopError, match="uncommitted changes"):
        run(repo)
    assert fake.prompts == []


@pytest.mark.parametrize(("iterations", "spend"), [(0, 1.0), (1, 0.0), (1, -1.0)])
def test_caps_must_be_positive(iterations, spend):
    with pytest.raises(loop.LoopError):
        loop.Options(max_iterations=iterations, max_spend_usd=spend)


def test_default_spend_cap_comes_from_platform_data():
    data = yaml.safe_load((resources.policy_dir() / "platform" / "data.yaml").read_text(encoding="utf-8"))
    assert loop.default_max_spend() == float(data["loop_max_spend_usd"]) > 0


# --- the toggle ---


def test_disabled_by_default(tmp_path, kind, fake):
    repo = make_repo(tmp_path / "repo", loops=None)
    report = run(repo)
    assert report.outcome == "disabled" and report.exit_code == 0
    assert "no loops key" in report.reason
    assert fake.prompts == []
    assert not (repo / ".rlp" / "loops").exists()


def test_rlp_loops_off_overrides_rlp_yaml_and_removing_it_re_enables(tmp_path, kind, fake):
    repo = make_repo(tmp_path / "repo", ENABLED)
    fake.steps = steps("fixed\n")
    off = run(repo, env={"RLP_LOOPS": "off"})
    assert off.outcome == "disabled" and "RLP_LOOPS=off" in off.reason
    assert fake.prompts == []
    on = run(repo, env={})
    assert on.outcome == "fixed" and len(fake.prompts) == 1


@pytest.mark.parametrize(
    ("loops", "enabled", "reason"),
    [
        (None, False, "no loops key"),
        ({"enabled": False}, False, "loops.enabled: false"),
        ({"enabled": True}, True, "every kind"),
        ({"enabled": True, "kinds": ["quality"]}, True, "loops.kinds includes it"),
        ({"enabled": True, "kinds": ["vuln"]}, False, "does not include quality"),
    ],
)
def test_toggle_reads_rlp_yaml(loops, enabled, reason):
    doc = {} if loops is None else {"loops": loops}
    t = loop.toggle(doc, "quality", env={})
    assert t.enabled is enabled and reason in t.reason and t.source == "rlp.yaml"


@pytest.mark.parametrize("value", ["off", "OFF", "0", "false", "no"])
def test_any_off_value_is_the_kill_switch(value):
    t = loop.toggle({"loops": {"enabled": True}}, "quality", env={"RLP_LOOPS": value})
    assert not t.enabled and t.source == "RLP_LOOPS"


# --- the command line ---


HARNESS = """
import sys
from pathlib import Path

from rlp_cli import loop
from rlp_cli.cli import main


def discover(root, doc):
    text = (Path(root) / "src" / "app.py").read_text(encoding="utf-8")
    return [] if "fixed" in text else [{"message": "src/app.py is broken", "path": "src/app.py"}]


def allowed(root, doc):
    return ["src"]


loop.register(loop.LoopKind("quality", "Test kind.", discover, ("lint",), allowed))
if "--no-vuln" in sys.argv:
    sys.argv.remove("--no-vuln")
    loop.KINDS.pop("vuln", None)
sys.argv[0] = "rlp"
main()
"""


@pytest.fixture
def cli_run(tmp_path):
    """`rlp` as a subprocess, with the test kind registered and the fake provider scripted."""
    harness = tmp_path / "harness.py"
    harness.write_text(HARNESS, encoding="utf-8")

    def go(*args: str, script: list[dict] | None = None, env: dict | None = None):
        path = tmp_path / "script.json"
        path.write_text(json.dumps(script or []), encoding="utf-8")
        full = {k: v for k, v in os.environ.items() if k not in ("RLP_LOOPS", "RLP_OUTPUT")}
        full |= {"RLP_FAKE_AGENT_SCRIPT": str(path), **(env or {})}
        return subprocess.run([sys.executable, str(harness), *args], capture_output=True, text=True, env=full, check=False)

    return go


def script(*contents: str, cost: float = 0.1) -> list[dict]:
    return [{"files": {"src/app.py": c}, "costUsd": cost} for c in contents]


def test_cli_prints_the_final_state_as_json(tmp_path, cli_run):
    repo = make_repo(tmp_path / "repo", ENABLED)
    result = cli_run("loop", "quality", "--path", str(repo), "--provider", "fake", "--max-iterations", "2", "--output", "json", script=script("b1\n", "b2\n", "b3\n"))
    assert result.returncode == 1, result.stderr
    doc = json.loads(result.stdout)
    assert doc["outcome"] == "escalated" and len(doc["iterations"]) == 2 and doc["ok"] is False


def test_cli_fixed_exits_0_and_names_the_state_file(tmp_path, cli_run):
    repo = make_repo(tmp_path / "repo", ENABLED)
    result = cli_run("loop", "quality", "--path", str(repo), "--provider", "fake", "--max-spend", "1", script=script("fixed\n"))
    assert result.returncode == 0, result.stderr
    assert "quality: fixed" in result.stdout and "state: .rlp/loops/quality-" in result.stdout
    assert "iteration 1: lint passed" in result.stderr


def test_cli_dry_run(tmp_path, cli_run):
    repo = make_repo(tmp_path / "repo", ENABLED)
    result = cli_run("loop", "quality", "--path", str(repo), "--provider", "fake", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "dry run, 1 finding(s); no agent called" in result.stdout
    assert "verify stages: lint; allowed paths: src" in result.stdout


@pytest.mark.parametrize("bad", [["--max-iterations", "0"], ["--max-spend", "0"], ["--max-spend", "-2"]])
def test_cli_rejects_non_positive_caps(tmp_path, cli_run, bad):
    repo = make_repo(tmp_path / "repo", ENABLED)
    result = cli_run("loop", "quality", "--path", str(repo), "--provider", "fake", *bad, script=script("fixed\n"))
    assert result.returncode == 2
    assert not (repo / ".rlp").exists()


def test_cli_status_shows_each_kind(tmp_path, cli_run):
    repo = make_repo(tmp_path / "repo", {"enabled": True, "kinds": ["quality"]})
    result = cli_run("loop", "status", "--path", str(repo), "--output", "json")
    assert result.returncode == 0, result.stderr
    rows = {r["loop"]: r for r in json.loads(result.stdout)["loops"]}
    assert rows["quality"]["enabled"] is True and rows["quality"]["implemented"] is True
    assert rows["vuln"]["enabled"] is False and "does not include vuln" in rows["vuln"]["reason"]
    off = cli_run("loop", "status", "--path", str(repo), env={"RLP_LOOPS": "off"})
    assert "quality  off RLP_LOOPS=off" in off.stdout


def test_unknown_kind_is_a_usage_error(tmp_path, cli_run):
    repo = make_repo(tmp_path / "repo", ENABLED)
    assert cli_run("loop", "refactor", "--path", str(repo)).returncode == 2


def test_an_enabled_kind_that_is_not_implemented_is_an_error(tmp_path, cli_run):
    repo = make_repo(tmp_path / "repo", ENABLED)
    result = cli_run("--no-vuln", "loop", "vuln", "--path", str(repo), "--provider", "fake", "--output", "json")
    assert result.returncode == 2
    assert "not implemented yet" in json.loads(result.stdout)["error"]


def test_installed_cli_reports_disabled_without_an_agent(rlp, tmp_path):
    """As a subprocess, the way CI runs it: no loops key, no credentials, exit 0."""
    repo = make_repo(tmp_path / "repo", loops=None)
    result = rlp("loop", "quality", "--path", str(repo), "--output", "json", env={"RLP_AGENT_PROVIDER": "claude-code", "ANTHROPIC_API_KEY": ""})
    assert result.code == 0, result.stderr
    assert result.json["outcome"] == "disabled"
    assert not (repo / ".rlp").exists()


# --- the state schema ---


def test_state_schema_is_valid_and_published():
    schema = resources.schema("LoopState")
    Draft202012Validator.check_schema(schema)
    assert schema["$id"].endswith("/schemas/v1alpha1/loop-state.schema.json")


def test_known_kinds_match_the_repo_schema():
    enum = resources.schema("Repo")["$defs"]["loops"]["properties"]["kinds"]["items"]["enum"]
    assert sorted(enum) == sorted(loop.KNOWN_KINDS)
