"""The sonar and trivy stages, VEX exceptions and the gitleaks SARIF report.

Trivy's vulnerability database and SonarQube Cloud need the network and, for
Sonar, a token, so these tests stand in for the scanners: a fake `trivy`
replays a recorded report, and the Sonar scanner's exit code is stubbed. The
golden-selftest workflow runs the real Trivy against the fixture repo.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from rlp_cli import pipeline, sonar, tools, trivy, vex

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "hello"
VULNERABLE = HERE / "fixtures" / "trivy" / "vulnerable-fs.json"
CLEAN = {"SchemaVersion": 2, "ArtifactName": ".", "Results": [{"Target": "requirements-dev.txt", "Vulnerabilities": []}]}
TODAY = dt.date(2026, 10, 16)

FAKE_TRIVY = '''#!{python}
"""Stands in for trivy: scans write $FAKE_TRIVY_REPORT, convert writes a one-run SARIF."""
import os, shutil, sys
args = sys.argv[1:]
out = args[args.index("--output") + 1]
if args[0] == "convert":
    open(out, "w").write('{{"version": "2.1.0", "runs": [{{"tool": {{"driver": {{"name": "Trivy"}}}}, "results": []}}]}}')
else:
    with open(os.environ["FAKE_TRIVY_LOG"], "a") as log:
        log.write(args[0] + " " + args[-1] + "\\n")
    shutil.copyfile(os.environ["FAKE_TRIVY_REPORT"], out)
'''


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    dest = tmp_path / "hello"
    shutil.copytree(FIXTURE, dest)
    return dest


@pytest.fixture
def fake_trivy(tmp_path: Path, monkeypatch) -> dict:
    exe = tmp_path / "bin" / "trivy"
    exe.parent.mkdir()
    exe.write_text(FAKE_TRIVY.format(python=sys.executable), encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "trivy.log"
    log.write_text("", encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text(json.dumps(CLEAN), encoding="utf-8")
    monkeypatch.setattr(tools, "ensure_trivy", lambda dest=None: exe)
    monkeypatch.setattr(trivy, "image_exists", lambda ref: True)
    monkeypatch.setenv("FAKE_TRIVY_LOG", str(log))
    monkeypatch.setenv("FAKE_TRIVY_REPORT", str(report))
    return {"report": report, "log": log}


def write_vex(repo: Path, expires: str, component: str | None = None) -> None:
    entry = {"id": "CVE-2018-18074", "reason": "Redirects are never followed.", "expires": expires}
    if component:
        entry["component"] = component
    (repo / ".rlp").mkdir(exist_ok=True)
    (repo / ".rlp" / "vex.yaml").write_text(json.dumps({"exceptions": [entry]}), encoding="utf-8")


def run_stage(repo: Path, stage: str) -> pipeline.StageResult:
    [result] = pipeline.run(pipeline.Context(root=repo, stream=subprocess.DEVNULL), [stage], reset=True)
    return result


# --- trivy ---


def test_evaluate_keeps_only_fixable_high_and_critical():
    report = json.loads(VULNERABLE.read_text(encoding="utf-8"))
    findings = trivy.evaluate(report, "fs", [])
    assert [(f.id, f.package, f.fixed) for f in findings] == [("CVE-2018-18074", "requests", "2.20.0")]
    assert findings[0].line() == "CVE-2018-18074 (HIGH) in requests 2.19.1, fixed in 2.20.0 [fs: requirements.txt]"


def test_clean_scan_passes_and_scans_fs_and_image(repo, fake_trivy):
    result = run_stage(repo, "trivy")
    assert result.status == "passed", result.detail
    scanned = fake_trivy["log"].read_text(encoding="utf-8").split()
    assert scanned[:2] == ["fs", "."]
    assert scanned[2] == "image" and scanned[3].startswith("hello-api:")
    categories = set()
    for scan in result.data["scans"]:
        assert (repo / scan["json"]).is_file()
        doc = json.loads((repo / scan["sarif"]).read_text(encoding="utf-8"))
        categories |= {r["automationDetails"]["id"] for r in doc["runs"]}
    assert categories == {"rlp/trivy-fs/", "rlp/trivy-image-api/"}


def test_vulnerable_dependency_fails_with_readable_report(repo, fake_trivy):
    shutil.copyfile(VULNERABLE, fake_trivy["report"])
    result = run_stage(repo, "trivy")
    assert result.status == "failed"
    assert "CVE-2018-18074 (HIGH) in requests 2.19.1, fixed in 2.20.0" in result.detail
    assert result.data["blocking"] == 2  # the fs scan and the image scan both replay the report


def test_unexpired_vex_entry_lets_the_cve_pass(repo, fake_trivy):
    shutil.copyfile(VULNERABLE, fake_trivy["report"])
    write_vex(repo, "2099-12-31")
    result = run_stage(repo, "trivy")
    assert result.status == "passed", result.detail
    assert "covered by .rlp/vex.yaml" in result.detail
    assert {f["excepted_by"] for f in result.data["findings"]} == {"Redirects are never followed."}


def test_expired_vex_entry_no_longer_covers_the_cve(repo, fake_trivy):
    shutil.copyfile(VULNERABLE, fake_trivy["report"])
    write_vex(repo, "2020-01-31")
    assert run_stage(repo, "trivy").status == "failed"


def test_expired_vex_entry_fails_policy(repo):
    write_vex(repo, "2020-01-31")
    result = run_stage(repo, "policy")
    assert result.status == "failed"
    assert "RLP-L005" in result.detail


def test_vex_component_narrows_the_exception(repo, fake_trivy):
    shutil.copyfile(VULNERABLE, fake_trivy["report"])
    write_vex(repo, "2099-12-31", component="urllib3")
    assert run_stage(repo, "trivy").status == "failed"
    write_vex(repo, "2099-12-31", component="requests")
    assert run_stage(repo, "trivy").status == "passed"


def test_missing_image_fails_with_a_hint(repo, fake_trivy, monkeypatch):
    monkeypatch.setattr(trivy, "image_exists", lambda ref: False)
    result = run_stage(repo, "trivy")
    assert result.status == "failed"
    assert "run the package stage first" in result.detail


def test_vex_load_and_active(repo):
    write_vex(repo, "2026-10-16")
    [entry] = vex.load(repo)
    assert entry.active(TODAY) and not entry.active(TODAY + dt.timedelta(days=1))
    assert vex.active([entry], TODAY) == [entry]
    assert entry.covers("CVE-2018-18074", "anything")


def test_vex_rejects_non_list(repo):
    (repo / ".rlp").mkdir()
    (repo / ".rlp" / "vex.yaml").write_text("exceptions: nope\n", encoding="utf-8")
    with pytest.raises(vex.VexError):
        vex.load(repo)


# --- sonar ---


def doc_with(fmt: str, path: str = "reports/cov") -> dict:
    return {
        "metadata": {"name": "hello", "project": "hello"},
        "components": [
            {"name": "api", "path": "services/api", "reports": {"coverage": {"format": fmt, "path": path}}},
            {"name": "web", "path": "web"},
        ],
    }


@pytest.mark.parametrize(
    "fmt, prop",
    [
        ("cobertura", "sonar.python.coverage.reportPaths"),
        ("lcov", "sonar.javascript.lcov.reportPaths"),
        ("jacoco", "sonar.coverage.jacoco.xmlReportPaths"),
        ("opencover", "sonar.cs.opencover.reportsPaths"),
    ],
)
def test_coverage_format_maps_to_its_sonar_property(tmp_path, fmt, prop):
    props = sonar.properties(tmp_path, doc_with(fmt), env={"GITHUB_REPOSITORY": "rdlucas2/hello"})
    assert props[prop] == "services/api/reports/cov"
    assert props["sonar.sources"] == "services/api,web"
    assert props["sonar.projectKey"] == "rdlucas2_hello"
    assert props["sonar.organization"] == "rdlucas2"
    assert props["sonar.qualitygate.wait"] == "true"


def test_self_hosted_sonar_needs_no_organization(tmp_path):
    props = sonar.properties(tmp_path, doc_with("cobertura"), env={"SONAR_HOST_URL": "http://localhost:9000"})
    assert "sonar.organization" not in props
    assert props["sonar.projectKey"] == "hello"


def test_sonarcloud_without_organization_is_an_error(tmp_path):
    with pytest.raises(sonar.SonarError, match="SONAR_ORGANIZATION"):
        sonar.properties(tmp_path, doc_with("cobertura"), env={})


def test_sonar_skips_without_token(repo, monkeypatch):
    monkeypatch.delenv("SONAR_TOKEN", raising=False)
    result = run_stage(repo, "sonar")
    assert result.status == "skipped"
    assert "SONAR_TOKEN" in result.detail


@pytest.mark.parametrize("code, status", [(0, "passed"), (1, "failed")])
def test_sonar_gate_follows_the_scanner(repo, monkeypatch, code, status):
    monkeypatch.setenv("SONAR_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "rdlucas2/hello")
    monkeypatch.setattr(sonar, "analyse", lambda root, doc, stream=None: code)
    result = run_stage(repo, "sonar")
    assert result.status == status
    assert result.data["properties"]["sonar.python.coverage.reportPaths"] == "reports/coverage.xml"


def test_sonar_without_scanner_fails(repo, monkeypatch):
    monkeypatch.setenv("SONAR_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "rdlucas2/hello")
    monkeypatch.setattr(shutil, "which", lambda name, path=None: None)
    result = run_stage(repo, "sonar")
    assert result.status == "failed"
    assert "pysonar is not installed" in result.detail


# --- gitleaks SARIF ---


def test_precommit_writes_gitleaks_sarif(tmp_path):
    plain = tmp_path / "plain"
    shutil.copytree(FIXTURE, plain)
    result = run_stage(plain, "precommit")
    assert result.status == "passed", result.detail
    sarif = json.loads((plain / pipeline.REPORTS / "gitleaks.sarif").read_text(encoding="utf-8"))
    assert sarif["runs"][0]["tool"]["driver"]["name"] == "gitleaks"
    assert sarif["runs"][0]["automationDetails"]["id"] == "rlp/gitleaks/"
