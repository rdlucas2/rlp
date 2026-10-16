"""Pinned OPA, conftest and gitleaks binaries.

`rlp policy check` runs the policy bundle through conftest, and the pipeline's
secret scan runs gitleaks. Rather than depend on whatever is on PATH, rlp
downloads the pinned versions once into a per-user cache and checks each
against the SHA256 published with its release. Versions match
.devcontainer/install-tools.sh.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import stat
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

OPA_VERSION = "1.21.2"
CONFTEST_VERSION = "0.71.1"
GITLEAKS_VERSION = "8.30.1"
GITHUB = "https://github.com"

# (system, machine) -> (asset name, sha256)
OPA = {
    ("linux", "x86_64"): ("opa_linux_amd64_static", "830ab11f61ffec760b5e19db0d7ad02bf037e13f5ef4d665faeaf17632177527"),
    ("linux", "arm64"): ("opa_linux_arm64_static", "ed13bb8647b8a81e1f11741dd18babe02aced7a304314bd6bf8007793b6d4ff1"),
    ("darwin", "x86_64"): ("opa_darwin_amd64", "4bd9627845899723add7d2dae1bdf97407b2c56fc8d4d15895c6de9c3aa64fec"),
    ("darwin", "arm64"): ("opa_darwin_arm64_static", "e358121f0d96a41103499d878184e493cc5ab10956f98e835089b9de0ac2de62"),
    ("windows", "x86_64"): ("opa_windows_amd64.exe", "0089fa7ad8cdea7ff9e56ff86fa9202462afb63ef74506e2b547a24b4a987ccb"),
}
CONFTEST = {
    ("linux", "x86_64"): ("Linux_x86_64.tar.gz", "c3f6b2a753bd56e377a1e51a95ae3e057f926f2e9139f7d5b5dfbbaf810f7985"),
    ("linux", "arm64"): ("Linux_arm64.tar.gz", "218c682e110ae7e826dc14a746a6f9b43eb782643dbe730b771fd305634e3f47"),
    ("darwin", "x86_64"): ("Darwin_x86_64.tar.gz", "a309853b5763c18486768157442d440248ee683b20a2aa1f6b28214e818bd780"),
    ("darwin", "arm64"): ("Darwin_arm64.tar.gz", "e1cc50d2590b6c5e3da6912666a71264bae82d60bdd62d7ae2b99971ccc8eedd"),
    ("windows", "x86_64"): ("Windows_x86_64.zip", "34d623334b5e7ed4465bf62a4cb3b092684859cd3d224bbd390925a0852b922a"),
}
GITLEAKS = {
    ("linux", "x86_64"): ("linux_x64.tar.gz", "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"),
    ("linux", "arm64"): ("linux_arm64.tar.gz", "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080"),
    ("darwin", "x86_64"): ("darwin_x64.tar.gz", "dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709"),
    ("darwin", "arm64"): ("darwin_arm64.tar.gz", "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5"),
    ("windows", "x86_64"): ("windows_x64.zip", "d29144deff3a68aa93ced33dddf84b7fdc26070add4aa0f4513094c8332afc4e"),
}


class ToolError(RuntimeError):
    """A pinned tool could not be installed."""


def current_platform() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    return system, machine


def exe_suffix() -> str:
    return ".exe" if current_platform()[0] == "windows" else ""


def cache_dir() -> Path:
    """Where pinned tools live: $RLP_TOOLS_DIR, else the platform's user cache."""
    override = os.environ.get("RLP_TOOLS_DIR")
    if override:
        return Path(override)
    system = current_platform()[0]
    if system == "windows":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif system == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "rlp" / "tools" / f"opa-{OPA_VERSION}-conftest-{CONFTEST_VERSION}-gitleaks-{GITLEAKS_VERSION}"


def _fetch(url: str, sha256: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            payload = response.read()
    except OSError as err:
        raise ToolError(f"could not download {url}: {err}") from err
    digest = hashlib.sha256(payload).hexdigest()
    if digest != sha256:
        raise ToolError(f"checksum mismatch for {url}: got {digest}, want {sha256}")
    return payload


def _write_executable(path: Path, payload: bytes) -> None:
    tmp = path.with_name(path.name + ".partial")
    tmp.write_bytes(payload)
    tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    tmp.replace(path)


def _extract(archive: bytes, asset: str, member: str) -> bytes:
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            return zf.read(member)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
        handle = tf.extractfile(member)
        if handle is None:
            raise ToolError(f"{member} not found in {asset}")
        return handle.read()


def ensure(dest: Path | None = None) -> dict[str, Path]:
    """Install the pinned opa, conftest and gitleaks into dest (default: cache_dir()) and return their paths."""
    dest = dest or cache_dir()
    key = current_platform()
    if key not in OPA or key not in CONFTEST or key not in GITLEAKS:
        raise ToolError(f"no pinned OPA/conftest/gitleaks build for {key[0]} {key[1]}")
    exe = exe_suffix()
    paths = {"opa": dest / f"opa{exe}", "conftest": dest / f"conftest{exe}", "gitleaks": dest / f"gitleaks{exe}"}
    stamp = dest / ".versions"
    want = f"opa {OPA_VERSION}\nconftest {CONFTEST_VERSION}\ngitleaks {GITLEAKS_VERSION}\n"
    if stamp.exists() and stamp.read_text(encoding="utf-8") == want and all(p.exists() for p in paths.values()):
        return paths

    dest.mkdir(parents=True, exist_ok=True)
    asset, sha = OPA[key]
    _write_executable(paths["opa"], _fetch(f"{GITHUB}/open-policy-agent/opa/releases/download/v{OPA_VERSION}/{asset}", sha))
    suffix, sha = CONFTEST[key]
    asset = f"conftest_{CONFTEST_VERSION}_{suffix}"
    archive = _fetch(f"{GITHUB}/open-policy-agent/conftest/releases/download/v{CONFTEST_VERSION}/{asset}", sha)
    _write_executable(paths["conftest"], _extract(archive, asset, f"conftest{exe}"))
    suffix, sha = GITLEAKS[key]
    asset = f"gitleaks_{GITLEAKS_VERSION}_{suffix}"
    archive = _fetch(f"{GITHUB}/gitleaks/gitleaks/releases/download/v{GITLEAKS_VERSION}/{asset}", sha)
    _write_executable(paths["gitleaks"], _extract(archive, asset, f"gitleaks{exe}"))
    stamp.write_text(want, encoding="utf-8")
    return paths


def main(argv: list[str]) -> int:
    dest = Path(argv[1]) if len(argv) > 1 else None
    try:
        paths = ensure(dest)
    except ToolError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    for name, path in paths.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
