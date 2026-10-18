"""Agent providers (#47): the interface loops use, the fake provider, and claude-code.

No test calls a real model. The claude-code tests replace the process runner
with a stub that edits the tree and prints what `claude --print
--output-format json` prints.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from rlp_cli import agent

CREDS = {"ANTHROPIC_API_KEY": "sk-test", "PATH": ""}


def git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)


# --- a loop written only against the interface ---


def tiny_loop(workdir: Path, gate, provider_name: str | None, env: dict, cap_usd: float = 1.0, per_call_usd: float = 0.5) -> dict:
    """What a loop looks like: it imports the interface and nothing provider-specific."""
    provider = agent.get_provider(provider_name, env=env)
    provider.check()
    spend = agent.Spend(cap_usd)
    while not gate(workdir):
        try:
            budget = spend.budget(per_call_usd)
        except agent.AgentError:
            return {"fixed": False, "spent": spend.spent_usd, "calls": spend.calls, "reason": "spend cap"}
        result = provider.run("make the gate pass", workdir, ["src"], budget)
        spend.charge(result)
        if result.outside_allowed:
            return {"fixed": False, "spent": spend.spent_usd, "calls": spend.calls, "reason": "outside allowed paths"}
        if not result.ok:
            return {"fixed": False, "spent": spend.spent_usd, "calls": spend.calls, "reason": result.summary}
    return {"fixed": True, "spent": spend.spent_usd, "calls": spend.calls}


def gate(workdir: Path) -> bool:
    path = workdir / "src" / "app.py"
    return path.exists() and "fixed" in path.read_text()


def write_script(tmp_path: Path, steps: list[dict]) -> str:
    script = tmp_path / "script.json"
    script.write_text(json.dumps(steps))
    return str(script)


def test_a_loop_runs_on_the_fake_provider_selected_by_env(tmp_path):
    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    (work / "src" / "app.py").write_text("broken\n")
    script = write_script(tmp_path, [
        {"files": {"src/app.py": "still broken\n"}, "costUsd": 0.2},
        {"files": {"src/app.py": "fixed\n"}, "costUsd": 0.3},
    ])
    env = {agent.PROVIDER_ENV: "fake", agent.FAKE_SCRIPT_ENV: script}
    outcome = tiny_loop(work, gate, None, env)
    assert outcome == {"fixed": True, "spent": pytest.approx(0.5), "calls": 2}


def test_the_spend_cap_counts_the_reported_cost(tmp_path):
    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    provider = agent.FakeProvider([agent.FakeStep(files={"src/app.py": f"try {i}\n"}, cost_usd=0.4) for i in range(5)])
    spend = agent.Spend(1.0)
    while True:
        try:
            budget = spend.budget(0.5)
        except agent.AgentError as err:
            assert "spend cap of $1.00 reached after 3 call(s)" in str(err)
            break
        spend.charge(provider.run("fix", work, ["src"], budget))
    # 0.4 + 0.4 + 0.4: the third call got only the $0.20 left and its report went over it
    assert spend.spent_usd == pytest.approx(1.2)
    assert spend.calls == 3


def test_spend_budget_is_capped_by_what_is_left():
    spend = agent.Spend(1.0, spent_usd=0.75)
    assert spend.budget(0.5).max_usd == pytest.approx(0.25)
    assert agent.Spend(1.0).budget(0.5).max_usd == 0.5


def test_budget_must_be_positive():
    with pytest.raises(ValueError):
        agent.Budget(0)


# --- fake provider ---


def test_fake_applies_one_patch_per_call_and_reports_fixed_cost(tmp_path):
    (tmp_path / "keep.txt").write_text("x")
    (tmp_path / "gone.txt").write_text("x")
    fake = agent.FakeProvider([agent.FakeStep(files={"src/a.py": "a\n", "gone.txt": None}, cost_usd=0.07, summary="did it")])
    result = fake.run("prompt one", tmp_path, ["."], agent.Budget(1))
    assert result.ok and result.provider == "fake"
    assert result.changed_files == ["gone.txt", "src/a.py"]
    assert result.outside_allowed == []
    assert result.cost_usd == 0.07 and result.summary == "did it"
    assert result.tokens == 150
    assert not (tmp_path / "gone.txt").exists()
    assert fake.prompts == ["prompt one"]
    exhausted = fake.run("prompt two", tmp_path, ["."], agent.Budget(1))
    assert not exhausted.ok and exhausted.changed_files == [] and exhausted.cost_usd == 0


def test_changes_outside_allowed_paths_are_flagged(tmp_path):
    fake = agent.FakeProvider([agent.FakeStep(files={"src/a.py": "a", "policy/quality.rego": "weakened", "srcx/b.py": "b"})])
    result = fake.run("fix", tmp_path, ["src"], agent.Budget(1))
    assert result.changed_files == ["policy/quality.rego", "src/a.py", "srcx/b.py"]
    assert result.outside_allowed == ["policy/quality.rego", "srcx/b.py"]


def test_a_step_that_costs_more_than_the_budget_is_not_ok(tmp_path):
    result = agent.FakeProvider([agent.FakeStep(cost_usd=2)]).run("fix", tmp_path, ["."], agent.Budget(1))
    assert result.over_budget and not result.ok and result.cost_usd == 2


def test_allowed_paths_must_stay_inside_the_tree(tmp_path):
    with pytest.raises(agent.AgentError, match="outside the working tree"):
        agent.FakeProvider().run("fix", tmp_path, ["../elsewhere"], agent.Budget(1))
    with pytest.raises(agent.AgentError, match="empty"):
        agent.FakeProvider().run("fix", tmp_path, [], agent.Budget(1))


def test_fake_script_errors(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    with pytest.raises(agent.AgentError, match="JSON list"):
        agent.get_provider("fake", env={agent.FAKE_SCRIPT_ENV: str(bad)})
    with pytest.raises(agent.AgentError, match="can't read"):
        agent.get_provider("fake", env={agent.FAKE_SCRIPT_ENV: str(tmp_path / "missing.json")})


def test_snapshot_skips_git_ignored_files(tmp_path):
    git_init(tmp_path)
    (tmp_path / ".gitignore").write_text("build/\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.bin").write_text("x")
    (tmp_path / "a.py").write_text("x")
    assert sorted(agent.snapshot(tmp_path)) == [".gitignore", "a.py"]


def test_snapshot_outside_git_skips_dot_git(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("x")
    (tmp_path / "a.py").write_text("x")
    assert list(agent.snapshot(tmp_path)) == ["a.py"]


# --- selection ---


def test_provider_selection_order():
    assert agent.provider_name(None, {}) == "claude-code"
    assert agent.provider_name(None, {agent.PROVIDER_ENV: "fake"}) == "fake"
    assert agent.provider_name("claude-code", {agent.PROVIDER_ENV: "fake"}) == "claude-code"
    assert isinstance(agent.get_provider(None, {agent.PROVIDER_ENV: "fake"}), agent.FakeProvider)
    assert isinstance(agent.get_provider(None, {}), agent.AgentProvider)


def test_unknown_provider():
    with pytest.raises(agent.AgentError, match="choose one of: claude-code, fake"):
        agent.get_provider("gpt", env={})


# --- claude-code ---


def claude_json(cost=0.12, subtype="success", result="Fixed the lint errors.", is_error=False) -> str:
    return json.dumps({
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": result,
        "total_cost_usd": cost,
        "num_turns": 3,
        "usage": {"input_tokens": 10, "cache_creation_input_tokens": 200, "cache_read_input_tokens": 1000, "output_tokens": 300},
    })


class StubClaude:
    """Stands in for subprocess.run: edits the tree like the agent would, then prints JSON."""

    def __init__(self, edits: dict[str, str], stdout: str, returncode: int = 0):
        self.edits, self.stdout, self.returncode = edits, stdout, returncode
        self.calls: list[dict] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, **kwargs})
        for rel, content in self.edits.items():
            path = Path(kwargs["cwd"]) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "")


def claude_provider(tmp_path: Path, stub: StubClaude, env: dict | None = None) -> agent.ClaudeCodeProvider:
    # Any real executable passes the PATH check; the stub runner stands in for claude itself.
    return agent.ClaudeCodeProvider(env={**CREDS, **(env or {})}, executable=sys.executable, runner=stub)


def test_claude_code_refuses_without_credentials_and_names_the_variable(tmp_path):
    provider = agent.ClaudeCodeProvider(env={"PATH": ""}, runner=StubClaude({}, claude_json()))
    with pytest.raises(agent.CredentialsError, match="set ANTHROPIC_API_KEY"):
        provider.run("fix", tmp_path, ["."], agent.Budget(1))
    assert provider.runner.calls == []


def test_claude_code_accepts_the_action_oauth_token(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    provider = claude_provider(tmp_path, StubClaude({}, claude_json()), env={"ANTHROPIC_API_KEY": "", "CLAUDE_CODE_OAUTH_TOKEN": "t"})
    provider.check()


def test_claude_code_needs_the_cli_on_path(tmp_path):
    provider = agent.ClaudeCodeProvider(env={**CREDS, "PATH": str(tmp_path)}, executable="claude-not-installed", runner=StubClaude({}, claude_json()))
    with pytest.raises(agent.AgentError, match="not on PATH"):
        provider.check()


def test_claude_code_maps_json_to_a_result(tmp_path):
    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    (work / "src" / "app.py").write_text("broken\n")
    stub = StubClaude({"src/app.py": "fixed\n", "Taskfile.yml": "cheat"}, claude_json(cost=0.12))
    result = claude_provider(tmp_path, stub).run("fix the lint", work, ["src"], agent.Budget(0.5))
    assert result.ok and result.provider == "claude-code"
    assert result.summary == "Fixed the lint errors."
    assert result.changed_files == ["Taskfile.yml", "src/app.py"]
    assert result.outside_allowed == ["Taskfile.yml"]
    assert (result.input_tokens, result.output_tokens) == (1210, 300)
    assert result.cost_usd == 0.12 and not result.over_budget
    assert result.as_dict()["costUsd"] == 0.12

    call = stub.calls[0]
    cmd = call["cmd"]
    assert call["input"] == "fix the lint" and Path(call["cwd"]) == work
    assert cmd[:2] == [sys.executable, "--print"]
    assert cmd[cmd.index("--max-budget-usd") + 1] == "0.5000"
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert cmd[cmd.index("--setting-sources") + 1] == "user"
    tools = cmd[cmd.index("--allowedTools") + 1:]
    assert tools == ["Read", "Glob", "Grep", "Edit(./src/**)", "Write(./src/**)"]
    assert call["env"]["ANTHROPIC_API_KEY"] == "sk-test"


def test_claude_code_allowed_file_and_whole_tree_patterns(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "pyproject.toml").write_text("")
    provider = claude_provider(tmp_path, StubClaude({}, claude_json()))
    assert provider.command(work, ["pyproject.toml"], agent.Budget(1))[-2:] == ["Edit(./pyproject.toml)", "Write(./pyproject.toml)"]
    assert provider.command(work, ["."], agent.Budget(1))[-2:] == ["Edit(./**)", "Write(./**)"]


def test_claude_code_budget_stop_is_reported(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    stub = StubClaude({}, claude_json(cost=0.51, subtype="error_max_budget_usd", result="", is_error=True), returncode=1)
    result = claude_provider(tmp_path, stub).run("fix", work, ["."], agent.Budget(0.5))
    assert not result.ok and result.over_budget
    assert result.cost_usd == 0.51
    assert result.summary == "claude stopped: error_max_budget_usd"


def test_claude_code_cost_over_budget_is_caught_even_on_success(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    result = claude_provider(tmp_path, StubClaude({}, claude_json(cost=0.9))).run("fix", work, ["."], agent.Budget(0.5))
    assert result.over_budget and not result.ok


def test_claude_code_without_json_output_is_an_error(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(agent.AgentError, match="without a JSON result"):
        claude_provider(tmp_path, StubClaude({}, "boom", returncode=1)).run("fix", work, ["."], agent.Budget(1))


def test_parse_claude_stream_list_and_missing_result():
    stream = json.dumps([{"type": "system"}, json.loads(claude_json(cost=0.3))])
    assert agent.parse_claude(stream).cost_usd == 0.3
    with pytest.raises(agent.AgentError, match="no result message"):
        agent.parse_claude(json.dumps({"type": "assistant"}))


# --- rlp agent check ---


def test_agent_check_with_the_fake_provider(rlp):
    result = rlp("agent", "check", "--provider", "fake", "-o", "json")
    assert result.code == 0
    assert result.json == {"ok": True, "provider": "fake"}


def test_agent_check_picks_the_provider_from_env(rlp):
    result = rlp("agent", "check", env={agent.PROVIDER_ENV: "fake"})
    assert result.code == 0
    assert "agent provider fake: ready" in result.stdout


def test_agent_check_without_credentials_names_the_variable(rlp):
    result = rlp("agent", "check", "--provider", "claude-code", "-o", "json", env={"ANTHROPIC_API_KEY": "", "CLAUDE_CODE_OAUTH_TOKEN": ""})
    assert result.code == 2
    assert result.json["ok"] is False
    assert "ANTHROPIC_API_KEY" in result.json["error"]


def test_agent_check_rejects_an_unknown_provider(rlp):
    assert rlp("agent", "check", "--provider", "nope").code == 2
    result = rlp("agent", "check", "-o", "json", env={agent.PROVIDER_ENV: "nope"})
    assert result.code == 2 and "unknown agent provider" in result.json["error"]
