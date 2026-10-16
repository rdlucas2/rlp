"""Install the pinned OPA and conftest binaries into .tools/bin.

Used by `task test` so the policy bundle is tested with the same versions on
a laptop and in CI. Every download is checked against the SHA256 published
with its release. Re-running is a no-op once the right versions are present.
Versions match .devcontainer/install-tools.sh.

Usage: python scripts/install_policy_tools.py [DEST]   (default: .tools/bin)
"""

from __future__ import annotations

import hashlib
import io
import platform
import stat
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

OPA_VERSION = "1.21.2"
CONFTEST_VERSION = "0.71.1"
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


def current_platform() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    return system, machine


def fetch(url: str, sha256: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:
        payload = response.read()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != sha256:
        raise SystemExit(f"checksum mismatch for {url}: got {digest}, want {sha256}")
    return payload


def write_executable(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def extract(archive: bytes, asset: str, member: str) -> bytes:
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            return zf.read(member)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
        handle = tf.extractfile(member)
        if handle is None:
            raise SystemExit(f"{member} not found in {asset}")
        return handle.read()


def main(argv: list[str]) -> int:
    dest = Path(argv[1]) if len(argv) > 1 else Path(".tools") / "bin"
    dest.mkdir(parents=True, exist_ok=True)
    key = current_platform()
    if key not in OPA or key not in CONFTEST:
        raise SystemExit(f"no pinned OPA/conftest build for {key}")
    exe = ".exe" if key[0] == "windows" else ""
    stamp = dest / ".policy-tools-version"
    want = f"opa {OPA_VERSION}\nconftest {CONFTEST_VERSION}\n"
    if stamp.exists() and stamp.read_text(encoding="utf-8") == want:
        if (dest / f"opa{exe}").exists() and (dest / f"conftest{exe}").exists():
            return 0

    asset, sha = OPA[key]
    write_executable(dest / f"opa{exe}", fetch(f"{GITHUB}/open-policy-agent/opa/releases/download/v{OPA_VERSION}/{asset}", sha))

    suffix, sha = CONFTEST[key]
    asset = f"conftest_{CONFTEST_VERSION}_{suffix}"
    archive = fetch(f"{GITHUB}/open-policy-agent/conftest/releases/download/v{CONFTEST_VERSION}/{asset}", sha)
    write_executable(dest / f"conftest{exe}", extract(archive, asset, f"conftest{exe}"))

    stamp.write_text(want, encoding="utf-8")
    print(f"installed opa {OPA_VERSION} and conftest {CONFTEST_VERSION} into {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
