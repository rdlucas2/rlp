"""Lifecycle: move a project between phases when the entry rules allow it.

A project moves through proposed, designed, scaffolded, in-dev, live,
deprecated and retired. Which moves are allowed and what each phase needs are
Rego rules in the rlp.lifecycle package of the policy bundle (policy/lifecycle/
at the rlp repo root); this package ships a copy in policy/lifecycle/ and runs
it with the pinned `opa` binary, as `rlp policy check` runs the bundle. Code
here only gathers the input and records the outcome.

`transition()` builds the input from the project record, evaluates the rules
and, when every rule passes, sets the phase and records one lifecycle
transition with the rule results. When a rule fails it writes nothing and
returns the failing rules. Like the store, it flushes but never commits: the
caller owns the transaction. See docs/lifecycle.md.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from rlp_platform import opa
from rlp_platform.models import LifecycleTransition, Phase, Project
from rlp_platform.store import builds, projects, repos, transitions

POLICY_DIR = Path(__file__).resolve().parent / "policy" / "lifecycle"
# The input path the rule messages name, as conftest would for a file.
INPUT_PATH = "rlp-transition.json"


class LifecycleError(RuntimeError):
    """The rules could not be evaluated: OPA is missing, failed, or answered something unexpected."""


@dataclass(frozen=True)
class RuleResult:
    """One entry rule's verdict on a move."""

    rule: str
    description: str
    passed: bool
    messages: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "description": self.description, "passed": self.passed, "messages": self.messages}


@dataclass(frozen=True)
class Outcome:
    """What a requested move came to. `transition` is the recorded row when it was allowed."""

    project: str
    from_phase: Phase
    to_phase: str
    results: list[RuleResult]
    transition: LifecycleTransition | None = None

    @property
    def allowed(self) -> bool:
        return self.transition is not None

    @property
    def failures(self) -> list[RuleResult]:
        return [r for r in self.results if not r.passed]


def build_input(session: Session, project: Project, to: Phase | str) -> dict[str, Any]:
    """The rlp_transition document the rules read: the project, its repos and their builds."""
    repo_docs = []
    for repo in repos.list_(session, project_id=project.id):
        history = builds.list_(session, repo_id=repo.id)  # newest first
        latest = next((b for b in history if b.branch == repo.default_branch), None)
        repo_docs.append(
            {
                "full_name": repo.full_name,
                "default_branch": repo.default_branch,
                "builds": len(history),
                "latest_default_branch_build": None
                if latest is None
                else {"commit": latest.commit, "passed": latest.passed},
            }
        )
    return {
        "project": {
            "name": project.name,
            "description": project.description,
            "owner": project.owner,
            "dashboard_url": project.dashboard_url,
        },
        "from": Phase(project.phase).value,
        "to": str(to.value if isinstance(to, Phase) else to),
        "repos": repo_docs,
        # The retirement workflow (#96) records the teardown; until it does,
        # nothing has been torn down and RLP-L108 holds every move to retired.
        "teardown": None,
    }


def evaluate(document: dict[str, Any], *, opa_path: str | None = None) -> list[RuleResult]:
    """Run the rlp.lifecycle rules on an rlp_transition document and return every rule's result, in rule order.

    `opa_path` is the opa binary to run; the default is the one on PATH (the
    image installs it at /usr/local/bin/opa). The argument list is fixed and the
    document goes in on stdin, so nothing from the record reaches the command line.
    """
    # Absolute, because opa runs in the policy directory.
    exe = str(Path(opa_path or _find_opa()).resolve())
    payload = json.dumps([{"path": INPUT_PATH, "contents": {"rlp_transition": document}}])
    try:
        proc = subprocess.run(
            [
                "/usr/local/bin/opa",
                "eval",
                "--format",
                "json",
                "--data",
                ".",
                "--stdin-input",
                'x := {"failures": data.rlp.lifecycle.failures, "results": data.rlp.lifecycle.results}',
            ],
            executable=exe,
            cwd=POLICY_DIR,
            input=payload,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise LifecycleError(f"could not run opa: {err}") from err
    if proc.returncode != 0:
        raise LifecycleError(f"opa eval failed: {(proc.stderr or proc.stdout).strip()}")
    try:
        rules = json.loads(proc.stdout)["result"][0]["bindings"]["x"]["results"][INPUT_PATH]
        return [
            RuleResult(rule=r["rule"], description=r["description"], passed=r["passed"], messages=list(r["messages"]))
            for r in rules
        ]
    except (ValueError, KeyError, IndexError, TypeError) as err:
        raise LifecycleError(f"opa answered something unexpected: {proc.stdout[:500]}") from err


def transition(
    session: Session,
    project: Project,
    to: Phase | str,
    actor: str,
    reason: str = "",
    *,
    opa_path: str | None = None,
) -> Outcome:
    """Move `project` to `to` if every entry rule passes; record the transition with the rule results."""
    from_phase = Phase(project.phase)
    results = evaluate(build_input(session, project, to), opa_path=opa_path)
    target = str(to.value if isinstance(to, Phase) else to)
    if not results or any(not r.passed for r in results):
        return Outcome(project=project.name, from_phase=from_phase, to_phase=target, results=results)
    to_phase = Phase(target)
    projects.update(session, project, phase=to_phase)
    record = transitions.create(
        session,
        project_id=project.id,
        from_phase=from_phase,
        to_phase=to_phase,
        actor=actor,
        reason=reason,
        rule_results=[r.as_dict() for r in results],
    )
    return Outcome(project=project.name, from_phase=from_phase, to_phase=target, results=results, transition=record)


def _find_opa() -> str:
    try:
        return opa.find()
    except opa.OpaError as err:
        raise LifecycleError(str(err)) from err
