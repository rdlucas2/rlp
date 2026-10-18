"""Run the policy bundle against a repo through conftest."""

from __future__ import annotations

import json
import re
import subprocess
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


def conftest_args(conftest: Path, bundle: Path, namespaces: tuple[str, ...]) -> list[str]:
    args = [str(conftest), "test", "--combine", "--no-color", "--output", "json", "--ignore", IGNORE]
    args += ["--policy", str(bundle), "--data", str(bundle)]
    if namespaces:
        for ns in namespaces:
            args += ["--namespace", ns]
    else:
        args.append("--all-namespaces")
    return [*args, "."]


def parse(stdout: str) -> list[Violation]:
    try:
        report = json.loads(stdout)
    except json.JSONDecodeError as err:
        raise PolicyError(f"conftest returned non-JSON output: {stdout[:500]}") from err
    violations = []
    for result in report:
        if result.get("exceptions"):
            raise PolicyError(f"conftest reported exceptions: {result['exceptions']}")
        for failure in result.get("failures") or []:
            msg = failure["msg"]
            m = MESSAGE.match(msg)
            namespace = result.get("namespace", "")
            if m:
                violations.append(Violation(m["rule"], m["file"], m["field"], msg, namespace))
            else:
                violations.append(Violation("", "", "", msg, namespace))
    return sorted(violations, key=lambda v: (v.rule, v.file, v.field, v.message))


def check(repo: Path, namespaces: tuple[str, ...] = ()) -> list[Violation]:
    if not repo.is_dir():
        raise PolicyError(f"{repo} is not a directory")
    try:
        conftest = tools.ensure()["conftest"]
    except tools.ToolError as err:
        raise PolicyError(str(err)) from err
    args = conftest_args(conftest, resources.policy_dir(), namespaces)
    result = subprocess.run(args, cwd=repo, capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode not in (0, 1):
        raise PolicyError(f"conftest exited {result.returncode}: {result.stderr.strip() or result.stdout.strip()}")
    return parse(result.stdout)
