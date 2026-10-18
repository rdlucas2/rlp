"""Agent providers: how a remediation loop asks an agent to change a working tree.

Loop code depends on this module's interface only: get_provider() returns an
AgentProvider, and AgentProvider.run() takes a prompt, the working tree, the
paths the agent may change and a per-call budget, and returns an AgentResult.
Each run() is one fresh agent context; nothing carries over between calls.

Providers:

- claude-code: runs the `claude` CLI headless (`claude -p`) in the working tree.
- fake: applies a scripted patch per call and reports a fixed cost. For tests.

The provider is chosen by --provider, else RLP_AGENT_PROVIDER, else claude-code.
Credentials come only from the environment, never from files in the repo.

Every provider reports the files that changed, measured on disk before and
after the call, and flags any change outside allowed_paths. The cost a provider
reports is what Spend, the loop runner's spend cap, counts.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol, runtime_checkable

PROVIDER_ENV = "RLP_AGENT_PROVIDER"
DEFAULT_PROVIDER = "claude-code"


class AgentError(Exception):
    """The provider could not run the agent."""


class CredentialsError(AgentError):
    """The provider has no credentials in the environment."""


@dataclass(frozen=True)
class Budget:
    """What one call may spend, in USD."""

    max_usd: float

    def __post_init__(self) -> None:
        if self.max_usd <= 0:
            raise ValueError("budget must be more than $0")


@dataclass
class AgentResult:
    """What one agent call did and cost."""

    ok: bool  # the agent finished without an error and within budget
    summary: str  # the agent's own account of what it did, or why it stopped
    changed_files: list[str] = field(default_factory=list)  # POSIX paths relative to workdir, sorted
    outside_allowed: list[str] = field(default_factory=list)  # changed files not under allowed_paths
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    over_budget: bool = False
    provider: str = ""

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "ok": self.ok,
            "summary": self.summary,
            "changedFiles": self.changed_files,
            "outsideAllowed": self.outside_allowed,
            "tokens": {"input": self.input_tokens, "output": self.output_tokens},
            "costUsd": round(self.cost_usd, 6),
            "overBudget": self.over_budget,
        }


@runtime_checkable
class AgentProvider(Protocol):
    """The interface every provider implements."""

    name: str

    def check(self) -> None:
        """Raise CredentialsError (or AgentError) if the provider can't run here."""

    def run(self, prompt: str, workdir: Path, allowed_paths: Sequence[str], budget: Budget) -> AgentResult:
        """Run the agent once, in a fresh context, on workdir."""


# --- spend cap ---


@dataclass
class Spend:
    """A loop's spend cap. It counts exactly the cost each provider reports."""

    cap_usd: float
    spent_usd: float = 0.0
    calls: int = 0

    @property
    def remaining(self) -> float:
        return max(self.cap_usd - self.spent_usd, 0.0)

    @property
    def exhausted(self) -> bool:
        return self.spent_usd >= self.cap_usd

    def charge(self, result: AgentResult) -> None:
        self.spent_usd += result.cost_usd
        self.calls += 1

    def budget(self, per_call_usd: float) -> Budget:
        """The budget for the next call: the per-call limit, capped by what's left."""
        if self.exhausted:
            raise AgentError(f"spend cap of ${self.cap_usd:.2f} reached after {self.calls} call(s)")
        return Budget(min(per_call_usd, self.remaining))


# --- working-tree changes ---


def snapshot(workdir: Path) -> dict[str, str]:
    """Content hash of every file under workdir, keyed by POSIX relative path.

    Inside a git work tree, only tracked and untracked-but-not-ignored files
    count; elsewhere every file except those under .git does.
    """
    paths = _git_files(workdir)
    if paths is None:
        paths = [
            p.relative_to(workdir).as_posix()
            for p in workdir.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(workdir).parts
        ]
    hashes = {}
    for rel in paths:
        path = workdir / rel
        if path.is_file():
            hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _git_files(workdir: Path) -> list[str] | None:
    if shutil.which("git") is None:
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(workdir), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return sorted({p for p in out.decode().split("\0") if p})


def changed(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))


def is_allowed(path: str, allowed_paths: Sequence[str]) -> bool:
    """Whether path (POSIX, relative) is one of allowed_paths or under one of them."""
    target = PurePosixPath(path)
    for allowed in allowed_paths:
        base = PurePosixPath(allowed.strip("/") or ".")
        if base == PurePosixPath(".") or target == base or base in target.parents:
            return True
    return False


