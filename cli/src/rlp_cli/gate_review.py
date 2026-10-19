"""The gate-review check: `rlp pipeline gate-review` (#73).

The rlp.agent rules (policy/agent/) flag an agent-authored change that touches
a gate, and the policy stage records each flag in results.json under
`data.gate_changes`. The flags are warnings, so the policy stage passes; this
check turns them into a merge block only a human can lift:

- no gate changes: the check passes, with no review needed and no API call;
- gate changes: the check passes only when the pull request has an approving
  review on its current head commit from someone who is neither the PR author
  nor an agent account (`agent_authors` in policy/platform/data.yaml, or any
  GitHub App bot). A push moves the head, so it needs a fresh approval.

Each reviewer counts by their latest review that has a verdict (approved,
changes requested or dismissed); comment-only reviews don't change it. The pull
request (repository, number, head commit, author) comes from the
pull_request or pull_request_review event at $GITHUB_EVENT_PATH, or from the
command's options. Reviews are read through the GitHub REST API with GH_TOKEN
(else GITHUB_TOKEN), which needs `pull-requests: read`.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import yaml

from rlp_cli import escalate, resources

PER_PAGE = 100
# Review states that set a reviewer's verdict; COMMENTED and PENDING don't.
VERDICTS = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}


class GateReviewError(Exception):
    """The results, the pull request or its reviews could not be read."""


class Client(Protocol):
    def request(self, method: str, path: str, body: dict | None = None) -> object: ...


@dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int
    head_sha: str
    author: str

    def as_dict(self) -> dict:
        return {"repo": self.repo, "number": self.number, "head_sha": self.head_sha, "author": self.author}


@dataclass
class Verdict:
    ok: bool
    detail: str
    gate_changes: list[dict] = field(default_factory=list)
    approved_by: list[str] = field(default_factory=list)
    ignored: list[dict] = field(default_factory=list)  # {"login", "reason"}
    pull_request: PullRequest | None = None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "detail": self.detail,
            "gate_changes": self.gate_changes,
            "approved_by": self.approved_by,
            "ignored": self.ignored,
            "pull_request": self.pull_request.as_dict() if self.pull_request else None,
        }


# --- inputs ---


def agent_authors() -> list[str]:
    """GitHub logins that count as agents, from policy/platform/data.yaml."""
    data = yaml.safe_load((resources.policy_dir() / "platform" / "data.yaml").read_text(encoding="utf-8")) or {}
    return [str(a) for a in data.get("agent_authors") or []]


def load_results(path: Path) -> dict[str, dict]:
    if not path.is_file():
        raise GateReviewError(f"no pipeline results at {path}; run the policy stage first")
    try:
        return {r["stage"]: r for r in json.loads(path.read_text(encoding="utf-8"))}
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as err:
        raise GateReviewError(f"can't read pipeline results at {path}: {err}") from err


def gate_changes(results: Mapping[str, dict]) -> list[dict]:
    """The policy stage's gate changes. Without a policy result they are unknown, which fails the check."""
    stage = results.get("policy")
    if stage is None:
        raise GateReviewError("the policy stage did not run, so the gate changes are unknown")
    return list((stage.get("data") or {}).get("gate_changes") or [])


def pull_request_from_env(env: Mapping[str, str] | None = None) -> PullRequest | None:
    """The pull request of a pull_request or pull_request_review event, or None outside one."""
    env = os.environ if env is None else env
    event_path = env.get("GITHUB_EVENT_PATH")
    if not event_path or not Path(event_path).is_file():
        return None
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise GateReviewError(f"can't read the GitHub event at {event_path}: {err}") from err
    pr = event.get("pull_request") if isinstance(event, dict) else None
    if not isinstance(pr, dict):
        return None
    repo = env.get("GITHUB_REPOSITORY") or ((event.get("repository") or {}).get("full_name") or "")
    return PullRequest(
        repo=repo,
        number=int(pr.get("number") or 0),
        head_sha=((pr.get("head") or {}).get("sha") or ""),
        author=((pr.get("user") or {}).get("login") or ""),
    )


def resolve_pull_request(
    event: PullRequest | None, repo: str = "", number: int | None = None, head_sha: str = "", author: str = "",
) -> PullRequest | None:
    """The event's pull request with any option overriding its field; None when there is nothing to go on."""
    if event is None and not (repo or number or head_sha or author):
        return None
    base = event or PullRequest("", 0, "", "")
    return PullRequest(
        repo=repo or base.repo,
        number=number if number is not None else base.number,
        head_sha=head_sha or base.head_sha,
        author=author or base.author,
    )


