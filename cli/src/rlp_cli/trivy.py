"""Vulnerability scanning with the pinned Trivy.

Two targets: the repo's dependencies (`trivy fs`) and each container image the
package stage built (`trivy image`). The gate fails on any vulnerability at
HIGH or above that has a fix (`--ignore-unfixed`), unless an unexpired entry in
.rlp/vex.yaml covers it. Every scan leaves a JSON report and a SARIF report in
.rlp/pipeline/reports/ for code scanning and for agents.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from rlp_cli import artifacts, sarif, tools, vex

SEVERITIES = ("HIGH", "CRITICAL")
REPORTS = Path(".rlp") / "pipeline" / "reports"


class ScanError(Exception):
    """Trivy could not run, or a scan target is missing."""


@dataclass
class Finding:
    target: str  # what was scanned: "fs" or the image reference
    file: str  # Trivy's result target inside it, e.g. requirements.txt
    id: str
    package: str
    installed: str
    fixed: str
    severity: str
    title: str
    path: str = ""  # where the package was found, when Trivy knows (images)
    excepted_by: str | None = None  # the VEX reason when an exception covers it

    def line(self) -> str:
        return f"{self.id} ({self.severity}) in {self.package} {self.installed}, fixed in {self.fixed} [{self.target}: {self.path or self.file}]"


def evaluate(report: dict, target: str, exceptions: list[vex.Entry]) -> list[Finding]:
    """Fixable HIGH and CRITICAL findings in a Trivy JSON report; excepted ones carry excepted_by."""
    out = []
    for result in report.get("Results") or []:
        for v in result.get("Vulnerabilities") or []:
            if v.get("Severity") not in SEVERITIES or not v.get("FixedVersion"):
                continue
            f = Finding(
                target=target,
                file=result.get("Target", ""),
                id=v.get("VulnerabilityID", ""),
                package=v.get("PkgName", ""),
                installed=v.get("InstalledVersion", ""),
                fixed=v.get("FixedVersion", ""),
                severity=v["Severity"],
                title=v.get("Title", ""),
                path=v.get("PkgPath", ""),
            )
            cover = next((e for e in exceptions if e.covers(f.id, f.package)), None)
            if cover:
                f.excepted_by = cover.reason
            out.append(f)
    return out


def _run(args: list[str], cwd: Path) -> None:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=False)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-5:])
        raise ScanError(f"trivy {args[1]} exited {proc.returncode}: {tail}")


def scan(trivy: Path, root: Path, kind: str, ref: str, name: str) -> dict:
    """Run one scan; write <name>.json and <name>.sarif under REPORTS; return the JSON report."""
    out = root / REPORTS
    out.mkdir(parents=True, exist_ok=True)
    json_path, sarif_path = out / f"{name}.json", out / f"{name}.sarif"
    common = ["--quiet", "--no-progress", "--skip-version-check", "--disable-telemetry"]
    _run(
        [str(trivy), kind, *common, "--scanners", "vuln", "--severity", ",".join(SEVERITIES), "--ignore-unfixed",
         "--exit-code", "0", "--format", "json", "--output", str(json_path), ref],
        root,
    )
    _run([str(trivy), "convert", "--quiet", "--format", "sarif", "--output", str(sarif_path), str(json_path)], root)
    sarif.tag(sarif_path, name)
    return json.loads(json_path.read_text(encoding="utf-8"))


def image_exists(ref: str) -> bool:
    try:
        proc = subprocess.run(["docker", "image", "inspect", ref], capture_output=True, check=False)
    except OSError:
        return False
    return proc.returncode == 0


def check(root: Path, doc: dict, only: tuple[str, ...] = (), today: dt.date | None = None) -> dict:
    """Scan the repo and each built image. Returns {"scans": [...], "findings": [...], "blocking": int}."""
    try:
        trivy = tools.ensure_trivy()
    except tools.ToolError as err:
        raise ScanError(str(err)) from err
    exceptions = vex.active(vex.load(root), today)
    targets = [("fs", ".", "fs", "trivy-fs")]
    for entry in artifacts.resolve(root, doc):
        if entry["kind"] != "container-image" or (only and entry["component"] not in only):
            continue
        if not image_exists(entry["image"]):
            raise ScanError(f"image {entry['image']} for component {entry['component']} not found; run the package stage first")
        targets.append(("image", entry["image"], entry["image"], f"trivy-image-{entry['component']}"))
    scans, findings = [], []
    for kind, ref, label, name in targets:
        found = evaluate(scan(trivy, root, kind, ref, name), label, exceptions)
        scans.append({"target": label, "json": (REPORTS / f"{name}.json").as_posix(), "sarif": (REPORTS / f"{name}.sarif").as_posix()})
        findings += found
    return {
        "scans": scans,
        "findings": [asdict(f) for f in findings],
        "blocking": sum(1 for f in findings if f.excepted_by is None),
        "lines": [f.line() for f in findings if f.excepted_by is None],
    }
