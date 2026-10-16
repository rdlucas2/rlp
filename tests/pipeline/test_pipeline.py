"""The golden pipeline's gates, run through `rlp pipeline run` on fixture repos.

Each test copies tests/pipeline/fixtures/hello into a fresh git repo, applies
one change, and checks which stage the pipeline stops at. The package stage
needs Docker, so these tests run the gates up to coverage; the `golden-selftest`
workflow runs the full pipeline, package included, in GitHub Actions.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "hello"
UP_TO_COVERAGE = ["precommit", "validate", "policy", "setup", "lint", "test", "coverage"]
GIT_ENV = {
    "GIT_AUTHOR_NAME": "rlp-tests",
    "GIT_AUTHOR_EMAIL": "rlp-tests@example.invalid",
    "GIT_COMMITTER_NAME": "rlp-tests",
    "GIT_COMMITTER_EMAIL": "rlp-tests@example.invalid",
}
# A fake but realistic-looking AWS credential pair, stored reversed so this file
# never contains secret-shaped text (gitleaks also decodes base64 and hex): the
# self-test scans this repo's whole history.
SAMPLE_ID = "K2VUYTR5MLP3QXZ7AIKA"[::-1]
SAMPLE_VALUE = "4qK9eX2bG0sN5dJ3cH6mY1vR7pZ4kLw9Qf2TnXs8"[::-1]


def env(extra: dict | None = None) -> dict:
    """The test interpreter's directory first on PATH, so fixture verbs find `python` with pytest and coverage."""
    base = {**os.environ, **GIT_ENV, **(extra or {})}
    base["PATH"] = str(Path(sys.executable).parent) + os.pathsep + base.get("PATH", "")
    base.pop("RLP_OUTPUT", None)
    return base


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env())


def commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    dest = tmp_path / "hello"
    shutil.copytree(FIXTURE, dest)
    git(dest, "init", "-q")
    commit(dest, "fixture")
    return dest


def rlp(repo: Path, *args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rlp_cli", *args],
        cwd=repo, capture_output=True, text=True, encoding="utf-8", env=env(extra_env), check=False,
    )


def run_stages(repo: Path, stages=UP_TO_COVERAGE) -> tuple[int, dict]:
    args = ["pipeline", "run", "--reset", "-o", "json"]
    for s in stages:
        args += ["--stage", s]
    proc = rlp(repo, *args)
    return proc.returncode, json.loads(proc.stdout)


def statuses(report: dict) -> dict[str, str]:
    return {s["stage"]: s["status"] for s in report["stages"]}


def test_clean_repo_passes_every_gate_up_to_coverage(repo):
    code, report = run_stages(repo)
    assert code == 0, report
    assert statuses(report) == {s: "passed" for s in UP_TO_COVERAGE}


def test_failing_test_stops_at_test(repo):
    (repo / "tests" / "test_broken.py").write_text("def test_broken():\n    assert 1 == 2\n", encoding="utf-8")
    commit(repo, "break a test")
    code, report = run_stages(repo)
    assert code == 1
    assert list(statuses(report).items())[-1] == ("test", "failed")
    assert "coverage" not in statuses(report)


def test_coverage_drop_stops_at_coverage(repo):
    src = repo / "src" / "hello" / "__init__.py"
    src.write_text(src.read_text(encoding="utf-8") + "\n\ndef untested(x):\n    if x:\n        return 1\n    return 2\n", encoding="utf-8")
    commit(repo, "add untested code")
    code, report = run_stages(repo)
    assert code == 1
    stages = statuses(report)
    assert stages["test"] == "passed"
    assert stages["coverage"] == "failed"
    cov = report["stages"][-1]["data"]["results"][0]
    assert cov["percent"] < cov["minimum"] == 80


