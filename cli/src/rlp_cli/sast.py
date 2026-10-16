"""Static analysis with Semgrep Community Edition and the pinned semgrep-rules.

Semgrep is a Python package: install it at tools.SEMGREP_VERSION (golden.yml
does). rlp fetches the rules itself at a pinned commit of semgrep-rules, so a
scan needs no account, no registry and no login, and runs with metrics off.

The rules come from each component's `language` (python, typescript, go, ...)
plus Dockerfile and GitHub Actions rules for every repo. Rules under `audit/`
are left out: they flag patterns to review by hand, not defects.

The gate fails on any ERROR or WARNING finding that is new since the baseline
commit (rlp_cli.baseline). With no baseline, every finding is new. A
`# nosemgrep` comment silences a finding; like VEX exceptions, those are
quality-gate configuration that agent-authored changes may not add.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from rlp_cli import sarif, tools
from rlp_cli.contract import component_dir

REPORTS = Path(".rlp") / "pipeline" / "reports"
SEVERITIES = ("ERROR", "WARNING")
ALWAYS = ("dockerfile", "yaml/github-actions")
# component.language -> semgrep-rules directories; other languages map to the directory of the same name
LANGUAGES = {
    "typescript": ("typescript", "javascript"),
    "ts": ("typescript", "javascript"),
    "javascript": ("javascript",),
    "js": ("javascript",),
    "node": ("javascript",),
    "golang": ("go",),
    "c#": ("csharp",),
    "dotnet": ("csharp",),
}


class SastError(Exception):
    """Semgrep is missing or could not run."""


@dataclass
class Finding:
    rule: str
    path: str
    line: int
    severity: str
    message: str

    def line_text(self) -> str:
        return f"{self.path}:{self.line} {self.rule} ({self.severity})"


def rule_dirs(rules: Path, doc: dict, only: tuple[str, ...] = ()) -> list[str]:
    """The semgrep-rules directories that apply to this repo, in a stable order."""
    wanted = []
    for c in doc.get("components") or []:
        if only and c["name"] not in only:
            continue
        lang = str(c.get("language", "")).lower()
        wanted += LANGUAGES.get(lang, (lang,))
    wanted += ALWAYS
    return [d for d in dict.fromkeys(wanted) if d and (rules / d).is_dir()]


def select_rules(rules: Path, dirs: list[str], dest: Path) -> int:
    """Copy the rule files under dirs into dest, leaving out audit rules and rule tests. Returns the count."""
    count = 0
    for d in dirs:
        for path in sorted((rules / d).rglob("*.yaml")):
            rel = path.relative_to(rules)
            if path.name.endswith(".test.yaml") or "audit" in rel.parts:
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            count += 1
    return count


def _rule_id(check_id: str, config: Path) -> str:
    """Semgrep prefixes rule ids with the config directory's path; drop it."""
    for path in (config, config.resolve()):
        prefix = ".".join(p.strip("/\\:") for p in path.parts if p.strip("/\\:")) + "."
        if check_id.startswith(prefix):
            return check_id[len(prefix):]
    return check_id


def find_semgrep() -> tuple[Path, str]:
    """The semgrep on PATH and its version."""
    exe = shutil.which("semgrep")
    if exe is None:
        raise SastError(f"semgrep is not installed: pip install semgrep=={tools.SEMGREP_VERSION}")
    proc = subprocess.run([exe, "--version"], capture_output=True, text=True, check=False)
    return Path(exe), proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "unknown"


def scan(root: Path, doc: dict, baseline: str | None, only: tuple[str, ...] = ()) -> dict:
    """Run Semgrep over the repo (or the selected components). Returns the findings and report paths."""
    exe, version = find_semgrep()
    try:
        rules = tools.ensure_semgrep_rules()
    except tools.ToolError as err:
        raise SastError(str(err)) from err
    dirs = rule_dirs(rules, doc, only)
    out = root / REPORTS
    out.mkdir(parents=True, exist_ok=True)
    json_path, sarif_path = out / "semgrep.json", out / "semgrep.sarif"
    targets = [str(component_dir(root, c)) for c in doc.get("components") or [] if c["name"] in only] if only else ["."]
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "rules"
        count = select_rules(rules, dirs, config)
        if count == 0:
            raise SastError(f"no Semgrep rules found under {rules} for {', '.join(dirs) or 'this repo'}")
        args = [str(exe), "scan", "--config", str(config), "--metrics=off", "--disable-version-check", "--quiet",
                "--json-output", str(json_path), "--sarif-output", str(sarif_path)]
        for s in SEVERITIES:
            args += ["--severity", s]
        if baseline:
            args += ["--baseline-commit", baseline]
        # Read rule files as UTF-8 whatever the locale.
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.run([*args, *targets], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, check=False)
        if proc.returncode not in (0, 1) or not json_path.is_file():
            tail = "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-5:])
            raise SastError(f"semgrep exited {proc.returncode}: {tail}")
        report = json.loads(json_path.read_text(encoding="utf-8"))
        findings = [
            Finding(
                rule=_rule_id(r.get("check_id", ""), config),
                path=r.get("path", ""),
                line=(r.get("start") or {}).get("line", 0),
                severity=(r.get("extra") or {}).get("severity", ""),
                message=((r.get("extra") or {}).get("message", "").strip().splitlines() or [""])[0],
            )
            for r in report.get("results") or []
        ]
    sarif.tag(sarif_path, "semgrep")
    return {
        "semgrep": version,
        "pinned": tools.SEMGREP_VERSION,
        "baseline": baseline,
        "rule_dirs": dirs,
        "rules": count,
        "findings": [asdict(f) for f in findings],
        "lines": [f.line_text() for f in findings],
        "errors": len(report.get("errors") or []),
        "json": (REPORTS / "semgrep.json").as_posix(),
        "sarif": (REPORTS / "semgrep.sarif").as_posix(),
    }
