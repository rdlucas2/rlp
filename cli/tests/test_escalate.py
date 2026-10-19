"""Escalation issues for loops that give up (#52): rlp loop escalate.

The rendered Markdown is pinned by snapshots in cli/tests/snapshots/. After a
deliberate change to the format, regenerate them with
RLP_UPDATE_SNAPSHOTS=1 pytest cli/tests/test_escalate.py and review the diff.

Nothing here calls GitHub: posting goes to a fake client, or to a local HTTP
server standing in for the REST API.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from rlp_cli import agent, escalate, loop

SNAPSHOTS = Path(__file__).parent / "snapshots"
STATE = SNAPSHOTS / "escalation-state.json"
STATE_FILE = ".rlp/loops/quality-20261019T120000Z-abc123.json"
TRIGGER = "https://github.com/acme/app/actions/runs/42"
GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@example.com"]
LINT = f'"{Path(sys.executable).as_posix()}" -c "import sys; print(\'lint says: still broken\'); sys.exit(0 if \'fixed\' in open(\'src/app.py\').read() else 1)"'


def snapshot(name: str, actual: str) -> None:
    path = SNAPSHOTS / name
    if os.environ.get("RLP_UPDATE_SNAPSHOTS") == "1":
        path.write_text(actual, encoding="utf-8", newline="\n")
    assert path.is_file(), f"no snapshot {name}; run with RLP_UPDATE_SNAPSHOTS=1"
    assert actual == path.read_text(encoding="utf-8")


def state() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8"))


# --- rendering ---


def test_issue_body_snapshot():
    snapshot("escalation-issue.md", escalate.render(state(), TRIGGER, STATE_FILE))


def test_comment_snapshot():
    snapshot("escalation-comment.md", escalate.render_comment(state(), TRIGGER, STATE_FILE))


def test_snapshot_state_is_a_valid_escalated_state():
    assert escalate.load_state(STATE)["outcome"] == "escalated"


def test_title_and_labels_name_the_loop_kind():
    s = state()
    assert escalate.title(s) == "rlp loop quality escalated on main"
    assert escalate.labels(s) == ["needs:human", "area:agent", "loop:quality"]


def test_long_gate_output_keeps_its_tail():
    s = state()
    s["iterations"][-1]["gates"][1]["detail"] = "noise\n" * 2000 + "THE REAL ERROR"
    body = escalate.render(s)
    assert "THE REAL ERROR" in body and "characters cut" in body
    assert len(body) < escalate.GATE_OUTPUT_LIMIT + 8000


def test_many_findings_are_capped():
    s = state()
    s["findings"] = [{"message": f"finding {n}"} for n in range(50)]
    body = escalate.render(s)
    assert "finding 19" in body and "finding 20\n" not in body and "and 30 more" in body


def test_no_failing_gate_says_so():
    s = state()
    s["outcome"], s["reason"] = "error", "git switch failed"
    for it in s["iterations"]:
        it["gates"] = []
    body = escalate.render(s)
    assert "No gate failed in a kept attempt" in body and "error: git switch failed" in body


def test_trigger_comes_from_the_actions_environment():
    env = {"GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "acme/app", "GITHUB_RUN_ID": "42"}
    assert escalate.trigger_url(env) == TRIGGER
    assert escalate.trigger_url({}) == ""
    assert "not recorded" in escalate.render(state(), "")


# --- reading state files ---


@pytest.mark.parametrize("outcome", ["fixed", "no-findings", "running"])
def test_only_runs_that_gave_up_are_escalated(tmp_path, outcome):
    s = state()
    s["outcome"] = outcome
    path = tmp_path / "s.json"
    path.write_text(json.dumps(s), encoding="utf-8")
    with pytest.raises(escalate.EscalationError, match=f"outcome is {outcome}"):
        escalate.load_state(path)


def test_a_file_that_is_not_loop_state_is_refused(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"kind": "Repo"}), encoding="utf-8")
    with pytest.raises(escalate.EscalationError, match="not a loop state file"):
        escalate.load_state(path)
    with pytest.raises(escalate.EscalationError, match="no such state file"):
        escalate.load_state(tmp_path / "missing.json")


# --- an escalated fake-provider run, end to end ---


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def escalated_state(tmp_path, monkeypatch) -> Path:
    """Run a real loop with the fake provider until it escalates; return its state file."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    doc = {
        "apiVersion": "rlp/v1alpha1",
        "kind": "Repo",
        "metadata": {"name": "looped", "project": "looped"},
        "loops": {"enabled": True},
        "components": [{
            "name": "app",
            "language": "python",
            "artifact": {"kind": "container-image"},
            "runtime": {"kind": "container-service", "port": 8000},
            "verbs": {"lint": LINT},
        }],
    }
    (repo / "rlp.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    (repo / "src" / "app.py").write_text("broken\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".rlp/\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, *GIT_ID, "commit", "-q", "-m", "init")

    def discover(root, _doc):
        text = (root / "src" / "app.py").read_text(encoding="utf-8")
        return [] if "fixed" in text else [{"message": f"src/app.py says {text.strip()}", "path": "src/app.py", "line": 1}]

    monkeypatch.setitem(loop.KINDS, "quality", loop.LoopKind("quality", "Test kind.", discover, ("lint",), lambda root, doc: ["src"]))
    provider = agent.FakeProvider([agent.FakeStep(files={"src/app.py": f"attempt {n}\n"}, cost_usd=0.1, summary=f"tried attempt {n}") for n in (1, 2, 3)])
    monkeypatch.setitem(agent.PROVIDERS, "fake", lambda env: provider)
    report = loop.run(repo, "quality", loop.Options(max_iterations=3, max_spend_usd=5, provider="fake", env={"PATH": os.environ.get("PATH", "")}))
    assert report.outcome == "escalated"
    return repo / report.state_file


def test_escalated_run_body_has_every_iteration_and_the_last_gate_output(escalated_state):
    doc = escalate.load_state(escalated_state)
    body = escalate.render(doc, TRIGGER)
    for n in (1, 2, 3):
        assert f"### Iteration {n}" in body and f"tried attempt {n}" in body
        assert f"src/app.py says attempt {n - 1}" in body or n == 1
    last = doc["iterations"][-1]["gates"][0]
    assert last["status"] == "failed" and last["detail"].strip()
    section = body.split("## Last failing gate", 1)[1].split("## Findings", 1)[0]
    assert "iteration 3" in section and last["detail"].strip().splitlines()[-1].rstrip() in section
    assert TRIGGER in body and doc["branch"] in body and "$0.30 of $5.00" in body


# --- posting ---


class FakeGitHub:
    """Records requests; holds issues in memory like the REST API would."""

    def __init__(self, issues: list[dict] | None = None) -> None:
        self.issues = issues or []
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            return [i for i in self.issues if i["state"] == "open"]
        if path.endswith("/comments"):
            number = int(path.split("/")[-2])
            return {"html_url": f"https://github.com/acme/app/issues/{number}#issuecomment-1"}
        number = len(self.issues) + 1
        self.issues.append({"number": number, "state": "open", "html_url": f"https://github.com/acme/app/issues/{number}", **body})
        return self.issues[-1]


def test_first_escalation_opens_an_issue_and_the_second_comments_on_it():
    gh, s = FakeGitHub(), state()
    first = escalate.post(gh, "acme/app", s, TRIGGER)
    assert first.action == "opened" and first.number == 1
    method, path, body = gh.calls[-1]
    assert (method, path) == ("POST", "/repos/acme/app/issues")
    assert body["labels"] == ["needs:human", "area:agent", "loop:quality"] and body["title"] == "rlp loop quality escalated on main"

    s["runId"] = "20261019T160000Z-def456"
    second = escalate.post(gh, "acme/app", s, TRIGGER)
    assert second.action == "commented" and second.number == 1 and len(gh.issues) == 1
    method, path, body = gh.calls[-1]
    assert (method, path) == ("POST", "/repos/acme/app/issues/1/comments")
    assert body["body"].startswith("## Escalated again: run `20261019T160000Z-def456`")
    assert "labels=needs%3Ahuman%2Carea%3Aagent%2Cloop%3Aquality" in gh.calls[0][1]


def test_a_different_base_or_kind_or_a_closed_issue_opens_a_new_one():
    s = state()
    gh = FakeGitHub([
        {"number": 7, "state": "open", "body": "<!-- rlp-loop-escalation kind=quality base=release -->"},
        {"number": 8, "state": "open", "body": "<!-- rlp-loop-escalation kind=vuln base=main -->"},
        {"number": 9, "state": "closed", "body": escalate.marker(s)},
        {"number": 10, "state": "open", "body": escalate.marker(s), "pull_request": {}},
    ])
    assert escalate.post(gh, "acme/app", s).action == "opened"


def test_posting_needs_a_token_and_a_repo():
    with pytest.raises(escalate.EscalationError, match="GH_TOKEN"):
        escalate.client_from_env({})
    with pytest.raises(escalate.EscalationError, match="--repo"):
        escalate.repo_from_env(None, {})
    assert escalate.repo_from_env(None, {"GITHUB_REPOSITORY": "acme/app"}) == "acme/app"


# --- the command ---


@pytest.fixture
def api():
    """A local stand-in for the GitHub REST API, recording requests."""
    seen: list[dict] = []
    issues: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, payload):
            raw = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            seen.append({"method": "GET", "path": self.path, "auth": self.headers.get("Authorization")})
            self._reply(200, issues)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"method": "POST", "path": self.path, "body": body, "auth": self.headers.get("Authorization")})
            if self.path.endswith("/comments"):
                self._reply(201, {"html_url": "http://example.test/issues/1#c"})
                return
            issue = {"number": len(issues) + 1, "html_url": f"http://example.test/issues/{len(issues) + 1}", **body}
            issues.append(issue)
            self._reply(201, issue)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {"url": f"http://127.0.0.1:{server.server_address[1]}", "seen": seen}
    server.shutdown()


