"""Build the standalone rlp binary for this OS with PyInstaller.

PyInstaller can't cross-compile, so the release workflow runs this on one
runner per OS. The binary embeds the schemas and the policy bundle.

Usage: python cli/build_binary.py [--dist DIR]
Prints the path of the built binary.
"""

from __future__ import annotations

import argparse
import os
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def asset_name() -> str:
    system = {"darwin": "macos"}.get(platform.system().lower(), platform.system().lower())
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    return f"rlp-{system}-{machine}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    args = parser.parse_args()

    import PyInstaller.__main__

    name = asset_name()
    work = ROOT / "build" / "pyinstaller"
    sep = os.pathsep
    PyInstaller.__main__.run([
        str(ROOT / "cli" / "src" / "rlp_cli" / "__main__.py"),
        "--onefile",
        "--noconfirm",
        "--clean",
        "--name", name,
        "--distpath", str(args.dist),
        "--workpath", str(work),
        "--specpath", str(work),
        "--paths", str(ROOT / "cli" / "src"),
        "--collect-submodules", "rlp_cli",
        "--collect-all", "copier",
        # copier loads this Jinja extension by name at render time
        "--collect-all", "jinja2_ansible_filters",
        # diff-cover (new-code coverage) loads its report templates and plugins at run time
        "--collect-all", "diff_cover",
        "--add-data", f"{ROOT / 'schemas' / 'v1alpha1'}{sep}rlp_cli/_data/schemas/v1alpha1",
        "--add-data", f"{ROOT / 'policy'}{sep}rlp_cli/_data/policy",
    ])
    exe = ".exe" if sys.platform == "win32" else ""
    print((args.dist / f"{name}{exe}").resolve().as_posix())
    return 0


if __name__ == "__main__":
    sys.exit(main())