def test_injected_secret_stops_at_precommit(repo):
    (repo / "deploy.ini").write_text(f"aws_access_key_id = {SAMPLE_ID}\naws_secret_access_key = {SAMPLE_VALUE}\n", encoding="utf-8")
    commit(repo, "add credentials")
    code, report = run_stages(repo)
    assert code == 1
    assert statuses(report) == {"precommit": "failed"}
    files = {f["file"] for f in report["stages"][0]["data"]["findings"]}
    assert files == {"deploy.ini"}


def test_secret_removed_later_still_fails_from_history(repo):
    leak = repo / "deploy.ini"
    leak.write_text(f"aws_access_key_id = {SAMPLE_ID}\naws_secret_access_key = {SAMPLE_VALUE}\n", encoding="utf-8")
    commit(repo, "add credentials")
    leak.unlink()
    commit(repo, "remove credentials")
    code, report = run_stages(repo, ["precommit"])
    assert code == 1
    assert statuses(report) == {"precommit": "failed"}


def test_secret_outside_git_is_scanned_as_files(tmp_path):
    plain = tmp_path / "plain"
    shutil.copytree(FIXTURE, plain)
    (plain / "deploy.ini").write_text(f"aws_access_key_id = {SAMPLE_ID}\naws_secret_access_key = {SAMPLE_VALUE}\n", encoding="utf-8")
    proc = rlp(plain, "secrets", "scan", "-o", "json")
    assert proc.returncode == 1
    assert {f["file"] for f in json.loads(proc.stdout)["findings"]} == {"deploy.ini"}


def test_stages_run_separately_build_one_report(repo):
    for stage in ["validate", "policy"]:
        assert rlp(repo, "pipeline", "run", "--stage", stage).returncode == 0
    summary = rlp(repo, "pipeline", "summary")
    assert summary.returncode == 0
    assert "| validate | passed |" in summary.stdout
    assert "| policy | passed |" in summary.stdout
    assert "| lint | not run |" in summary.stdout
    recorded = json.loads(rlp(repo, "pipeline", "summary", "-o", "json").stdout)
    assert [s["stage"] for s in recorded["stages"]] == ["validate", "policy"]
    rlp(repo, "pipeline", "run", "--reset", "--stage", "validate")
    recorded = json.loads(rlp(repo, "pipeline", "summary", "-o", "json").stdout)
    assert [s["stage"] for s in recorded["stages"]] == ["validate"]


def test_summary_has_per_component_table(repo):
    run_stages(repo)
    out = rlp(repo, "pipeline", "summary").stdout
    assert "### Per component" in out
    assert "| api | passed | passed | passed | passed |" in out


def test_pending_and_deploy_stages_skip_with_reason(repo):
    proc = rlp(repo, "pipeline", "run", "--reset", "-s", "sonar", "-s", "trivy", "-s", "terraform", "-s", "deploy", "--deploy-local", "-o", "json")
    assert proc.returncode == 0
    report = json.loads(proc.stdout)
    assert statuses(report) == {"sonar": "skipped", "trivy": "skipped", "terraform": "skipped", "deploy": "skipped"}
    assert "LocalStack" in report["stages"][-1]["detail"]


def test_component_filter_rejects_unknown(repo):
    proc = rlp(repo, "pipeline", "run", "--reset", "-s", "lint", "--component", "web", "-o", "json")
    assert proc.returncode == 1
    assert "unknown component(s): web" in json.loads(proc.stdout)["stages"][0]["detail"]


