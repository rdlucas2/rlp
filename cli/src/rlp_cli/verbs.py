"""Run the standard verbs for each component."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from rlp_cli.contract import component_dir

VERBS = ("setup", "lint", "test", "build", "package", "run", "smoke")


class VerbError(Exception):
    """A verb could not be started."""


@dataclass
class Step:
    component: str
    verb: str
    command: list[str]
    cwd: Path
    exit_code: int | None = None
    seconds: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def as_dict(self, root: Path) -> dict:
        try:
            cwd = self.cwd.relative_to(root).as_posix() or "."
        except ValueError:
            cwd = str(self.cwd)
        return {
            "component": self.component,
            "verb": self.verb,
            "command": self.command,
            "cwd": cwd,
            "exit_code": self.exit_code,
            "seconds": round(self.seconds, 3),
            "error": self.error,
        }


def command_for(component: dict, verb: str) -> list[str]:
    """verbs.<name> split with POSIX rules on every OS, else `task <name>`.

    Overrides run without a shell: no pipes, globs or variable expansion.
    """
    override = (component.get("verbs") or {}).get(verb)
    if override:
        return shlex.split(override, posix=True)
    return ["task", verb]


def plan(root: Path, doc: dict, verb: str, only: tuple[str, ...] = ()) -> list[Step]:
    components = doc.get("components") or []
    names = [c["name"] for c in components]
    unknown = [n for n in only if n not in names]
    if unknown:
        raise VerbError(f"unknown component(s): {', '.join(unknown)}; rlp.yaml declares: {', '.join(names)}")
    return [
        Step(c["name"], verb, command_for(c, verb), component_dir(root, c))
        for c in components
        if not only or c["name"] in only
    ]


def execute(step: Step, stdout=None, stderr=None) -> Step:
    executable = shutil.which(step.command[0], path=None)
    if executable is None:
        step.error = f"command not found: {step.command[0]}"
        step.exit_code = 127
        return step
    if not step.cwd.is_dir():
        step.error = f"component directory not found: {step.cwd}"
        step.exit_code = 1
        return step
    start = time.monotonic()
    result = subprocess.run([executable, *step.command[1:]], cwd=step.cwd, stdout=stdout, stderr=stderr, check=False)
    step.seconds = time.monotonic() - start
    step.exit_code = result.returncode
    return step
