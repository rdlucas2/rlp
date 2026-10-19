"""Shared fixtures for the CLI tests.

Tests drive rlp as a subprocess, exactly as a user or CI would. By default that
is `python -m rlp_cli` from the installed package; set RLP_BIN to a standalone
binary to run the same suite against it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "examples"
SCHEMA_FIXTURES = REPO / "schemas" / "tests" / "fixtures"
POLICY_FIXTURES = REPO / "tests" / "policy" / "fixtures"


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str

    @property
    def json(self):
        return json.loads(self.stdout)


def rlp_command() -> list[str]:
    binary = os.environ.get("RLP_BIN")
    if binary:
        return [str(Path(binary).resolve())]
    return [sys.executable, "-m", "rlp_cli"]


@pytest.fixture
def rlp():
    def run(*args: str, cwd: Path | None = None, env: dict | None = None) -> Result:
        full_env = {**os.environ, **(env or {})}
        full_env.pop("RLP_OUTPUT", None)
        proc = subprocess.run(
            [*rlp_command(), *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=full_env,
            check=False,
        )
        return Result(proc.returncode, proc.stdout, proc.stderr)

    return run


def posix_python() -> str:
    """This interpreter's path with forward slashes, safe inside a POSIX-quoted verb override on any OS."""
    return Path(sys.executable).as_posix()


@pytest.fixture
def python_cmd() -> str:
    return f'"{posix_python()}"'


# The policy fixtures store Dockerfiles and plan JSON under other names, so
# `rlp policy check` on the rlp repo skips them (#62); tests/policy/test_bundle.py
# holds the same map.
STORED_FIXTURE_NAMES = {"Containerfile.fixture": "Dockerfile", "plan.json.fixture": "plan.json", "rlp-change.json.fixture": "rlp-change.json"}


def restore_fixture_names(repo: Path) -> None:
    for stored, real in STORED_FIXTURE_NAMES.items():
        for path in list(repo.rglob(stored)):
            path.rename(path.with_name(real))


@pytest.fixture
def copy_fixture(tmp_path: Path):
    def copy(src: Path, name: str | None = None) -> Path:
        dest = tmp_path / (name or src.name)
        if src.is_dir():
            shutil.copytree(src, dest)
            restore_fixture_names(dest)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
        return dest

    return copy
