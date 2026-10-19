"""The pull request a fixed loop run opens: `rlp loop pr <state-file>`.

A loop run that ends `fixed` has passed every verify stage on its loop branch.
This renders the PR for that branch from the run's state file (what was found,
what each iteration changed, the gate results) and, with --post, opens it
against the base branch and labels it `agent:authored` and `loop:<kind>`.

Only a `fixed` run can open a PR: any other outcome, or a state file whose
last kept iteration has a verify stage that did not pass, is refused, so no PR
is opened while a gate is red. Pushing the loop branch is the workflow's job
(as the agent identity, docs/loops.md#agent-identity); posting uses the same
GitHub REST client and token variables as `rlp loop escalate`.
"""

from __future__ import annotations

from pathlib import Path

from rlp_cli import escalate

LABEL = "agent:authored"
GREEN = ("passed", "skipped")
REVIEW = (
    "This PR was written by an agent. The policy stage flags any change it makes to tests, quality config, "
    "suppressions or exception files (the `rlp.agent` rules), and a human reviews each one before merge."
)


class PullRequestError(Exception):
    """The state file can't open a PR, or the PR couldn't be posted."""


def load_fixed(path: Path) -> dict:
    """Read a loop state file whose run is fixed and green on its last kept iteration."""
    try:
        state = escalate.read_state(path)
    except escalate.EscalationError as err:
        raise PullRequestError(str(err)) from err
    if state["outcome"] != "fixed":
        raise PullRequestError(f"{path}: outcome is {state['outcome']}; only a fixed run opens a pull request")
    if not state["branch"]:
        raise PullRequestError(f"{path}: the run has no loop branch")
    kept = [it for it in state["iterations"] if not it["rejected"]]
    gates = kept[-1]["gates"] if kept else []
    red = [g["stage"] for g in gates if g["status"] not in GREEN]
    missing = [s for s in state["verifyStages"] if s not in {g["stage"] for g in gates}]
    if not gates or red or missing:
        why = ", ".join(red + [f"{s} (not run)" for s in missing]) or "no gate results"
        raise PullRequestError(f"{path}: the verify stages are not all green on the last commit: {why}")
    return state


def labels(state: dict) -> list[str]:
    return [LABEL, f"loop:{state['loop']}"]


def title(state: dict) -> str:
    n = len(state["findings"])
    return f"fix({state['loop']}): rlp loop fixes {n} finding{'' if n == 1 else 's'} on {state['base']}"


def render(state: dict, trigger: str = "") -> str:
    """The PR body, as Markdown."""
    limits, spend = state["limits"], state["spend"]
    kept = [it for it in state["iterations"] if not it["rejected"]]
    last = kept[-1]
    rows = [
        ("Loop", f"`{state['loop']}`"),
        ("Base", f"`{state['base']}`"),
        ("Loop branch", f"`{state['branch']}`"),
        ("Trigger", trigger or "not recorded (not run in GitHub Actions)"),
        ("Provider", f"`{state['provider']}`"),
        ("Run", f"`{state['runId']}`, {state['startedAt']} to {state['finishedAt'] or 'unfinished'}"),
        ("Iterations", f"{len(state['iterations'])} of {limits['maxIterations']}"),
        ("Spend", f"${spend['spentUsd']:.2f} of ${limits['maxSpendUsd']:.2f} over {spend['calls']} agent call(s)"),
        ("Allowed paths", ", ".join(f"`{p}`" for p in state["allowedPaths"]) or "none"),
    ]
    stages = ", ".join(f"`{s}`" for s in state["verifyStages"])
    intro = (
        f"The `{state['loop']}` remediation loop fixed what the gates found on `{state['base']}`. "
        f"Every verify stage ({stages}) passed on the loop branch's last commit."
    )
    out = [
        intro,
        "",
        "| | |",
        "| --- | --- |",
        *[f"| {k} | {escalate._cell(v)} |" for k, v in rows],
        "",
        f"## Findings fixed ({len(state['findings'])})",
        "",
        *escalate._findings(state["findings"]),
        "",
        "## Gates on the last commit",
        "",
        "| Gate | Result | Detail |",
        "| --- | --- | --- |",
        *[f"| {escalate._cell(g['stage'])} | {g['status']} | {escalate._cell(escalate._first_line(g['detail']))} |" for g in last["gates"]],
        "",
        "## Iterations",
        "",
    ]
    for it in state["iterations"]:
        agent = it["agent"]
        if it["rejected"]:
            what = f"thrown away: {escalate._cell(it['rejected'])}"
        else:
            stat = it["diffStat"]
            commit = f"`{it['commit'][:12]}`" if it["commit"] else "nothing committed"
            what = f"{stat['files']} file(s), +{stat['insertions']} -{stat['deletions']}; {commit}"
        out.append(f"- **{it['number']}.** ${agent['costUsd']:.2f}; {what}.")
        summary = escalate._truncate(agent["summary"], escalate.SUMMARY_LIMIT)
        if summary:
            out += ["", *("  " + line for line in escalate._code(summary).splitlines())]
    out += [
        "",
        "## Review",
        "",
        REVIEW,
    ]
    return "\n".join(out) + "\n"


def post(client: escalate.Client, repo: str, state: dict, trigger: str = "") -> escalate.Posted:
    """Open the PR from the loop branch into its base, then label it."""
    try:
        pr = client.request("POST", f"/repos/{repo}/pulls", {
            "title": title(state),
            "head": state["branch"],
            "base": state["base"],
            "body": render(state, trigger),
        })
        client.request("POST", f"/repos/{repo}/issues/{pr['number']}/labels", {"labels": labels(state)})
    except escalate.EscalationError as err:
        raise PullRequestError(str(err)) from err
    return escalate.Posted("opened", pr["number"], pr.get("html_url", ""))