def check_pull_request(pr: PullRequest) -> None:
    missing = []
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", pr.repo):
        missing.append("the repository (--repo OWNER/NAME)")
    if pr.number <= 0:
        missing.append("the pull request number (--pr)")
    if not re.fullmatch(r"[0-9a-f]{7,64}", pr.head_sha):
        missing.append("the head commit (--head-sha)")
    if not pr.author:
        missing.append("the PR author (--author)")
    if missing:
        raise GateReviewError("the gate changes need a review, but the pull request is incomplete: " + ", ".join(missing) + " missing")


# --- reviews ---


def fetch_reviews(client: Client, pr: PullRequest) -> list[dict]:
    """Every review on the pull request, oldest first."""
    out: list[dict] = []
    page = 1
    while True:
        try:
            batch = client.request("GET", f"/repos/{pr.repo}/pulls/{pr.number}/reviews?per_page={PER_PAGE}&page={page}")
        except escalate.EscalationError as err:
            raise GateReviewError(str(err)) from err
        if not isinstance(batch, list):
            raise GateReviewError(f"GitHub API returned no review list for {pr.repo}#{pr.number}")
        out += [r for r in batch if isinstance(r, dict)]
        if len(batch) < PER_PAGE:
            return out
        page += 1


def latest_verdicts(reviews: list[dict]) -> dict[str, dict]:
    """login (lowercase) -> that reviewer's latest review with a verdict."""
    latest: dict[str, dict] = {}
    for r in reviews:
        login = ((r.get("user") or {}).get("login") or "").lower()
        if login and r.get("state") in VERDICTS:
            latest[login] = r
    return latest


def judge(reviews: list[dict], pr: PullRequest, agents: list[str]) -> tuple[list[str], list[dict]]:
    """Approvers that satisfy the check, and the approvals that don't, with why."""
    agent_set = {a.lower() for a in agents}
    approved, ignored = [], []
    for login, r in sorted(latest_verdicts(reviews).items()):
        if r.get("state") != "APPROVED":
            continue
        user = r.get("user") or {}
        name = user.get("login") or login
        if login == pr.author.lower():
            reason = "the PR author"
        elif login in agent_set:
            reason = "an agent account (agent_authors)"
        elif user.get("type") == "Bot":
            reason = "a bot account"
        elif r.get("commit_id") != pr.head_sha:
            reason = f"approved an older commit ({(r.get('commit_id') or 'unknown')[:12]})"
        else:
            approved.append(name)
            continue
        ignored.append({"login": name, "reason": reason})
    return approved, ignored


def review(results: Mapping[str, dict], pr: PullRequest | None, client_factory, agents: list[str]) -> Verdict:
    """Decide the check. client_factory is only called when there are gate changes to review."""
    changes = gate_changes(results)
    if not changes:
        return Verdict(True, "no gate changes; no review needed", pull_request=pr)
    if pr is None:
        raise GateReviewError(
            f"{len(changes)} gate change(s) need a review, but there is no pull request to read it from "
            "(run on a pull_request or pull_request_review event, or pass --repo, --pr, --head-sha and --author)"
        )
    check_pull_request(pr)
    approved, ignored = judge(fetch_reviews(client_factory(), pr), pr, agents)
    head = pr.head_sha[:12]
    if approved:
        detail = f"{len(changes)} gate change(s) approved on {head} by " + ", ".join(approved)
        return Verdict(True, detail, changes, approved, ignored, pr)
    detail = (
        f"{len(changes)} gate change(s) need an approving review on {head} "
        "from someone who is neither the PR author nor an agent account"
    )
    return Verdict(False, detail, changes, approved, ignored, pr)


def client_from_env(env: Mapping[str, str] | None = None) -> escalate.GitHub:
    env = os.environ if env is None else env
    token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")
    if not token:
        raise GateReviewError("set GH_TOKEN (or GITHUB_TOKEN) to a token that can read pull requests to check the reviews")
    return escalate.GitHub(token, env.get("GITHUB_API_URL") or escalate.DEFAULT_API)


# --- output ---


def summary(verdict: Verdict) -> str:
    """Markdown for $GITHUB_STEP_SUMMARY."""
    status = "passed" if verdict.ok else "**failed**"
    lines = ["## rlp gate review", "", f"Result: {status}. {verdict.detail}."]
    if verdict.gate_changes:
        lines += ["", "### Gate changes", ""]
        lines += [f"- {g.get('message', g)}" for g in verdict.gate_changes]
    if verdict.ignored:
        lines += ["", "### Approvals that don't count", ""]
        lines += [f"- @{i['login']}: {i['reason']}" for i in verdict.ignored]
    if verdict.gate_changes and not verdict.ok:
        advice = (
            "A human who is not the PR author reviews each gate change above and approves the pull request. "
            "The check runs again on the review; a new push needs a new approval."
        )
        lines += ["", advice]
    return "\n".join(lines) + "\n"
