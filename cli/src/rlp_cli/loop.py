"""The remediation loop runner: discover, act, verify, iterate.

`rlp loop <kind>` asks an agent (through rlp_cli.agent, never a vendor SDK) to
fix what a gate found, reruns the gate, and repeats until the gate passes or a
cap is hit:

1. **Discover**: the kind lists its findings. None means nothing to do.
2. **Act**: one agent call in a fresh context. The prompt is rebuilt every
   iteration from the current findings and gate output; no conversation
   carries over.
3. **Verify**: the kind's golden pipeline stages run (`rlp pipeline run -s`).
   An attempt that changed files outside the kind's allowed paths is thrown
   away before verification.
4. **Iterate** until the verify stages pass (`fixed`), or the iteration cap
   or the spend cap is reached (`escalated`).

Work happens on a branch `rlp/loop/<kind>/<run-id>`, one commit per accepted
attempt; the base branch is never committed to, and is checked out again when
the run ends. Every iteration is recorded in `.rlp/loops/<kind>-<run-id>.json`,
which validates against schemas/v1alpha1/loop-state.schema.json.

Loops are off by default. A repo opts in with `loops.enabled: true` in
rlp.yaml (optionally `loops.kinds`), and RLP_LOOPS=off in the environment
turns every loop off whatever rlp.yaml says.

Loop kinds register in KINDS. The concrete kinds (quality #48, vuln #49) are
separate issues; until one registers, running it reports that it is not
implemented yet.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from rlp_cli import agent, contract, pipeline, resources

STATE_DIR = Path(".rlp") / "loops"
TOGGLE_ENV = "RLP_LOOPS"
OFF_VALUES = ("off", "0", "false", "no", "disabled")
# Every kind rlp.yaml may name in loops.kinds; keep in step with repo.schema.json.
KNOWN_KINDS = ("quality", "vuln")
DEFAULT_MAX_ITERATIONS = 3

# Outcomes and their exit codes. disabled and no-findings are not failures.
EXIT_CODES = {"fixed": 0, "no-findings": 0, "disabled": 0, "dry-run": 0, "escalated": 1, "error": 2}


class LoopError(Exception):
    """The loop could not start or continue (setup, git, agent)."""


Finding = dict  # at least "message"; kinds add id, path, line, rule, severity


@dataclass
class LoopKind:
    """What a loop kind plugs into the runner."""

    name: str
    description: str
    discover: Callable[[Path, dict], list[Finding]]  # (repo root, rlp.yaml) -> current findings
    verify_stages: tuple[str, ...]  # golden pipeline stages that must pass
    allowed_paths: Callable[[Path, dict], list[str]]  # paths the agent may change, relative to the root
    build_prompt: Callable[[PromptInput], str] | None = None  # None: default_prompt


@dataclass
class PromptInput:
    """Everything a prompt is built from. Nothing else carries between iterations."""

    kind: str
    iteration: int
    max_iterations: int
    findings: list[Finding]
    gates: list[dict]  # the last verification's stage results; empty before the first
    allowed_paths: list[str]
    rejected: str = ""  # why the previous attempt was thrown away, if it was


KINDS: dict[str, LoopKind] = {}


def register(kind: LoopKind) -> LoopKind:
    unknown = [s for s in kind.verify_stages if s not in pipeline.STAGE_NAMES]
    if unknown:
        raise ValueError(f"loop kind {kind.name}: unknown verify stage(s) {', '.join(unknown)}")
    KINDS[kind.name] = kind
    return kind


def kind_names() -> list[str]:
    return sorted(set(KNOWN_KINDS) | set(KINDS))


# --- limits and the on/off toggle ---


def default_max_spend() -> float:
    """Default spend cap per run (USD), from policy/platform/data.yaml."""
    data = yaml.safe_load((resources.policy_dir() / "platform" / "data.yaml").read_text(encoding="utf-8"))
    return float(data["loop_max_spend_usd"])


@dataclass(frozen=True)
class Toggle:
    enabled: bool
    reason: str
    source: str  # rlp.yaml or RLP_LOOPS


def toggle(doc: dict, kind: str, env: Mapping[str, str] | None = None) -> Toggle:
    """Whether loop `kind` may run, and why. RLP_LOOPS=off wins over rlp.yaml."""
    env = os.environ if env is None else env
    switch = (env.get(TOGGLE_ENV) or "").strip().lower()
    if switch in OFF_VALUES:
        return Toggle(False, f"{TOGGLE_ENV}={env.get(TOGGLE_ENV)} turns every loop off", TOGGLE_ENV)
    loops = doc.get("loops") or {}
    if not loops:
        return Toggle(False, "rlp.yaml has no loops key; loops are off by default (set loops.enabled: true)", "rlp.yaml")
    if loops.get("enabled") is not True:
        return Toggle(False, "rlp.yaml sets loops.enabled: false", "rlp.yaml")
    kinds = loops.get("kinds")
    if kinds is not None and kind not in kinds:
        return Toggle(False, f"rlp.yaml loops.kinds does not include {kind}", "rlp.yaml")
    which = "loops.kinds includes it" if kinds is not None else "every kind"
    return Toggle(True, f"rlp.yaml sets loops.enabled: true ({which})", "rlp.yaml")


# --- state ---


@dataclass
class Iteration:
    number: int
    findings: list[Finding]
    agent: dict
    diff_stat: dict
    gates: list[dict]
    rejected: str
    commit: str
    spent_usd: float

    def as_dict(self) -> dict:
        return {
            "number": self.number,
            "findings": self.findings,
            "agent": self.agent,
            "diffStat": self.diff_stat,
            "gates": self.gates,
            "rejected": self.rejected,
            "commit": self.commit,
            "spentUsd": round(self.spent_usd, 6),
        }


@dataclass
class State:
    kind: str
    run_id: str
    branch: str
    base: str
    provider: str
    max_iterations: int
    max_spend_usd: float
    verify_stages: list[str]
    allowed_paths: list[str]
    started_at: str
    findings: list[Finding] = field(default_factory=list)
    iterations: list[Iteration] = field(default_factory=list)
    spent_usd: float = 0.0
    calls: int = 0
    outcome: str = "running"
    reason: str = ""
    finished_at: str = ""

    def as_dict(self) -> dict:
        return {
            "apiVersion": "rlp/v1alpha1",
            "kind": "LoopState",
            "loop": self.kind,
            "runId": self.run_id,
            "branch": self.branch,
            "base": self.base,
            "provider": self.provider,
            "limits": {"maxIterations": self.max_iterations, "maxSpendUsd": self.max_spend_usd},
            "verifyStages": self.verify_stages,
            "allowedPaths": self.allowed_paths,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at or None,
            "findings": self.findings,
            "iterations": [i.as_dict() for i in self.iterations],
            "spend": {"spentUsd": round(self.spent_usd, 6), "calls": self.calls},
            "outcome": self.outcome,
            "reason": self.reason,
        }


def state_path(root: Path, kind: str, run_id: str) -> Path:
    return root / STATE_DIR / f"{kind}-{run_id}.json"


def save(root: Path, state: State) -> Path:
    path = state_path(root, state.kind, state.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.as_dict(), indent=2) + "\n", encoding="utf-8")
    return path


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


# --- git ---

FALLBACK_IDENTITY = ("rlp loop", "rlp-loop@users.noreply.github.com")


def git(root: Path, *args: str, check: bool = True) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    except OSError as err:
        raise LoopError(f"could not run git: {err}") from err
    if check and proc.returncode != 0:
        raise LoopError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


def current_branch(root: Path) -> str:
    if git(root, "rev-parse", "--is-inside-work-tree", check=False) != "true":
        raise LoopError(f"{root} is not in a git work tree; loops work on a branch")
    branch = git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return branch or git(root, "rev-parse", "HEAD")


def ensure_clean(root: Path) -> None:
    dirty = git(root, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise LoopError("the work tree has uncommitted changes to tracked files; commit or stash them first")


def identity_args(root: Path) -> list[str]:
    if git(root, "config", "user.email", check=False):
        return []
    name, email = FALLBACK_IDENTITY
    return ["-c", f"user.name={name}", "-c", f"user.email={email}"]


def stage_and_stat(root: Path, files: Sequence[str]) -> dict:
    """Stage exactly the agent's changed files and measure the staged diff."""
    if not files:
        return {"files": 0, "insertions": 0, "deletions": 0}
    git(root, "add", "-A", "--", *files)
    insertions = deletions = count = 0
    for line in git(root, "diff", "--cached", "--numstat", "--", *files).splitlines():
        added, removed, _ = line.split("\t", 2)
        count += 1
        insertions += int(added) if added.isdigit() else 0
        deletions += int(removed) if removed.isdigit() else 0
    return {"files": count, "insertions": insertions, "deletions": deletions}


