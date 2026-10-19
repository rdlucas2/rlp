"""Escalation issues for remediation loops: `rlp loop escalate <state-file>`.

When a loop gives up (outcome `escalated`, or `error`), a human takes over from
one GitHub issue that says what failed, what each attempt changed and the last
gate output, so nobody has to re-run anything. Everything in it comes from the
loop state file (.rlp/loops/<kind>-<run-id>.json, written by rlp_cli.loop).

There is one open escalation per loop kind and base branch. The issue body
carries a hidden marker naming both; a second escalation for the same pair
comments on the open issue instead of opening another.

Posting uses the GitHub REST API with a token from GH_TOKEN (else
GITHUB_TOKEN). In a loop workflow that is the agent identity's token
(docs/loops.md#agent-identity).
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from jsonschema import Draft202012Validator

from rlp_cli import resources

# Outcomes a human has to take over from. fixed and no-findings need nobody.
ESCALATABLE = ("escalated", "error")
BASE_LABELS = ("needs:human", "area:agent")
GATE_OUTPUT_LIMIT = 4000  # characters of the last failing gate's output kept in the issue
SUMMARY_LIMIT = 1000  # characters of each agent summary
FINDINGS_LIMIT = 20  # findings listed per iteration
DEFAULT_API = "https://api.github.com"
MARKER = "<!-- rlp-loop-escalation kind={kind} base={base} -->"
MARKER_RE = re.compile(r"<!-- rlp-loop-escalation kind=(\S+) base=(\S+) -->")


class EscalationError(Exception):
    """The state file can't be escalated, or the issue couldn't be posted."""


# --- reading the state file ---


def load_state(path: Path) -> dict:
    """Read a loop state file, validated against the LoopState schema."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise EscalationError(f"{path}: no such state file") from err
    except (OSError, ValueError) as err:
        raise EscalationError(f"{path}: not a readable JSON state file: {err}") from err
    errors = sorted(Draft202012Validator(resources.schema("LoopState")).iter_errors(doc), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.absolute_path) or "(root)"
        raise EscalationError(f"{path}: not a loop state file: {where}: {first.message}")
    if doc["outcome"] not in ESCALATABLE:
        raise EscalationError(f"{path}: outcome is {doc['outcome']}; only {' or '.join(ESCALATABLE)} runs are escalated")
    return doc


def trigger_url(env: Mapping[str, str] | None = None) -> str:
    """The GitHub Actions run that started the loop, from the runner's environment."""
    env = os.environ if env is None else env
    server, repo, run_id = env.get("GITHUB_SERVER_URL"), env.get("GITHUB_REPOSITORY"), env.get("GITHUB_RUN_ID")
    if server and repo and run_id:
        return f"{server.rstrip('/')}/{repo}/actions/runs/{run_id}"
    return ""


# --- rendering ---


def labels(state: dict) -> list[str]:
    return [*BASE_LABELS, f"loop:{state['loop']}"]


def title(state: dict) -> str:
    return f"rlp loop {state['loop']} escalated on {state['base']}"


def marker(state: dict) -> str:
    return MARKER.format(kind=state["loop"], base=state["base"])


def _cell(text: object) -> str:
    """Text safe for one Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _code(text: str) -> str:
    """A fenced block that the text itself can't close."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    body = "\n".join(line.rstrip() for line in text.rstrip().splitlines())
    return f"{fence}text\n{body}\n{fence}"


def _truncate(text: str, limit: int, keep: str = "head") -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = len(text) - limit
    if keep == "tail":
        return f"[... {cut} characters cut ...]\n{text[-limit:]}"
    return f"{text[:limit]}\n[... {cut} characters cut ...]"


def _first_line(text: str, limit: int = 160) -> str:
    """A gate detail's first line, for a table cell; the full output has its own section."""
    lines = text.strip().splitlines()
    first = lines[0] if lines else ""
    more = len(lines) > 1 or len(first) > limit
    return (first[:limit] + " ...") if more else first