def cli_env(**extra: str) -> dict:
    env = {k: "" for k in ("GH_TOKEN", "GITHUB_TOKEN", "GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_SERVER_URL")}
    return {**env, "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost", **extra}


def test_cli_prints_the_issue_markdown_without_posting(rlp):
    result = rlp("loop", "escalate", str(STATE), "--trigger", TRIGGER, env=cli_env())
    assert result.code == 0, result.stderr
    assert result.stdout.startswith("<!-- rlp-loop-escalation kind=quality base=main -->")
    assert "### Iteration 3" in result.stdout and TRIGGER in result.stdout


def test_cli_json_carries_title_labels_and_body(rlp):
    result = rlp("loop", "escalate", str(STATE), "--output", "json", env=cli_env())
    assert result.code == 0, result.stderr
    doc = result.json
    assert doc["ok"] and doc["title"] == "rlp loop quality escalated on main"
    assert doc["labels"] == ["needs:human", "area:agent", "loop:quality"] and "## Last failing gate" in doc["body"]


def test_cli_post_opens_then_comments(rlp, api):
    env = cli_env(GH_TOKEN="t0ken", GITHUB_API_URL=api["url"], GITHUB_SERVER_URL="https://github.com", GITHUB_REPOSITORY="acme/app", GITHUB_RUN_ID="42")
    first = rlp("loop", "escalate", str(STATE), "--post", env=env)
    assert first.code == 0, first.stderr
    assert first.stdout.strip() == "opened acme/app#1: http://example.test/issues/1"
    opened = api["seen"][1]
    assert opened["path"] == "/repos/acme/app/issues" and opened["auth"] == "Bearer t0ken"
    assert TRIGGER in opened["body"]["body"]

    second = rlp("loop", "escalate", str(STATE), "--post", "--output", "json", env=env)
    assert second.code == 0, second.stderr
    assert second.json["action"] == "commented" and second.json["number"] == 1
    assert api["seen"][-1]["path"] == "/repos/acme/app/issues/1/comments"


def test_cli_post_without_a_token_is_a_usage_error(rlp):
    result = rlp("loop", "escalate", str(STATE), "--post", "--repo", "acme/app", env=cli_env())
    assert result.code == 2 and "GH_TOKEN" in result.stderr


def test_cli_refuses_a_fixed_run(rlp, tmp_path):
    s = state()
    s["outcome"] = "fixed"
    path = tmp_path / "s.json"
    path.write_text(json.dumps(s), encoding="utf-8")
    result = rlp("loop", "escalate", str(path), env=cli_env())
    assert result.code == 2 and "only escalated or error runs" in result.stderr