def test_verbs_get_rlp_environment(repo):
    import yaml

    (repo / "probe.py").write_text(
        "import json, os\njson.dump({k: v for k, v in os.environ.items() if k.startswith('RLP_')}, open('env.json', 'w'))\n",
        encoding="utf-8",
    )
    doc = yaml.safe_load((repo / "rlp.yaml").read_text(encoding="utf-8"))
    doc["components"][0]["verbs"] = {"build": f'"{Path(sys.executable).as_posix()}" probe.py'}
    (repo / "rlp.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    proc = rlp(repo, "build", extra_env={"RLP_VERSION": "1.2.3"})
    assert proc.returncode == 0, proc.stderr
    seen = json.loads((repo / "env.json").read_text(encoding="utf-8"))
    assert seen == {"RLP_PROJECT": "hello", "RLP_COMPONENT": "api", "RLP_VERSION": "1.2.3", "RLP_IMAGE": "hello-api:1.2.3"}


def test_artifacts_use_git_commit_as_version(repo):
    sha = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    proc = rlp(repo, "artifacts", "-o", "json")
    assert json.loads(proc.stdout)["artifacts"] == [{"component": "api", "kind": "container-image", "version": sha, "image": f"hello-api:{sha}"}]


def fake_tool(bin_dir: Path, name: str) -> Path:
    """A stand-in for docker or gh that records its arguments."""
    log = bin_dir / f"{name}.log"
    script = bin_dir / name
    script.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return log


@pytest.mark.skipif(os.name == "nt", reason="fake docker/gh are POSIX shell scripts")
def test_publish_pushes_images_to_registry(repo, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = fake_tool(bin_dir, "docker")
    path_env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "RLP_VERSION": "abc123"}
    proc = rlp(repo, "publish", "--registry", "ghcr.io/rdlucas2/hello", "-o", "json", extra_env=path_env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert log.read_text().splitlines() == [
        "tag hello-api:abc123 ghcr.io/rdlucas2/hello/hello-api:abc123",
        "push ghcr.io/rdlucas2/hello/hello-api:abc123",
    ]


@pytest.mark.skipif(os.name == "nt", reason="fake docker/gh are POSIX shell scripts")
def test_publish_attaches_zips_to_release(repo, tmp_path):
    doc = (repo / "rlp.yaml").read_text(encoding="utf-8")
    doc = doc.replace("      kind: container-image\n", "      kind: zip\n      path: dist/hello.zip\n")
    doc = doc.replace("      kind: container-service\n      port: 8000\n      health:\n        liveness: /healthz\n", "      kind: function\n      handler: hello.handler\n")
    (repo / "rlp.yaml").write_text(doc, encoding="utf-8")
    (repo / "dist").mkdir()
    (repo / "dist" / "hello.zip").write_bytes(b"PK")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = fake_tool(bin_dir, "gh")
    path_env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    skipped = rlp(repo, "publish", "-o", "json", extra_env=path_env)
    assert json.loads(skipped.stdout)["published"][0]["status"] == "skipped"
    proc = rlp(repo, "publish", "--release", "v1.0.0", "-o", "json", extra_env=path_env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert log.read_text().split()[:3] == ["release", "upload", "v1.0.0"]


def test_coverage_parsers(tmp_path):
    from rlp_cli import coverage

    (tmp_path / "lcov.info").write_text("SF:a.js\nLF:10\nLH:7\nend_of_record\nSF:b.js\nLF:10\nLH:10\nend_of_record\n", encoding="utf-8")
    (tmp_path / "jacoco.xml").write_text('<report name="x"><counter type="LINE" missed="25" covered="75"/></report>', encoding="utf-8")
    (tmp_path / "opencover.xml").write_text('<CoverageSession><Summary sequenceCoverage="91.5"/></CoverageSession>', encoding="utf-8")
    (tmp_path / "cobertura.xml").write_text('<coverage line-rate="0.825"/>', encoding="utf-8")
    assert coverage.lcov(tmp_path / "lcov.info") == pytest.approx(85.0)
    assert coverage.jacoco(tmp_path / "jacoco.xml") == pytest.approx(75.0)
    assert coverage.opencover(tmp_path / "opencover.xml") == pytest.approx(91.5)
    assert coverage.cobertura(tmp_path / "cobertura.xml") == pytest.approx(82.5)


def test_coverage_minimum_defaults_to_platform_floor():
    from rlp_cli import coverage

    assert coverage.minimum({}) == coverage.platform_floor()
    assert coverage.minimum({"quality": {"coverage": {"minimum": 95}}}) == 95
