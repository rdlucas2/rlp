"""`rlp pipeline gate-review`: agent gate changes wait for a human's approval (#73).

The unit tests judge review lists in memory. The command tests run rlp as a
subprocess against a local stand-in for the GitHub reviews endpoint
(GITHUB_API_URL), with a pull_request event file like the one Actions writes.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from rlp_cli import gate_review

HEAD = "a" * 40
OLD = "b" * 40
PR = gate_review.PullRequest("acme/app", 7, HEAD, "rdlucas2")
AGENTS = ["localrlp[bot]"]
CHANGE = {"rule": "RLP-A001", "file": "policy/x.rego", "field": "(file)", "message": "[RLP-A001] policy/x.rego (file): an agent-authored change edits a protected gate file. Fix: revert it.", "namespace": "rlp.agent"}


def approval(login: str, commit: str = HEAD, state: str = "APPROVED", kind: str = "User") -> dict:
    return {"user": {"login": login, "type": kind}, "state": state, "commit_id": commit}


def results(changes: list[dict] | None) -> dict:
    data = {} if changes is None else {"gate_changes": changes}
    return {"policy": {"stage": "policy", "status": "passed", "detail": "", "data": data}}


class FakeGitHub:
    def __init__(self, reviews: list[dict]) -> None:
        self.reviews = reviews
        self.paths: list[str] = []

    def request(self, method, path, body=None):
        self.paths.append(path)
        page = int(path.rsplit("page=", 1)[1])
        size = gate_review.PER_PAGE
        return self.reviews[(page - 1) * size : page * size]


def decide(reviews: list[dict], changes=(CHANGE,), pr=PR) -> gate_review.Verdict:
    return gate_review.review(results(list(changes)), pr, lambda: FakeGitHub(reviews), AGENTS)


# --- the decision ---


def test_no_gate_changes_pass_without_a_review_or_an_api_call():
    def no_client():
        raise AssertionError("no API call is needed without gate changes")

    verdict = gate_review.review(results(None), None, no_client, AGENTS)
    assert verdict.ok
    assert verdict.detail == "no gate changes; no review needed"


def test_gate_changes_fail_with_no_review():
    verdict = decide([])
    assert not verdict.ok
    assert verdict.gate_changes == [CHANGE]
    assert "need an approving review" in verdict.detail


def test_a_human_approval_on_the_head_commit_passes():
    verdict = decide([approval("reviewer")])
    assert verdict.ok
    assert verdict.approved_by == ["reviewer"]


def test_an_approval_of_an_older_commit_does_not_count():
    verdict = decide([approval("reviewer", commit=OLD)])
    assert not verdict.ok
    assert verdict.ignored == [{"login": "reviewer", "reason": f"approved an older commit ({OLD[:12]})"}]


@pytest.mark.parametrize(
    ("review", "reason"),
    [
        (approval("rdlucas2"), "the PR author"),
        (approval("RDLucas2"), "the PR author"),
        (approval("localrlp[bot]", kind="Bot"), "an agent account (agent_authors)"),
        (approval("other-app[bot]", kind="Bot"), "a bot account"),
    ],
)
def test_approvals_from_the_author_or_an_agent_do_not_count(review, reason):
    verdict = decide([review])
    assert not verdict.ok
    assert verdict.ignored == [{"login": review["user"]["login"], "reason": reason}]


def test_a_later_verdict_replaces_an_approval_but_a_comment_does_not():
    assert not decide([approval("reviewer"), approval("reviewer", state="CHANGES_REQUESTED")]).ok
    assert not decide([approval("reviewer"), approval("reviewer", state="DISMISSED")]).ok
    assert decide([approval("reviewer"), approval("reviewer", state="COMMENTED")]).ok
    assert decide([approval("reviewer", state="CHANGES_REQUESTED"), approval("reviewer")]).ok


def test_reviews_are_read_page_by_page():
    filler = [approval(f"commenter{i}", state="COMMENTED") for i in range(gate_review.PER_PAGE)]
    client = FakeGitHub([*filler, approval("reviewer")])
    verdict = gate_review.review(results([CHANGE]), PR, lambda: client, AGENTS)
    assert verdict.ok
    assert [p.rsplit("page=", 1)[1] for p in client.paths] == ["1", "2"]
    assert client.paths[0] == "/repos/acme/app/pulls/7/reviews?per_page=100&page=1"


def test_no_policy_result_fails_closed():
    with pytest.raises(gate_review.GateReviewError, match="policy stage did not run"):
        gate_review.review({}, PR, lambda: FakeGitHub([]), AGENTS)


def test_gate_changes_without_a_pull_request_are_an_error():
    with pytest.raises(gate_review.GateReviewError, match="no pull request"):
        gate_review.review(results([CHANGE]), None, lambda: FakeGitHub([]), AGENTS)
    incomplete = gate_review.PullRequest("acme/app", 7, "", "rdlucas2")
    with pytest.raises(gate_review.GateReviewError, match="head commit"):
        gate_review.review(results([CHANGE]), incomplete, lambda: FakeGitHub([]), AGENTS)


def test_agent_authors_come_from_the_platform_data():
    assert "localrlp[bot]" in gate_review.agent_authors()


def test_the_pull_request_comes_from_the_event_and_options_override_it(tmp_path: Path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 7, "head": {"sha": HEAD}, "user": {"login": "localrlp[bot]"}}}), encoding="utf-8")
    pr = gate_review.pull_request_from_env({"GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": "acme/app"})
    assert pr == gate_review.PullRequest("acme/app", 7, HEAD, "localrlp[bot]")
    assert gate_review.resolve_pull_request(pr, number=8).number == 8
    assert gate_review.resolve_pull_request(None) is None
    push = tmp_path / "push.json"
    push.write_text(json.dumps({"ref": "refs/heads/main"}), encoding="utf-8")
    assert gate_review.pull_request_from_env({"GITHUB_EVENT_PATH": str(push)}) is None


def test_summary_lists_the_changes_and_the_approvals_that_do_not_count():
    text = gate_review.summary(decide([approval("rdlucas2")]))
    assert "Result: **failed**" in text
    assert "- [RLP-A001] policy/x.rego" in text
    assert "- @rdlucas2: the PR author" in text


# --- the command ---


@pytest.fixture
def github():
    """A local stand-in for GET /repos/{repo}/pulls/{n}/reviews; set .reviews before calling rlp."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            server.requests.append((self.path, self.headers.get("Authorization")))
            body = json.dumps(server.reviews if "page=1" in self.path else []).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.reviews, server.requests = [], []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def write_results(root: Path, changes: list[dict] | None) -> None:
    path = root / ".rlp" / "pipeline" / "results.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(list(results(changes).values())), encoding="utf-8")