def commit(root: Path, message: str) -> str:
    if not git(root, "diff", "--cached", "--name-only"):
        return ""
    git(root, *identity_args(root), "commit", "--quiet", "--no-verify", "-m", message)
    return git(root, "rev-parse", "HEAD")


def discard(root: Path, files: Sequence[str]) -> None:
    """Throw away an attempt: restore tracked files from HEAD, delete new ones."""
    for rel in files:
        tracked = git(root, "ls-files", "--error-unmatch", "--", rel, check=False)
        if tracked:
            git(root, "checkout", "HEAD", "--", rel)
        else:
            (root / rel).unlink(missing_ok=True)


# --- prompts ---


def default_prompt(p: PromptInput) -> str:
    lines = [
        f"You are fixing findings from rlp's `{p.kind}` gate in this repository (attempt {p.iteration} of {p.max_iterations}).",
        "",
        "Rules:",
        f"- Change only files under: {', '.join(p.allowed_paths)}. Changes anywhere else are thrown away.",
        "- Fix the code. Never weaken a gate: no suppression comments, no lowered thresholds, no skipped or deleted tests, no exception files.",
        "- When you are done, say in one short paragraph what you changed.",
        "",
        "Findings:",
    ]
    for f in p.findings:
        where = f.get("path", "")
        if where and f.get("line"):
            where += f":{f['line']}"
        rule = f" [{f['rule']}]" if f.get("rule") else ""
        lines.append(f"- {where + ': ' if where else ''}{f.get('message', '')}{rule}")
    if p.rejected:
        lines += ["", f"The previous attempt was thrown away: {p.rejected}"]
    failed = [g for g in p.gates if g.get("status") == "failed"]
    if failed:
        lines += ["", "The gate still fails after the previous attempt:"]
        lines += [f"- {g['stage']}: {g.get('detail', '')}" for g in failed]
    return "\n".join(lines) + "\n"


