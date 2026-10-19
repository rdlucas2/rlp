"""Loop telemetry for the job summary: `rlp loop summary <state-file>`.

Renders one loop run's state file (.rlp/loops/<kind>-<run-id>.json) as a
Markdown table, one row per iteration: findings before and after, gate
results, cost and duration; then the outcome and the total spend. Loop
workflows append it to $GITHUB_STEP_SUMMARY and upload the state file as an
artifact, so every run's cost and result are visible without opening the JSON.

The total spend is the sum of the iterations' agent costs, the same numbers
the table shows. State files written before rlp recorded findingsAfter and
durationSeconds still render: findings after come from the next iteration, or
zero for the last one of a fixed run, and an unknown value shows as a dash.
"""

from __future__ import annotations

from rlp_cli import escalate

UNKNOWN = "-"


def total_spend(state: dict) -> float:
    """What the run spent: the sum of every iteration's agent cost."""
    return sum(it["agent"]["costUsd"] for it in state["iterations"])


def usd(amount: float) -> str:
    return f"${amount:.4f}"


def duration(seconds: float | None) -> str:
    if seconds is None:
        return UNKNOWN
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(round(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m {rest:02d}s" if hours else f"{minutes}m {rest:02d}s"


def findings_after(state: dict, index: int) -> str:
    """How many findings were left after iteration `index` (0-based)."""
    iterations = state["iterations"]
    it = iterations[index]
    if "findingsAfter" in it:
        return str(it["findingsAfter"])
    if index + 1 < len(iterations):
        return str(len(iterations[index + 1]["findings"]))
    if state["outcome"] == "fixed":
        return "0"
    return UNKNOWN


def gates(it: dict) -> str:
    if it["rejected"]:
        return f"thrown away: {escalate._cell(it['rejected'])}"
    if not it["gates"]:
        return "not run"
    return ", ".join(f"{escalate._cell(g['stage'])} {g['status']}" for g in it["gates"])


def render(state: dict) -> str:
    """The job summary for one loop run, as Markdown."""
    limits = state["limits"]
    total = total_spend(state)
    branch = f", loop branch `{state['branch']}`" if state["branch"] else ""
    run = f"`{state['runId']}`, {state['startedAt']} to {state['finishedAt'] or 'unfinished'}"
    out = [
        f"## rlp loop {state['loop']}: {state['outcome']}",
        "",
        f"Base `{state['base']}`{branch}, provider `{state['provider']}`, run {run}.",
        "",
    ]
    if state["iterations"]:
        out += [
            "| Iteration | Findings before | Findings after | Gates | Cost | Duration |",
            "| ---: | ---: | ---: | --- | ---: | ---: |",
        ]
        for n, it in enumerate(state["iterations"]):
            cells = [str(it["number"]), str(len(it["findings"])), findings_after(state, n), gates(it),
                     usd(it["agent"]["costUsd"]), duration(it.get("durationSeconds"))]
            out.append("| " + " | ".join(cells) + " |")
    else:
        out.append(f"No iterations: discovery found {len(state['findings'])} finding(s) and no agent was called.")
    count = f"{len(state['iterations'])} of {limits['maxIterations']} iteration(s)"
    out += [
        "",
        f"**Outcome:** {state['outcome']}: {escalate._cell(state['reason']) or 'no reason recorded'}",
        "",
        f"**Total spend:** {usd(total)} of {usd(limits['maxSpendUsd'])} cap, over {count}.",
    ]
    return "\n".join(out) + "\n"
