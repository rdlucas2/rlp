"""Secret scanning with the pinned gitleaks.

In a git work tree the scan covers the full history (`gitleaks git`), so a
secret committed and then deleted still fails. Outside git it scans the
directory. `.gitleaks.toml` and `.gitleaksignore` in the repo apply as usual.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from rlp_cli import sarif as sarif_report
from rlp_cli import tools


class ScanError(Exception):
    """gitleaks could not run."""


def in_git(root: Path) -> bool:
    try:
        out = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=root, capture_output=True, text=True, check=False)
    except OSError:
        return False
    return out.returncode == 0 and out.stdout.strip() == "true"


def scan(root: Path, sarif: Path | None = None) -> list[dict]:
    """Findings as dicts with rule, file, line and commit (secrets redacted).

    With sarif, also write a SARIF report there for code scanning.
    """
    try:
        gitleaks = tools.ensure()["gitleaks"]
    except tools.ToolError as err:
        raise ScanError(str(err)) from err
    mode = "git" if in_git(root) else "dir"
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "gitleaks.json"
        args = [str(gitleaks), mode, ".", "--redact", "--no-banner", "--report-format", "json", "--report-path", str(report), "--exit-code", "1"]
        result = subprocess.run(args, cwd=root, capture_output=True, text=True, encoding="utf-8", check=False)
        if result.returncode not in (0, 1):
            raise ScanError(f"gitleaks exited {result.returncode}: {result.stderr.strip()}")
        findings = json.loads(report.read_text(encoding="utf-8") or "[]") if report.exists() else []
    if sarif is not None:
        sarif.parent.mkdir(parents=True, exist_ok=True)
        args[args.index("--report-format") + 1] = "sarif"
        args[args.index("--report-path") + 1] = str(sarif)
        result = subprocess.run(args, cwd=root, capture_output=True, text=True, encoding="utf-8", check=False)
        if result.returncode not in (0, 1):
            raise ScanError(f"gitleaks exited {result.returncode} writing SARIF: {result.stderr.strip()}")
        sarif_report.tag(sarif, "gitleaks")
    return [
        {
            "rule": f.get("RuleID", ""),
            "file": f.get("File", "").replace("\\", "/"),
            "line": f.get("StartLine"),
            "commit": f.get("Commit", "")[:12],
            "description": f.get("Description", ""),
        }
        for f in findings
    ]