# --- the runner ---


@dataclass
class Options:
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    max_spend_usd: float = 5.0
    dry_run: bool = False
    env: Mapping[str, str] | None = None
    provider: str | None = None
    on_event: Callable[[str], None] | None = None  # receives progress lines, for text output

    def event(self, message: str) -> None:
        if self.on_event is not None:
            self.on_event(message)

    def __post_init__(self) -> None:
        if self.max_iterations < 1:
            raise LoopError("--max-iterations must be at least 1")
        if self.max_spend_usd <= 0:
            raise LoopError("--max-spend must be more than $0")


@dataclass
class Report:
    """What `rlp loop <kind>` prints and how it exits."""

    outcome: str
    reason: str
    payload: dict
    state_file: str = ""

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.outcome]


def verify(root: Path, stages: Sequence[str], stream) -> list[dict]:
    ctx = pipeline.Context(root=root, stream=stream)
    results = pipeline.run(ctx, list(stages))
    ran = {r.stage: r for r in results}
    gates = []
    for name in [s for s in pipeline.STAGE_NAMES if s in stages]:
        r = ran.get(name)
        if r is None:
            gates.append({"stage": name, "status": "not-run", "detail": "an earlier verify stage failed"})
        else:
            gates.append({"stage": r.stage, "status": r.status, "detail": r.detail})
    return gates


def passed(gates: Sequence[dict]) -> bool:
    return bool(gates) and all(g["status"] in ("passed", "skipped") for g in gates)


