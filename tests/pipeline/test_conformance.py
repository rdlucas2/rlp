"""Template conformance: `rlp template certify` on a small Copier template built from the hello fixture.

The package, trivy, sast, run and smoke gates need Docker or Semgrep, so the
subprocess tests skip them (which leaves the template uncertified) and check
the gates that ran. The in-process tests stub the gates to check the
certification rules: stop at the first failure, always take down what run
started, certify only when nothing failed or was skipped by the caller.
The template-conformance workflow runs every gate in GitHub Actions.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from rlp_cli import certify, runtime

from test_pipeline import FIXTURE, commit, git, rlp

NO_DOCKER = "sast,package,trivy,run,smoke"
TEMPLATE_YAML = """\
apiVersion: rlp/v1alpha1
kind: Template
metadata:
  name: hello
  version: 0.3.0
  description: Test template rendered from the pipeline fixture.
produces:
  components:
    - language: python
      artifact: container-image
      runtime: container-service
conformance:
  answers: tests/default-answers.yml
  verbs: [setup, lint, test, build, package, run, smoke]
"""


@pytest.fixture
def template(tmp_path: Path) -> Path:
    root = tmp_path / "tpl"
    shutil.copytree(FIXTURE, root / "template")
    rlp_yaml = (root / "template" / "rlp.yaml").read_text(encoding="utf-8")
    rlp_yaml = rlp_yaml.replace("name: hello", "name: {{ project_id }}").replace("minimum: 80", "minimum: {{ coverage_minimum }}")
    (root / "template" / "rlp.yaml").unlink()
    (root / "template" / "rlp.yaml.jinja").write_text(rlp_yaml, encoding="utf-8")
    (root / "copier.yml").write_text(
        "_subdirectory: template\n"
        "project_id:\n  type: str\n  default: hello\n"
        "coverage_minimum:\n  type: int\n  default: 80\n",
        encoding="utf-8",
    )
    (root / "rlp-template.yaml").write_text(TEMPLATE_YAML, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "default-answers.yml").write_text("_src_path: ignored\nproject_id: greeter\ncoverage_minimum: 80\n", encoding="utf-8")
    git(root, "init", "-q")
    commit(root, "template")
    return root


def certify_cli(template: Path, *extra: str) -> tuple[int, dict]:
    proc = rlp(template.parent, "template", "certify", str(template), "--skip", NO_DOCKER, "-o", "json", *extra)
    assert proc.stdout, proc.stderr
    return proc.returncode, json.loads(proc.stdout)


def statuses(report: dict) -> dict[str, str]:
    return {g["gate"]: g["status"] for g in report["gates"]}


def test_renders_with_answers_and_runs_the_gates(template, tmp_path):
    out = tmp_path / "report" / "conformance.json"
    code, report = certify_cli(template, "--report", str(out), "--keep")
    s = statuses(report)
    assert [k for k, v in s.items() if v == "passed"] == ["template", "render", "precommit", "validate", "policy", "setup", "lint", "test", "coverage", "build"]
    assert s["sonar"] == "skipped" and s["down"] == "skipped"
    assert all(s[g] == "skipped" for g in NO_DOCKER.split(","))
    # Skipping gates on purpose never certifies.
    assert code == 1 and report["certified"] is False and report["ok"] is False
    assert report["template"] == "hello" and report["version"] == "0.3.0" and report["contract"] == "rlp/v1alpha1"
    assert report["commit"] and len(report["commit"]) == 40 and report["ref"] == "HEAD"
    rendered = Path(report["rendered"])
    assert "name: greeter" in (rendered / "rlp.yaml").read_text(encoding="utf-8")
    assert json.loads(out.read_text(encoding="utf-8"))["gates"] == report["gates"]


def test_rendered_repo_is_removed_without_keep(template):
    _, report = certify_cli(template, "--skip", "precommit,setup,lint,test,coverage,build")
    assert statuses(report)["render"] == "passed"
    assert not Path(report["rendered"]).exists()


def test_uncommitted_template_changes_are_certified(template):
    (template / "template" / "tests" / "test_broken.py").write_text("def test_broken():\n    assert 1 == 2\n", encoding="utf-8")
    code, report = certify_cli(template)
    s = statuses(report)
    assert code == 1 and s["test"] == "failed"
    assert s["coverage"] == "not run" and s["build"] == "not run"


def test_broken_verb_fails_certification(template):
    taskfile = template / "template" / "Taskfile.yml"
    taskfile.write_text(taskfile.read_text(encoding="utf-8").replace('echo "nothing to compile"', "python -c 'raise SystemExit(4)'"), encoding="utf-8")
    commit(template, "break build")
    code, report = certify_cli(template)
    s = statuses(report)
    assert code == 1 and s["build"] == "failed"
    assert "build api exited" in next(g["detail"] for g in report["gates"] if g["gate"] == "build")


def test_coverage_drop_fails_certification(template):
    (template / "template" / "src" / "hello" / "extra.py").write_text(
        "".join(f"def f{i}(x):\n    if x:\n        return {i}\n    return -{i}\n\n\n" for i in range(40)),
        encoding="utf-8",
    )
    commit(template, "untested code")
    code, report = certify_cli(template)
    s = statuses(report)
    assert code == 1 and s["test"] == "passed" and s["coverage"] == "failed"


def test_missing_answers_file_fails_the_template_gate(template):
    (template / "tests" / "default-answers.yml").unlink()
    commit(template, "drop answers")
    code, report = certify_cli(template)
    s = statuses(report)
    assert code == 1 and s["template"] == "failed" and s["render"] == "not run"


def test_invalid_template_contract_fails(template):
    (template / "rlp-template.yaml").write_text(TEMPLATE_YAML.replace("version: 0.3.0", "version: three"), encoding="utf-8")
    code, report = certify_cli(template)
    assert code == 1 and statuses(report)["template"] == "failed"


def test_dest_must_be_empty(template, tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "stale.txt").write_text("x", encoding="utf-8")
    code, report = certify_cli(template, "--dest", str(dest))
    assert code == 1 and statuses(report)["render"] == "failed"


def test_not_a_template_and_unknown_gate_are_usage_errors(template, tmp_path):
    proc = rlp(tmp_path, "template", "certify", str(tmp_path), "-o", "json")
    assert proc.returncode == 2 and "no rlp-template.yaml" in json.loads(proc.stdout)["error"]
    proc = rlp(tmp_path, "template", "certify", str(template), "--skip", "deploy", "-o", "json")
    assert proc.returncode == 2 and "unknown gate" in json.loads(proc.stdout)["error"]


def test_summary_renders_markdown(template, tmp_path):
    out = tmp_path / "conformance.json"
    certify_cli(template, "--report", str(out), "--skip", "precommit,setup,lint,test,coverage,build")
    proc = rlp(tmp_path, "template", "summary", str(out))
    assert proc.returncode == 0
    assert "hello 0.3.0 is **not certified**" in proc.stdout
    assert "| render | passed |" in proc.stdout


# --- certification rules, with the gates stubbed ---


@pytest.fixture
def stubbed(monkeypatch, template, tmp_path):
    """certify() in-process with every pipeline gate and the Docker runtime replaced."""
    calls: list[str] = []
    outcome: dict[str, str] = {}

    def pipeline_gate(ctx, name):
        calls.append(name)
        return certify.Gate(name, outcome.get(name, "passed"), "stubbed")

    class FakePlan:
        project = "greeter"
        endpoints: list = []

    monkeypatch.setattr(certify, "pipeline_gate", pipeline_gate)
    monkeypatch.setattr(certify, "run_extra_verbs", lambda dest, names, stream: certify.Gate("build", outcome.get("build", "passed")))
    monkeypatch.setattr(runtime, "plan", lambda *a, **k: FakePlan())
    monkeypatch.setattr(runtime, "write", lambda *a, **k: [])
    monkeypatch.setattr(runtime, "up", lambda *a, **k: calls.append("up") or (1 if outcome.get("run") == "failed" else 0))

    def smoke(*a, **k):
        calls.append("smoke")
        return [runtime.Check("api", "http", "http://127.0.0.1:8000/healthz", outcome.get("smoke") != "failed", "200")]

    monkeypatch.setattr(runtime, "smoke", smoke)
    monkeypatch.setattr(runtime, "down", lambda *a, **k: calls.append("down") or 0)

    def go(**opts):
        return certify.certify(certify.Options(template=template, dest=tmp_path / "rendered", **opts))

    return go, calls, outcome


def test_all_gates_pass_certifies(stubbed):
    go, calls, _ = stubbed
    report = go()
    assert report.certified is True
    assert [g.gate for g in report.gates] == certify.GATES
    assert calls[-3:] == ["up", "smoke", "down"]


def test_smoke_failure_still_takes_down_and_fails(stubbed):
    go, calls, outcome = stubbed
    outcome["smoke"] = "failed"
    report = go()
    s = {g.gate: g.status for g in report.gates}
    assert report.certified is False
    assert s["smoke"] == "failed" and s["down"] == "passed"
    assert calls[-1] == "down"


def test_failed_gate_stops_the_sequence(stubbed):
    go, calls, outcome = stubbed
    outcome["trivy"] = "failed"
    report = go()
    s = {g.gate: g.status for g in report.gates}
    assert report.certified is False
    assert s["trivy"] == "failed" and s["build"] == "not run" and s["run"] == "not run"
    assert s["down"] == "not run" and "up" not in calls


def test_template_without_run_in_conformance_verbs_skips_the_runtime(stubbed, template):
    (template / "rlp-template.yaml").write_text(TEMPLATE_YAML.replace("[setup, lint, test, build, package, run, smoke]", "[setup, lint, test]"), encoding="utf-8")
    go, calls, _ = stubbed
    report = go()
    s = {g.gate: g.status for g in report.gates}
    assert report.certified is True
    assert s["build"] == "skipped" and s["run"] == "skipped" and s["smoke"] == "skipped"
    assert "up" not in calls


def test_smoke_without_run_is_skipped_not_crashed(stubbed):
    go, calls, _ = stubbed
    report = go(skip=("run",))
    s = {g.gate: g.status for g in report.gates}
    assert report.certified is False
    assert s["run"] == "skipped" and s["smoke"] == "skipped" and s["down"] == "skipped"
    assert "up" not in calls and "smoke" not in calls


def test_temporary_render_dir_is_cleaned_up_when_the_template_gate_fails(template):
    (template / "tests" / "default-answers.yml").unlink()
    code, report = certify_cli(template)
    assert code == 1 and report["rendered"] and not Path(report["rendered"]).exists()