def _finding(f: dict) -> str:
    where = f.get("path", "")
    if where and f.get("line"):
        where += f":{f['line']}"
    rule = f" [{f['rule']}]" if f.get("rule") else ""
    lead = f"`{where}`: " if where else ""
    return f"- {lead}{_cell(f.get('message', ''))}{rule}"


def _findings(findings: list[dict]) -> list[str]:
    if not findings:
        return ["None."]
    lines = [_finding(f) for f in findings[:FINDINGS_LIMIT]]
    if len(findings) > FINDINGS_LIMIT:
        lines.append(f"- ... and {len(findings) - FINDINGS_LIMIT} more (see the state file)")
    return lines


def last_failing_gate(state: dict) -> tuple[int, dict] | None:
    """The newest gate result that failed, with its iteration number."""
    for it in reversed(state["iterations"]):
        for gate in reversed(it["gates"]):
            if gate["status"] == "failed":
                return it["number"], gate
    return None


def _iteration(it: dict) -> list[str]:
    agent = it["agent"]
    stat = it["diffStat"]
    out = [
        f"### Iteration {it['number']}",
        "",
        f"**Findings in the prompt ({len(it['findings'])}).**",
        "",
        *_findings(it["findings"]),
        "",
        f"**Agent.** {'finished' if agent['ok'] else 'did not finish'}"
        f"{', over budget' if agent['overBudget'] else ''}; "
        f"${agent['costUsd']:.2f}, {agent['tokens']['input']} input and {agent['tokens']['output']} output tokens.",
        "",
    ]
    summary = _truncate(agent["summary"], SUMMARY_LIMIT)
    if summary:
        out += [_code(summary), ""]
    changed = agent["changedFiles"]
    out += [f"**Changed files ({len(changed)}).** " + (", ".join(f"`{c}`" for c in changed) if changed else "none"), ""]
    if it["rejected"]:
        out += [f"**Thrown away:** {_cell(it['rejected'])}", ""]
    else:
        commit = f"`{it['commit'][:12]}`" if it["commit"] else "nothing committed"
        out += [f"**Diff.** {stat['files']} file(s), +{stat['insertions']} -{stat['deletions']}; {commit}.", ""]
    if it["gates"]:
        out += ["| Gate | Result | Detail |", "| --- | --- | --- |"]
        out += [f"| {_cell(g['stage'])} | {g['status']} | {_cell(_first_line(g['detail']))} |" for g in it["gates"]]
        out.append("")
    out.append(f"Spend so far: ${it['spentUsd']:.2f}.")
    return out


def render(state: dict, trigger: str = "", state_file: str = "") -> str:
    """The escalation issue body, as Markdown."""
    limits, spend = state["limits"], state["spend"]
    rows = [
        ("Loop", f"`{state['loop']}`"),
        ("Outcome", f"{state['outcome']}: {state['reason']}"),
        ("Base", f"`{state['base']}`"),
        ("Loop branch", f"`{state['branch']}`" if state["branch"] else "none"),
        ("Trigger", trigger or "not recorded (not run in GitHub Actions)"),
        ("Provider", f"`{state['provider']}`"),
        ("Run", f"`{state['runId']}`, {state['startedAt']} to {state['finishedAt'] or 'unfinished'}"),
        ("Iterations", f"{len(state['iterations'])} of {limits['maxIterations']}"),
        ("Spend", f"${spend['spentUsd']:.2f} of ${limits['maxSpendUsd']:.2f} over {spend['calls']} agent call(s)"),
        ("Verify stages", ", ".join(f"`{s}`" for s in state["verifyStages"]) or "none"),
        ("Allowed paths", ", ".join(f"`{p}`" for p in state["allowedPaths"]) or "none"),
    ]
    if state_file:
        rows.append(("State file", f"`{state_file}`"))
    out = [
        marker(state),
        "",
        f"The `{state['loop']}` remediation loop gave up on `{state['base']}` and needs a human. "
        "Everything below comes from the loop's state file; nothing needs re-running to read it.",
        "",
        "| | |",
        "| --- | --- |",
        *[f"| {k} | {_cell(v)} |" for k, v in rows],
        "",
        "## Last failing gate",
        "",
    ]
    failing = last_failing_gate(state)
    if failing is None:
        out.append("No gate failed in a kept attempt; see the outcome above and each iteration below.")
    else:
        number, gate = failing
        out += [f"`{gate['stage']}`, iteration {number}:", "", _code(_truncate(gate["detail"], GATE_OUTPUT_LIMIT, keep="tail"))]
    out += ["", f"## Findings at the start ({len(state['findings'])})", "", *_findings(state["findings"]), "", "## Iterations", ""]
    if not state["iterations"]:
        out.append("None: the loop stopped before calling the agent.")
    for i, it in enumerate(state["iterations"]):
        if i:
            out.append("")
        out += _iteration(it)
    out += [
        "",
        "## Taking over",
        "",
        f"Check out `{state['branch'] or state['base']}`, fix what the last failing gate reports, "
        "and run the verify stages with `rlp pipeline run -s <stage>`. "
        "Close this issue when the gate passes on the base branch.",
    ]
    return "\n".join(out) + "\n"