def run(root: Path, kind_name: str, opts: Options, stream=None) -> Report:
    """Run loop `kind_name` on the repo at root. Never raises for an expected stop."""
    env = os.environ if opts.env is None else opts.env
    doc = contract.load_repo(root)
    if kind_name not in kind_names():
        raise LoopError(f"unknown loop kind {kind_name!r}; choose one of: {', '.join(kind_names())}")
    switch = toggle(doc, kind_name, env)
    if not switch.enabled:
        payload = {"ok": True, "loop": kind_name, "outcome": "disabled", "reason": switch.reason, "source": switch.source}
        return Report("disabled", switch.reason, payload)
    kind = KINDS.get(kind_name)
    if kind is None:
        raise LoopError(f"loop kind {kind_name} is not implemented yet (see docs/loops.md)")
    allowed = kind.allowed_paths(root, doc)
    findings = kind.discover(root, doc)
    build = kind.build_prompt or default_prompt

    if opts.dry_run:
        payload = {
            "ok": True,
            "loop": kind_name,
            "outcome": "dry-run",
            "reason": "dry run: no agent called, nothing changed",
            "findings": findings,
            "verifyStages": list(kind.verify_stages),
            "allowedPaths": allowed,
            "limits": {"maxIterations": opts.max_iterations, "maxSpendUsd": opts.max_spend_usd},
            "provider": agent.provider_name(opts.provider, env),
            "firstPrompt": build(PromptInput(kind_name, 1, opts.max_iterations, findings, [], allowed)) if findings else "",
        }
        return Report("dry-run", payload["reason"], payload)

    provider = agent.get_provider(opts.provider, env)
    provider.check()
    base = current_branch(root)
    run_id = new_run_id()
    state = State(
        kind=kind_name,
        run_id=run_id,
        branch=f"rlp/loop/{kind_name}/{run_id}",
        base=base,
        provider=provider.name,
        max_iterations=opts.max_iterations,
        max_spend_usd=opts.max_spend_usd,
        verify_stages=list(kind.verify_stages),
        allowed_paths=allowed,
        started_at=now(),
        findings=findings,
    )

    def finish(outcome: str, reason: str) -> Report:
        state.outcome, state.reason, state.finished_at = outcome, reason, now()
        path = save(root, state)
        opts.event(f"{outcome}: {reason}")
        return Report(outcome, reason, {"ok": outcome in ("fixed", "no-findings"), **state.as_dict()}, path.relative_to(root).as_posix())

    if not findings:
        state.branch = ""
        return finish("no-findings", f"the {kind_name} gate has nothing to fix")

    ensure_clean(root)
    git(root, "switch", "--quiet", "-c", state.branch)
    save(root, state)
    try:
        return iterate(root, kind, opts, state, provider, build, stream, finish)
    except (LoopError, agent.AgentError, contract.ContractError) as err:
        return finish("error", str(err))
    except Exception as err:  # anything else still ends the run with its state on disk
        return finish("error", f"{type(err).__name__}: {err}")
    finally:
        git(root, "switch", "--quiet", base, check=False)


def iterate(root, kind: LoopKind, opts: Options, state: State, provider, build, stream, finish) -> Report:
    spend = agent.Spend(opts.max_spend_usd)
    findings, gates, rejected = state.findings, [], ""
    for number in range(1, opts.max_iterations + 1):
        if spend.exhausted:
            return finish("escalated", f"spend cap of ${spend.cap_usd:.2f} reached after {number - 1} iteration(s)")
        budget = spend.budget(spend.remaining)
        prompt = build(PromptInput(kind.name, number, opts.max_iterations, findings, gates, state.allowed_paths, rejected))
        opts.event(f"iteration {number}: asking {provider.name} to fix {len(findings)} finding(s) (budget ${budget.max_usd:.2f})")
        result = provider.run(prompt, root, state.allowed_paths, budget)
        spend.charge(result)
        state.spent_usd, state.calls = spend.spent_usd, spend.calls
        record = Iteration(number, findings, result.as_dict(), {"files": 0, "insertions": 0, "deletions": 0}, [], "", "", spend.spent_usd)
        if result.outside_allowed:
            discard(root, result.changed_files)
            record.rejected = rejected = f"it changed files outside the allowed paths: {', '.join(result.outside_allowed)}"
            opts.event(f"iteration {number}: attempt thrown away ({record.rejected})")
        else:
            rejected = ""
            record.diff_stat = stage_and_stat(root, result.changed_files)
            record.commit = commit(root, f"fix({kind.name}): rlp loop iteration {number}\n\n{result.summary}")
            gates = record.gates = verify(root, kind.verify_stages, stream)
            opts.event(f"iteration {number}: " + ", ".join(f"{g['stage']} {g['status']}" for g in gates))
        state.iterations.append(record)
        save(root, state)
        if not record.rejected and passed(gates):
            return finish("fixed", f"{', '.join(kind.verify_stages)} passed after {number} iteration(s)")
        if not record.rejected:
            findings = kind.discover(root, contract.load_repo(root))
    if spend.exhausted:
        return finish("escalated", f"spend cap of ${spend.cap_usd:.2f} reached after {opts.max_iterations} iteration(s)")
    return finish("escalated", f"iteration cap of {opts.max_iterations} reached; the gate still fails")


def status(root: Path, env: Mapping[str, str] | None = None) -> list[dict]:
    """Whether each loop kind is on for the repo at root, and why."""
    doc = contract.load_repo(root)
    rows = []
    for name in kind_names():
        t = toggle(doc, name, env)
        rows.append({"loop": name, "enabled": t.enabled, "reason": t.reason, "source": t.source, "implemented": name in KINDS})
    return rows