def event_env(tmp_path: Path, server) -> dict:
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 7, "head": {"sha": HEAD}, "user": {"login": "localrlp[bot]"}}}), encoding="utf-8")
    return {
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_REPOSITORY": "acme/app",
        "GITHUB_API_URL": f"http://127.0.0.1:{server.server_address[1]}",
        "GH_TOKEN": "test-token",
    }


def test_command_passes_without_gate_changes(rlp, tmp_path: Path):
    write_results(tmp_path, [])
    result = rlp("pipeline", "gate-review", "-o", "json", cwd=tmp_path, env={"GITHUB_EVENT_PATH": "", "GH_TOKEN": ""})
    assert result.code == 0, result.stderr
    assert result.json["ok"] is True


def test_command_holds_an_agent_gate_change_until_a_human_approves_the_head(rlp, tmp_path: Path, github):
    write_results(tmp_path, [CHANGE])
    env = event_env(tmp_path, github)

    github.reviews = [approval("localrlp[bot]", kind="Bot"), approval("reviewer", commit=OLD)]
    held = rlp("pipeline", "gate-review", cwd=tmp_path, env=env)
    assert held.code == 1, held.stderr
    assert "Result: **failed**" in held.stdout
    assert "- @reviewer: approved an older commit" in held.stdout
    assert github.requests[0] == ("/repos/acme/app/pulls/7/reviews?per_page=100&page=1", "Bearer test-token")

    github.reviews = [approval("reviewer")]
    passed = rlp("pipeline", "gate-review", "-o", "json", cwd=tmp_path, env=env)
    assert passed.code == 0, passed.stderr
    assert passed.json["approved_by"] == ["reviewer"]
    assert passed.json["pull_request"] == {"repo": "acme/app", "number": 7, "head_sha": HEAD, "author": "localrlp[bot]"}


def test_command_fails_without_results_or_a_token(rlp, tmp_path: Path, github):
    missing = rlp("pipeline", "gate-review", "-o", "json", cwd=tmp_path)
    assert missing.code == 2
    assert "no pipeline results" in missing.json["error"]

    write_results(tmp_path, [CHANGE])
    env = {**event_env(tmp_path, github), "GH_TOKEN": "", "GITHUB_TOKEN": ""}
    no_token = rlp("pipeline", "gate-review", "-o", "json", cwd=tmp_path, env=env)
    assert no_token.code == 2
    assert "GH_TOKEN" in no_token.json["error"]