def render_comment(state: dict, trigger: str = "", state_file: str = "") -> str:
    """A repeat escalation, as a comment on the open issue."""
    body = render(state, trigger, state_file).split("\n", 2)[2]  # drop the marker line
    return f"## Escalated again: run `{state['runId']}`\n\n{body}"


# --- posting ---


class Client(Protocol):
    def request(self, method: str, path: str, body: dict | None = None) -> object: ...


class GitHub:
    """A minimal GitHub REST client: one JSON request per call, no retries."""

    def __init__(self, token: str, api_url: str = DEFAULT_API, timeout: float = 30) -> None:
        self.token, self.api_url, self.timeout = token, api_url.rstrip("/"), timeout

    def request(self, method: str, path: str, body: dict | None = None) -> object:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.api_url + path, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "rlp-cli")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", "replace")[:500]
            raise EscalationError(f"GitHub API {method} {path}: HTTP {err.code}: {detail}") from err
        except (urllib.error.URLError, OSError) as err:
            raise EscalationError(f"GitHub API {method} {path}: {err}") from err
        return json.loads(raw) if raw else None


def client_from_env(env: Mapping[str, str] | None = None) -> GitHub:
    env = os.environ if env is None else env
    token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")
    if not token:
        raise EscalationError("set GH_TOKEN (or GITHUB_TOKEN) to a token that can write issues to post an escalation")
    return GitHub(token, env.get("GITHUB_API_URL") or DEFAULT_API)


def repo_from_env(repo: str | None, env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    chosen = repo or env.get("GITHUB_REPOSITORY") or ""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", chosen):
        raise EscalationError("name the repository with --repo OWNER/NAME (or set GITHUB_REPOSITORY)")
    return chosen


@dataclass
class Posted:
    action: str  # opened or commented
    number: int
    url: str

    def as_dict(self) -> dict:
        return {"action": self.action, "number": self.number, "url": self.url}


def find_open(client: Client, repo: str, state: dict) -> dict | None:
    """The open escalation for this loop kind and base branch, if there is one."""
    query = urllib.parse.urlencode({"state": "open", "labels": ",".join(labels(state)), "per_page": "100"})
    issues = client.request("GET", f"/repos/{repo}/issues?{query}") or []
    want = (state["loop"], state["base"])
    for issue in issues:
        if "pull_request" in issue:
            continue
        found = MARKER_RE.search(issue.get("body") or "")
        if found and found.groups() == want:
            return issue
    return None


def post(client: Client, repo: str, state: dict, trigger: str = "", state_file: str = "") -> Posted:
    """Open the escalation issue, or comment on the open one for the same kind and base."""
    existing = find_open(client, repo, state)
    if existing is not None:
        number = existing["number"]
        comment = client.request("POST", f"/repos/{repo}/issues/{number}/comments", {"body": render_comment(state, trigger, state_file)})
        return Posted("commented", number, comment.get("html_url", existing.get("html_url", "")))
    issue = client.request("POST", f"/repos/{repo}/issues", {"title": title(state), "body": render(state, trigger, state_file), "labels": labels(state)})
    return Posted("opened", issue["number"], issue.get("html_url", ""))
