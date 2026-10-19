"""Run the policy bundle against a repo through conftest."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from rlp_cli import resources, tools

MESSAGE = re.compile(r"^\[(?P<rule>RLP-[A-Z][0-9]{3})\] (?P<file>\S+) (?P<field>.+?): (?P<problem>.+)\. Fix: (?P<fix>.+)\.$")

# Paths conftest should never parse: VCS data, dependencies, caches, and Rego
# sources. Rego is policy, not configuration, and conftest would read a file
# like dockerfile.rego as a Dockerfile and fail to parse it.
IGNORE = r"(^|[/\\])(\.git|\.venv|venv|node_modules|\.tools|\.task|\.terraform|\.pytest_cache|__pycache__)([/\\]|$)|\.rego$"


class PolicyError(Exception):
    """conftest could not run or returned something unexpected."""


@dataclass(frozen=True)
class Violation:
    rule: str
    file: str
    field: str
    message: str
    namespace: str

    def as_dict(self) -> dict:
        return {"rule": self.rule, "file": self.file, "field": self.field, "message": self.message, "namespace": self.namespace}


@dataclass(frozen=True)
class Report:
    """What conftest found: violations (deny rules) fail a check; warnings (warn rules) don't."""

    violations: list[Violation]
    warnings: list[Violation]


def findings(result: dict, key: str) -> list[Violation]:
    out = []
    namespace = result.get("namespace", "")
    for finding in result.get(key) or []:
        msg = finding["msg"]
        m = MESSAGE.match(msg)
        if m:
            out.append(Violation(m["rule"], m["file"], m["field"], msg, namespace))
        else:
            out.append(Violation("", "", "", msg, namespace))
    return out


def ordered(items: list[Violation]) -> list[Violation]:
    return sorted(items, key=lambda v: (v.rule, v.file, v.field, v.message))


def conftest_args(conftest: Path, bundle: Path, namespaces: tuple[str, ...], extra: tuple[Path, ...] = ()) -> list[str]:
    args = [str(conftest), "test", "--combine", "--no-color", "--output", "json", "--ignore", IGNORE]
    args += ["--policy", str(bundle), "--data", str(bundle)]
    if namespaces:
        for ns in namespaces:
            args += ["--namespace", ns]
    else:
        args.append("--all-namespaces")
    return [*args, ".", *(str(p) for p in extra)]


def parse(stdout: str) -> Report:
    try:
        report = json.loads(stdout)
    except json.JSONDecodeError as err:
        raise PolicyError(f"conftest returned non-JSON output: {stdout[:500]}") from err
    violations, warnings = [], []
    for result in report:
        if result.get("exceptions"):
            raise PolicyError(f"conftest reported exceptions: {result['exceptions']}")
        violations += findings(result, "failures")
        warnings += findings(result, "warnings")
    return Report(ordered(violations), ordered(warnings))


def check(repo: Path, namespaces: tuple[str, ...] = (), change: dict | None = None) -> Report:
    """Violations and warnings in the repo. change is the rlp.agent input (change.build); it is checked alongside the repo's files."""
    if not repo.is_dir():
        raise PolicyError(f"{repo} is not a directory")
    try:
        conftest = tools.ensure()["conftest"]
    except tools.ToolError as err:
        raise PolicyError(str(err)) from err
    with tempfile.TemporaryDirectory(prefix="rlp-policy-") as tmp:
        extra: tuple[Path, ...] = ()
        if change is not None:
            path = Path(tmp) / "rlp-change.json"
            path.write_text(json.dumps(change), encoding="utf-8")
            extra = (path,)
        args = conftest_args(conftest, resources.policy_dir(), namespaces, extra)
        result = subprocess.run(args, cwd=repo, capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode not in (0, 1):
        raise PolicyError(f"conftest exited {result.returncode}: {result.stderr.strip() or result.stdout.strip()}")
    return parse(result.stdout)
