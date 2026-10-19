"""Loop telemetry for the job summary (#53): rlp loop summary.

The rendered Markdown is pinned by snapshots in cli/tests/snapshots/. After a
deliberate change to the format, regenerate them with
RLP_UPDATE_SNAPSHOTS=1 pytest cli/tests/test_loop_summary.py and review the diff.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from rlp_cli import escalate, loop_summary

SNAPSHOTS = Path(__file__).parent / "snapshots"
STATE = SNAPSHOTS / "loop-summary-state.json"
LEGACY = SNAPSHOTS / "escalation-state.json"  # written before findingsAfter and durationSeconds


def snapshot(name: str, actual: str) -> None:
    path = SNAPSHOTS / name
    if os.environ.get("RLP_UPDATE_SNAPSHOTS") == "1":
        path.write_text(actual, encoding="utf-8", newline="\n")
    assert path.is_file(), f"no snapshot {name}; run with RLP_UPDATE_SNAPSHOTS=1"
    assert actual == path.read_text(encoding="utf-8")


def load(path: Path) -> dict:
    return escalate.read_state(path)


def rows(body: str) -> list[list[str]]:
    """The iteration table's data rows, as cells."""
    lines = [line for line in body.splitlines() if re.match(r"^\| \d+ \|", line)]
    return [[c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]] for line in lines]


def test_summary_snapshot():
    snapshot("loop-summary.md", loop_summary.render(load(STATE)))


def test_summary_of_a_state_file_without_telemetry_fields_snapshot():
    snapshot("loop-summary-legacy.md", loop_summary.render(load(LEGACY)))


@pytest.mark.parametrize("path", [STATE, LEGACY])
def test_total_spend_is_the_sum_of_the_iteration_costs(path):
    state = load(path)
    body = loop_summary.render(state)
    costs = [float(r[4].lstrip("$")) for r in rows(body)]
    assert costs == [it["agent"]["costUsd"] for it in state["iterations"]]
    total = re.search(r"\*\*Total spend:\*\* \$([0-9.]+)", body)
    assert total and float(total.group(1)) == pytest.approx(sum(costs))
    assert loop_summary.total_spend(state) == pytest.approx(sum(it["agent"]["costUsd"] for it in state["iterations"]))


def test_one_row_per_iteration_with_findings_before_and_after():
    state = load(STATE)
    table = rows(loop_summary.render(state))
    assert [r[0] for r in table] == ["1", "2", "3"]
    assert [r[1] for r in table] == [str(len(it["findings"])) for it in state["iterations"]]
    assert [r[2] for r in table] == ["1", "1", "1"]
    assert "thrown away" in table[1][3] and "lint passed, test failed" == table[0][3]
    assert [r[5] for r in table] == ["3m 32s", "48.0s", "4m 05s"]


@pytest.mark.parametrize(("seconds", "shown"), [(None, "-"), (0.04, "0.0s"), (59.94, "59.9s"), (60, "1m 00s"), (3725, "1h 02m 05s")])
def test_duration_format(seconds, shown):
    assert loop_summary.duration(seconds) == shown


def test_findings_after_falls_back_to_the_next_iteration_or_a_fixed_outcome():
    state = load(LEGACY)
    assert [r[2] for r in rows(loop_summary.render(state))] == ["1", "1", "-"]
    state["outcome"] = "fixed"
    assert rows(loop_summary.render(state))[-1][2] == "0"
    assert rows(loop_summary.render(state))[-1][5] == "-"


def test_a_run_with_no_iterations_says_so():
    state = load(STATE)
    state["iterations"], state["outcome"], state["reason"] = [], "escalated", "needs a major-version bump"
    body = loop_summary.render(state)
    assert "No iterations: discovery found 3 finding(s)" in body
    assert "**Outcome:** escalated: needs a major-version bump" in body
    assert "**Total spend:** $0.0000 of $5.0000 cap, over 0 of 3 iteration(s)." in body


def test_cli_prints_the_summary(rlp):
    result = rlp("loop", "summary", str(STATE))
    assert result.code == 0, result.stderr
    assert result.stdout == loop_summary.render(load(STATE))


def test_cli_json_carries_the_total(rlp):
    result = rlp("loop", "summary", str(STATE), "--output", "json")
    assert result.code == 0, result.stderr
    doc = json.loads(result.stdout)
    assert doc["ok"] is True and doc["iterations"] == 3 and doc["outcome"] == "escalated"
    assert doc["totalSpendUsd"] == pytest.approx(0.42 + 0.31 + 1.1)
    assert doc["body"].startswith("## rlp loop quality: escalated")


def test_cli_refuses_a_file_that_is_not_loop_state(rlp, tmp_path):
    bad = tmp_path / "s.json"
    bad.write_text(json.dumps({"kind": "Repo"}), encoding="utf-8")
    result = rlp("loop", "summary", str(bad))
    assert result.code == 2 and "not a loop state file" in result.stderr
