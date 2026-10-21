"""The pinned OPA binary the lifecycle rules run on.

The platform image builds this version from source with a patched Go toolchain
(the Dockerfile's opa stage) and puts it on PATH; at runtime `find()` looks it
up there. `python -m rlp_platform.opa DEST` installs the released binary
instead, for tests and local runs. The version and checksums are the ones the rlp CLI pins in
cli/src/rlp_cli/tools.py; tests/policy/test_lifecycle_copy.py at the rlp repo
root fails when they drift apart.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import stat
import sys
import urllib.request
from pathlib import Path

OPA_VERSION = "1.21.2"

# (system, machine) -> (release asset, sha256)
ASSETS = {
    ("linux", "x86_64"): (
        "opa_linux_amd64_static",
        "830ab11f61ffec760b5e19db0d7ad02bf037e13f5ef4d665faeaf17632177527",
    ),
    ("linux", "arm64"): (
        "opa_linux_arm64_static",
        "ed13bb8647b8a81e1f11741dd18babe02aced7a304314bd6bf8007793b6d4ff1",
    ),
    ("darwin", "x86_64"): (
        "opa_darwin_amd64",
        "4bd9627845899723add7d2dae1bdf97407b2c56fc8d4d15895c6de9c3aa64fec",
    ),
    ("darwin", "arm64"): (
        "opa_darwin_arm64_static",
        "e358121f0d96a41103499d878184e493cc5ab10956f98e835089b9de0ac2de62",
    ),
    ("windows", "x86_64"): (
        "opa_windows_amd64.exe",
        "0089fa7ad8cdea7ff9e56ff86fa9202462afb63ef74506e2b547a24b4a987ccb",
    ),
}


class OpaError(RuntimeError):
    """OPA is missing, or the pinned binary could not be installed."""


def current_platform() -> tuple[str, str]:
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    return platform.system().lower(), machine


def find() -> str:
    """The opa on PATH."""
    found = shutil.which("opa")
    if found is None:
        raise OpaError(f"opa is not on PATH; install OPA {OPA_VERSION} (the platform image ships it)")
    return found


def install(dest: Path) -> Path:
    """Download the pinned opa for this machine to `dest`, check its SHA256 and make it executable."""
    key = current_platform()
    if key not in ASSETS:
        raise OpaError(f"no pinned OPA build for {key[0]} {key[1]}")
    asset, sha256 = ASSETS[key]
    url = f"https://github.com/open-policy-agent/opa/releases/download/v{OPA_VERSION}/{asset}"
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            payload = response.read()
    except OSError as err:
        raise OpaError(f"could not download {url}: {err}") from err
    digest = hashlib.sha256(payload).hexdigest()
    if digest != sha256:
        raise OpaError(f"{url} has SHA256 {digest}, expected {sha256}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(payload)
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return dest


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m rlp_platform.opa DEST", file=sys.stderr)
        return 2
    try:
        path = install(Path(argv[1]))
    except OpaError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    print(f"installed OPA {OPA_VERSION} at {os.fspath(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