def outside(paths: Sequence[str], allowed_paths: Sequence[str]) -> list[str]:
    return [p for p in paths if not is_allowed(p, allowed_paths)]


def _normalise_allowed(workdir: Path, allowed_paths: Sequence[str]) -> list[str]:
    root = workdir.resolve()
    result = []
    for allowed in allowed_paths:
        resolved = (root / allowed).resolve()
        if resolved != root and root not in resolved.parents:
            raise AgentError(f"allowed path {allowed!r} is outside the working tree")
        result.append(resolved.relative_to(root).as_posix())
    if not result:
        raise AgentError("allowed_paths is empty: name at least one path the agent may change")
    return result


# --- claude-code ---


def _process_env() -> Mapping[str, str]:
    return os.environ


CLAUDE_CREDENTIALS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
READ_TOOLS = ("Read", "Glob", "Grep")


@dataclass
class ClaudeCodeProvider:
    """Runs the `claude` CLI headless in the working tree.

    Locally that is `claude -p`; in CI the Claude Code GitHub Action installs
    the same CLI and sets CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY. The
    agent may read anything in the tree and edit only allowed_paths; any other
    tool use is denied (permission mode dontAsk). Project settings in the
    repo are not loaded, so the repo can't widen permissions or supply a key.
    """

    env: Mapping[str, str] = field(default_factory=_process_env)
    executable: str = "claude"
    model: str | None = None
    timeout: float = 1800.0
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
    name: str = "claude-code"

    def check(self) -> None:
        if not any(self.env.get(var) for var in CLAUDE_CREDENTIALS):
            raise CredentialsError(
                f"the claude-code provider needs credentials: set {CLAUDE_CREDENTIALS[0]} "
                f"(or {CLAUDE_CREDENTIALS[1]}, as the Claude Code GitHub Action does)"
            )
        if shutil.which(self.executable, path=self.env.get("PATH")) is None:
            raise AgentError(f"`{self.executable}` is not on PATH: install Claude Code (npm install -g @anthropic-ai/claude-code)")

    def command(self, workdir: Path, allowed_paths: Sequence[str], budget: Budget) -> list[str]:
        tools = [*READ_TOOLS]
        for allowed in allowed_paths:
            pattern = "./**" if allowed == "." else (f"./{allowed}" if (workdir / allowed).is_file() else f"./{allowed}/**")
            tools += [f"Edit({pattern})", f"Write({pattern})"]
        cmd = [
            self.executable,
            "--print",
            "--output-format", "json",
            "--max-budget-usd", f"{budget.max_usd:.4f}",
            "--permission-mode", "dontAsk",
            "--setting-sources", "user",
            "--strict-mcp-config",
            "--allowedTools", *tools,
        ]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def run(self, prompt: str, workdir: Path, allowed_paths: Sequence[str], budget: Budget) -> AgentResult:
        self.check()
        allowed = _normalise_allowed(workdir, allowed_paths)
        before = snapshot(workdir)
        try:
            proc = self.runner(
                self.command(workdir, allowed, budget),
                input=prompt,
                cwd=workdir,
                env=dict(self.env),
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as err:
            raise AgentError(f"claude did not finish within {self.timeout:.0f}s") from err
        except OSError as err:
            raise AgentError(f"could not run {self.executable}: {err}") from err
        result = parse_claude(proc.stdout, proc.returncode, proc.stderr)
        result.provider = self.name
        files = changed(before, snapshot(workdir))
        result.changed_files = files
        result.outside_allowed = outside(files, allowed)
        if result.cost_usd > budget.max_usd:
            result.over_budget = True
        result.ok = result.ok and not result.over_budget
        return result


def parse_claude(stdout: str, returncode: int = 0, stderr: str = "") -> AgentResult:
    """Map `claude --print --output-format json` output to an AgentResult."""
    try:
        doc = json.loads(stdout)
    except json.JSONDecodeError as err:
        detail = (stderr or stdout).strip().splitlines()[-1:] or ["no output"]
        raise AgentError(f"claude exited {returncode} without a JSON result: {detail[0]}") from err
    if isinstance(doc, list):  # some versions print the whole message stream; the result is last
        doc = next((m for m in reversed(doc) if isinstance(m, dict) and m.get("type") == "result"), {})
    if not isinstance(doc, dict) or doc.get("type") != "result":
        raise AgentError("claude's JSON output has no result message")
    usage = doc.get("usage") or {}
    subtype = str(doc.get("subtype") or "")
    over_budget = "budget" in subtype
    ok = returncode == 0 and not doc.get("is_error") and subtype == "success"
    summary = doc.get("result")
    if not isinstance(summary, str) or not summary:
        summary = f"claude stopped: {subtype or 'error'}"
    return AgentResult(
        ok=ok,
        summary=summary,
        input_tokens=int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cost_usd=float(doc.get("total_cost_usd") or 0.0),
        over_budget=over_budget,
    )


# --- fake ---


FAKE_SCRIPT_ENV = "RLP_FAKE_AGENT_SCRIPT"


@dataclass
class FakeStep:
    """One scripted call: files to write (content) or delete (None), and what it reports."""

    files: dict[str, str | None] = field(default_factory=dict)
    summary: str = "fake agent applied its scripted patch"
    cost_usd: float = 0.01
    input_tokens: int = 100
    output_tokens: int = 50
    ok: bool = True


@dataclass
class FakeProvider:
    """Applies one scripted patch per call and reports a fixed cost.

    The script is a list of FakeStep, or a JSON file named by
    RLP_FAKE_AGENT_SCRIPT holding a list of objects with the same keys
    (files, summary, costUsd, inputTokens, outputTokens, ok). Once the script
    runs out, calls change nothing and report ok=False.
    """

    steps: list[FakeStep] = field(default_factory=list)
    name: str = "fake"
    prompts: list[str] = field(default_factory=list)  # every prompt received, for assertions

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> FakeProvider:
        path = env.get(FAKE_SCRIPT_ENV)
        if not path:
            return cls()
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            raise AgentError(f"can't read the fake agent script {path}: {err}") from err
        if not isinstance(raw, list):
            raise AgentError(f"the fake agent script {path} must be a JSON list of steps")
        return cls([
            FakeStep(
                files=step.get("files") or {},
                summary=step.get("summary", FakeStep.summary),
                cost_usd=float(step.get("costUsd", FakeStep.cost_usd)),
                input_tokens=int(step.get("inputTokens", FakeStep.input_tokens)),
                output_tokens=int(step.get("outputTokens", FakeStep.output_tokens)),
                ok=bool(step.get("ok", True)),
            )
            for step in raw
        ])

    def check(self) -> None:
        return None

    def run(self, prompt: str, workdir: Path, allowed_paths: Sequence[str], budget: Budget) -> AgentResult:
        allowed = _normalise_allowed(workdir, allowed_paths)
        self.prompts.append(prompt)
        if not self.steps:
            return AgentResult(ok=False, summary="fake agent: script exhausted", provider=self.name)
        step = self.steps.pop(0)
        before = snapshot(workdir)
        for rel, content in step.files.items():
            path = workdir / rel
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
        files = changed(before, snapshot(workdir))
        over = step.cost_usd > budget.max_usd
        return AgentResult(
            ok=step.ok and not over,
            summary=step.summary,
            changed_files=files,
            outside_allowed=outside(files, allowed),
            input_tokens=step.input_tokens,
            output_tokens=step.output_tokens,
            cost_usd=step.cost_usd,
            over_budget=over,
            provider=self.name,
        )


# --- selection ---


def _claude_code(env: Mapping[str, str]) -> ClaudeCodeProvider:
    return ClaudeCodeProvider(env=env)


PROVIDERS: dict[str, Callable[[Mapping[str, str]], AgentProvider]] = {
    "claude-code": _claude_code,
    "fake": FakeProvider.from_env,
}


def provider_name(name: str | None = None, env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return name or env.get(PROVIDER_ENV) or DEFAULT_PROVIDER


def get_provider(name: str | None = None, env: Mapping[str, str] | None = None) -> AgentProvider:
    """The provider named by `name`, else RLP_AGENT_PROVIDER, else claude-code."""
    env = os.environ if env is None else env
    chosen = provider_name(name, env)
    factory = PROVIDERS.get(chosen)
    if factory is None:
        raise AgentError(f"unknown agent provider {chosen!r}; choose one of: {', '.join(sorted(PROVIDERS))}")
    return factory(env)
