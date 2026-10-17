"""Pinned OPA, conftest, gitleaks, Trivy and Infracost binaries, and the Semgrep rules.

`rlp policy check` runs the policy bundle through conftest, the pipeline's
secret scan runs gitleaks and its vulnerability scan runs Trivy. Rather than
depend on whatever is on PATH, rlp downloads the pinned versions once into a
per-user cache and checks each against the SHA256 published with its release.
Versions match .devcontainer/install-tools.sh.

Trivy is large and only the trivy stage needs it, so it installs separately
(ensure_trivy), not with the policy tools. Infracost likewise installs only
for the cost stage (ensure_infracost). Semgrep is a Python package, so it
is installed with pip at SEMGREP_VERSION (golden.yml does this); rlp fetches
only its rules, at a pinned commit.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

OPA_VERSION = "1.21.2"
CONFTEST_VERSION = "0.71.1"
GITLEAKS_VERSION = "8.30.1"
TRIVY_VERSION = "0.76.0"
SEMGREP_VERSION = "1.179.0"
INFRACOST_VERSION = "0.10.46"
# semgrep/semgrep-rules has no releases; pin a commit. Fetched with git, so the
# commit hash is the integrity check.
SEMGREP_RULES_REPO = "https://github.com/semgrep/semgrep-rules"
SEMGREP_RULES_COMMIT = "a84ff9cc2453ca91d581380de4b8b3f272f6f4be"
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
TRIVY = {
    ("linux", "x86_64"): ("Linux-64bit.tar.gz", "e24b1c51293c1b15e4ba8d45bbc7891ba053f90278ec2698f3562c993e145fe7"),
    ("linux", "arm64"): ("Linux-ARM64.tar.gz", "aab21e677a7b7071fc79f1711480840754c18a1b9d9cefa7ccd06af63a0e8f4c"),
    ("darwin", "x86_64"): ("macOS-64bit.tar.gz", "89563d4d495cf4ef06a0d1950f8710b124e9f7bd2615ec6c47b03673f4ad45a2"),
    ("darwin", "arm64"): ("macOS-ARM64.tar.gz", "e7ec0d21fe842553f59af99aba2efbcc822369fbaed27019c575408b6d135fce"),
    ("windows", "x86_64"): ("windows-64bit.zip", "7b4762a5a5326cb4b83dfd2bb5d9aaf15a1ed867c75cad7dd8b38840a28e7be6"),
}
# Infracost archives hold one binary, named per platform: (asset suffix, sha256, member).
INFRACOST = {
    ("linux", "x86_64"): ("linux-amd64", "d0d081cd39b07b2ca5c315830bfc4bcdfb0183b04c19cb18835c154482a2c97b", "infracost-linux-amd64"),
    ("linux", "arm64"): ("linux-arm64", "acd693c7d001fef44787bc5de65fffcb25bf5445885fd1da98a7b31281e32b43", "infracost-linux-arm64"),
    ("darwin", "x86_64"): ("darwin-amd64", "16f9d48469fcbc5e8133f10dca54cd34f15487f1da331c1be579cc8d80d9c3dc", "infracost-darwin-amd64"),
    ("darwin", "arm64"): ("darwin-arm64", "09fa73ecdc762c1b557df70234eb44cc446a9a9e412a4ab326185cac3f39e631", "infracost-darwin-arm64"),
    ("windows", "x86_64"): ("windows-amd64", "a915a576167d0138089ec2e9d0ff2a783a114580808cff2b573e42e1088e7929", "infracost.exe"),
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


def _cache_base() -> Path:
    system = current_platform()[0]
    if system == "windows":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif system == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "rlp" / "tools"


def cache_dir() -> Path:
    """Where pinned tools live: $RLP_TOOLS_DIR, else the platform's user cache."""
    override = os.environ.get("RLP_TOOLS_DIR")
    if override:
        return Path(override)
    return _cache_base() / f"opa-{OPA_VERSION}-conftest-{CONFTEST_VERSION}-gitleaks-{GITLEAKS_VERSION}"


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


def ensure_trivy(dest: Path | None = None) -> Path:
    """Install the pinned Trivy into dest (default: next to the other tools) and return its path."""
    override = os.environ.get("RLP_TOOLS_DIR")
    dest = dest or (Path(override) if override else _cache_base() / f"trivy-{TRIVY_VERSION}")
    key = current_platform()
    if key not in TRIVY:
        raise ToolError(f"no pinned Trivy build for {key[0]} {key[1]}")
    exe = exe_suffix()
    path = dest / f"trivy{exe}"
    stamp = dest / ".trivy-version"
    if stamp.exists() and stamp.read_text(encoding="utf-8") == TRIVY_VERSION and path.exists():
        return path
    dest.mkdir(parents=True, exist_ok=True)
    suffix, sha = TRIVY[key]
    asset = f"trivy_{TRIVY_VERSION}_{suffix}"
    archive = _fetch(f"{GITHUB}/aquasecurity/trivy/releases/download/v{TRIVY_VERSION}/{asset}", sha)
    _write_executable(path, _extract(archive, asset, f"trivy{exe}"))
    stamp.write_text(TRIVY_VERSION, encoding="utf-8")
    return path


def ensure_infracost(dest: Path | None = None) -> Path:
    """Install the pinned Infracost into dest (default: the user cache) and return its path."""
    override = os.environ.get("RLP_TOOLS_DIR")
    dest = dest or (Path(override) if override else _cache_base() / f"infracost-{INFRACOST_VERSION}")
    key = current_platform()
    if key not in INFRACOST:
        raise ToolError(f"no pinned Infracost build for {key[0]} {key[1]}")
    path = dest / f"infracost{exe_suffix()}"
    stamp = dest / ".infracost-version"
    if stamp.exists() and stamp.read_text(encoding="utf-8") == INFRACOST_VERSION and path.exists():
        return path
    dest.mkdir(parents=True, exist_ok=True)
    suffix, sha, member = INFRACOST[key]
    asset = f"infracost-{suffix}.tar.gz"
    archive = _fetch(f"{GITHUB}/infracost/infracost/releases/download/v{INFRACOST_VERSION}/{asset}", sha)
    _write_executable(path, _extract(archive, asset, member))
    stamp.write_text(INFRACOST_VERSION, encoding="utf-8")
    return path


def ensure_semgrep_rules(dest: Path | None = None) -> Path:
    """Fetch semgrep-rules at the pinned commit (default: into the user cache) and return the checkout.

    $RLP_SEMGREP_RULES points at an existing checkout instead, for offline use.
    """
    override = os.environ.get("RLP_SEMGREP_RULES")
    if override:
        return Path(override)
    dest = dest or _cache_base() / f"semgrep-rules-{SEMGREP_RULES_COMMIT[:12]}"
    stamp = dest / ".rlp-commit"
    if stamp.exists() and stamp.read_text(encoding="utf-8") == SEMGREP_RULES_COMMIT:
        return dest
    git = shutil.which("git")
    if git is None:
        raise ToolError("git is needed to fetch the Semgrep rules")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for args in (
        ["init", "-q"],
        ["fetch", "-q", "--depth", "1", SEMGREP_RULES_REPO, SEMGREP_RULES_COMMIT],
        ["-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD"],
    ):
        proc = subprocess.run([git, *args], cwd=dest, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise ToolError(f"could not fetch {SEMGREP_RULES_REPO}@{SEMGREP_RULES_COMMIT}: {proc.stderr.strip()}")
    head = subprocess.run([git, "rev-parse", "HEAD"], cwd=dest, capture_output=True, text=True, check=False).stdout.strip()
    if head != SEMGREP_RULES_COMMIT:
        raise ToolError(f"semgrep-rules checkout is at {head}, want {SEMGREP_RULES_COMMIT}")
    stamp.write_text(SEMGREP_RULES_COMMIT, encoding="utf-8")
    return dest


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
