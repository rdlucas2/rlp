"""The vulnerability loop (#49): rlp loop vuln.

The seeded cases under tests/loops/vuln/ run end to end on a copy of the
pipeline fixture repo: the real setup and test stages, a package verb that
builds nothing (no Docker daemon in the tests), a fake `trivy` that reports
each case's vulnerability while its pattern is still in a file (the
golden-selftest workflow runs the real one), and the fake agent provider with
each case's script. No test calls a real model.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from rlp_cli import agent, escalate, loop, loop_pr, pipeline, resources, tools, trivy, vuln

REPO = Path(__file__).resolve().parents[2]
HELLO = REPO / "tests" / "pipeline" / "fixtures" / "hello"
CASES = REPO / "tests" / "loops" / "vuln"
WORKFLOW = REPO / ".github" / "workflows" / "loop-vuln.yml"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "rlp-tests",
    "GIT_AUTHOR_EMAIL": "rlp-tests@example.invalid",
    "GIT_COMMITTER_NAME": "rlp-tests",
    "GIT_COMMITTER_EMAIL": "rlp-tests@example.invalid",
}
GATES = ("setup", "test", "package", "trivy")

FAKE_TRIVY = '''
"""Stands in for trivy: reports each rule in $FAKE_TRIVY_RULES whose pattern is still in its file."""
import json, os, sys
args = sys.argv[1:]
out = args[args.index("--output") + 1]
if args[0] == "convert":
    open(out, "w").write('{"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "Trivy"}}, "results": []}]}')
    sys.exit(0)
results = {}
for rule in json.loads(os.environ["FAKE_TRIVY_RULES"]):
    if rule["scan"] != args[0]:
        continue
    try:
        text = open(rule["file"], encoding="utf-8").read()
    except OSError:
        continue
    if rule["pattern"] in text:
        r = results.setdefault(rule["target"], {"Target": rule["target"], "Class": rule["class"], "Vulnerabilities": []})
        r["Vulnerabilities"].append(rule["vulnerability"])
json.dump({"SchemaVersion": 2, "Results": list(results.values())}, open(out, "w"))
'''


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env).stdout.strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def load_case(name: str) -> dict:
    return yaml.safe_load((CASES / name / "case.yaml").read_text(encoding="utf-8"))


def seed(repo: Path, name: str) -> None:
    """Copy a case's seed/ files into the repo, dropping the .fixture suffix."""
    src = CASES / name / "seed"
    for path in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = path.relative_to(src)
        target = repo / rel.parent / rel.name.removesuffix(".fixture")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


@pytest.fixture
def fake_trivy(tmp_path: Path, monkeypatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "fake_trivy.py"
    script.write_text(f"#!{sys.executable}\n{FAKE_TRIVY}", encoding="utf-8")
    if sys.platform == "win32":  # Windows runs .cmd files, not shebang scripts
        exe = bin_dir / "trivy.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = script
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(tools, "ensure_trivy", lambda *a, **k: exe)
    monkeypatch.setattr(trivy, "image_exists", lambda ref: True)  # the package verb below builds nothing
    monkeypatch.setenv("FAKE_TRIVY_RULES", "[]")


@pytest.fixture
def fake(monkeypatch) -> agent.FakeProvider:
    provider = agent.FakeProvider()
    monkeypatch.setitem(agent.PROVIDERS, "fake", lambda env: provider)
    return provider


def build_wheel(folder: Path, name: str, version: str) -> None:
    """A minimal pure-Python wheel, byte-for-byte the same every time, so uv can resolve offline."""
    dist = name.replace("-", "_")
    info = f"{dist}-{version}.dist-info"
    files = {
        f"{dist}/__init__.py": f'__version__ = "{version}"\n',
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: rlp-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = []
    for path, text in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=").decode()
        record.append(f"{path},sha256={digest},{len(text.encode())}")
    files[f"{info}/RECORD"] = "\n".join([*record, f"{info}/RECORD,,"]) + "\n"
    folder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(folder / f"{dist}-{version}-py3-none-any.whl", "w") as whl:
        for path, text in files.items():
            whl.writestr(zipfile.ZipInfo(path, (2020, 1, 1, 0, 0, 0)), text)


def make_repo(tmp_path: Path, monkeypatch, case: str | None, loops: dict | None = None) -> Path:
    """The pipeline fixture repo with loops set, a package verb that needs no Docker, a baseline commit, then the case's seed commit."""
    repo = tmp_path / "hello"
    shutil.copytree(HELLO, repo)
    doc = yaml.safe_load((repo / "rlp.yaml").read_text(encoding="utf-8"))
    doc["components"][0]["verbs"] = {"package": "python -c \"print('image built')\"", **((load_case(case).get("verbs") or {}) if case else {})}
    if loops is not None:
        doc["loops"] = loops
    (repo / "rlp.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "baseline")
    if case:
        seed(repo, case)
        for name, versions in (load_case(case).get("wheels") or {}).items():
            for version in versions:
                build_wheel(repo / "wheels", name, version)
        commit(repo, f"seed {case}")
        monkeypatch.setenv("FAKE_TRIVY_RULES", json.dumps(load_case(case)["trivy"]))
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("RLP_VERSION", raising=False)
    for name in ("user.name", "user.email"):
        git(repo, "config", name, GIT_ENV["GIT_AUTHOR_NAME"] if name == "user.name" else GIT_ENV["GIT_AUTHOR_EMAIL"])
    return repo


def run_gates(repo: Path) -> dict[str, str]:
    """What the loop workflow does first: each verify stage on its own so every failure is recorded."""
    ctx = pipeline.Context(root=repo, stream=subprocess.DEVNULL)
    statuses = {}
    for i, stage in enumerate(GATES):
        [r] = pipeline.run(ctx, [stage], reset=i == 0)
        statuses[stage] = r.status
    return statuses


def run_loop(repo: Path, **kw) -> loop.Report:
    env = kw.pop("env", {})
    return loop.run(repo, "vuln", loop.Options(provider="fake", env=env, **kw), stream=subprocess.DEVNULL)


def validate_state(path: Path) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    errors = [f"{list(e.absolute_path)}: {e.message}" for e in Draft202012Validator(resources.schema("LoopState")).iter_errors(doc)]
    assert errors == []
    return doc


def script(case: str) -> list[agent.FakeStep]:
    raw = json.loads((CASES / case / "agent.json").read_text(encoding="utf-8"))
    return [agent.FakeStep(files=s["files"], summary=s["summary"], cost_usd=s["costUsd"]) for s in raw]


def rlp_doc(repo: Path) -> dict:
    return yaml.safe_load((repo / "rlp.yaml").read_text(encoding="utf-8"))


# --- the seeded cases, end to end ---


@pytest.mark.parametrize("case", ["patch", "base-image"])
def test_seeded_vulnerability_is_fixed_and_the_pr_is_green(tmp_path, monkeypatch, fake_trivy, fake, case):
    expect = load_case(case)["expect"]
    repo = make_repo(tmp_path, monkeypatch, case, loops={"enabled": True})
    statuses = run_gates(repo)
    assert statuses == {"setup": "passed", "test": "passed", "package": "passed", "trivy": "failed"}
    planned = script(case)
    fake.steps = list(planned)
    report = run_loop(repo, max_iterations=3, max_spend_usd=5)
    assert report.outcome == expect["outcome"] == "fixed", report.reason
    state = validate_state(repo / report.state_file)
    assert len(state["iterations"]) == expect["iterations"]
    assert [g["stage"] for g in state["iterations"][-1]["gates"]] == list(GATES)
    assert all(g["status"] == "passed" for g in state["iterations"][-1]["gates"])
    [finding] = state["findings"]
    rule = load_case(case)["trivy"][0]["vulnerability"]
    assert (finding["rule"], finding["package"], finding["installed"], finding["fixed"]) == (
        rule["VulnerabilityID"], rule["PkgName"], rule["InstalledVersion"], rule["FixedVersion"])
    # the fix lives on the loop branch only, and never touches .rlp/vex.yaml
    branch = state["branch"]
    assert git(repo, "symbolic-ref", "--short", "HEAD") == "main"
    changed = git(repo, "diff", "--name-only", f"main..{branch}").splitlines()
    assert changed == sorted(planned[0].files) and ".rlp/vex.yaml" not in changed
    fixed = loop_pr.load_fixed(repo / report.state_file)
    body = loop_pr.render(fixed, "https://example.invalid/run/1")
    assert loop_pr.labels(fixed) == ["agent:authored", "loop:vuln"]
    assert "| trivy | passed |" in body and "| package | passed |" in body
    assert rule["VulnerabilityID"] in body


def test_patch_finding_reaches_the_prompt_grouped_by_manifest(tmp_path, monkeypatch, fake_trivy, fake):
    repo = make_repo(tmp_path, monkeypatch, "patch", loops={"enabled": True})
    run_gates(repo)
    fake.steps = script("patch")
    run_loop(repo)
    prompt = fake.prompts[0]
    assert "## requirements.txt\n\n- cryptography 42.0.0: CVE-2024-26130 (HIGH), fixed in 42.0.4 [fs]" in prompt
    assert "Change only these files: Dockerfile, requirements-dev.txt, requirements.txt" in prompt
    assert ".rlp/vex.yaml" in prompt and "within its current major version" in prompt


def test_base_image_finding_points_at_the_dockerfile(tmp_path, monkeypatch, fake_trivy, fake):
    repo = make_repo(tmp_path, monkeypatch, "base-image", loops={"enabled": True})
    run_gates(repo)
    [finding] = vuln.discover(repo, rlp_doc(repo))
    assert (finding["path"], finding["source"], finding["target"]) == ("Dockerfile", "base-image", "image api")
    assert finding["direct"] is False
    fake.steps = script("base-image")
    run_loop(repo)
    assert "## Dockerfile (base image)\n\n- libexpat1 2.5.0-1: CVE-2024-45491 (CRITICAL)" in fake.prompts[0]


def test_major_bump_escalates_before_calling_the_agent_and_opens_no_pr(tmp_path, monkeypatch, fake_trivy, fake):
    expect = load_case("major")["expect"]
    repo = make_repo(tmp_path, monkeypatch, "major", loops={"enabled": True})
    assert run_gates(repo)["trivy"] == "failed"
    fake.steps = script("major")
    report = run_loop(repo)
    assert report.outcome == expect["outcome"] == "escalated" and report.exit_code == 1
    assert "major-version bump of a direct dependency" in report.reason and "pyjwt 1.7.1 -> 2.4.0" in report.reason
    state = validate_state(repo / report.state_file)
    assert len(state["iterations"]) == expect["iterations"] and fake.prompts == []
    assert state["branch"] == "" and git(repo, "branch", "--list", "rlp/loop/*") == ""
    with pytest.raises(loop_pr.PullRequestError, match="only a fixed run"):
        loop_pr.load_fixed(repo / report.state_file)
    body = escalate.render(escalate.load_state(repo / report.state_file))
    assert "major-version bump" in body and "None: the loop stopped before calling the agent." in body


def test_a_green_major_base_image_bump_escalates_instead_of_opening_a_pr(tmp_path, monkeypatch, fake_trivy, fake):
    repo = make_repo(tmp_path, monkeypatch, "base-image", loops={"enabled": True})
    run_gates(repo)
    dockerfile = (repo / "Dockerfile").read_text(encoding="utf-8").replace("python:3.13.0-slim", "python:4.0-slim")
    fake.steps = [agent.FakeStep(files={"Dockerfile": dockerfile}, summary="moved to python 4", cost_usd=0.1)]
    report = run_loop(repo)
    assert report.outcome == "escalated", report.reason
    assert "base image python 3.13.0-slim -> 4.0-slim in Dockerfile" in report.reason
    state = validate_state(repo / report.state_file)
    assert all(g["status"] == "passed" for g in state["iterations"][-1]["gates"])
    with pytest.raises(loop_pr.PullRequestError, match="only a fixed run"):
        loop_pr.load_fixed(repo / report.state_file)


def test_an_attempt_that_adds_a_vex_exception_is_thrown_away(tmp_path, monkeypatch, fake_trivy, fake):
    repo = make_repo(tmp_path, monkeypatch, "patch", loops={"enabled": True})
    run_gates(repo)
    exception = "exceptions:\n  - id: CVE-2024-26130\n    reason: not reachable\n    expires: '2099-01-01'\n"
    fake.steps = [agent.FakeStep(files={".rlp/vex.yaml": exception}, summary="excepted it", cost_usd=0.05), *script("patch")]
    report = run_loop(repo)
    assert report.outcome == "fixed", report.reason
    first, second = validate_state(repo / report.state_file)["iterations"]
    assert ".rlp/vex.yaml" in first["rejected"] and first["commit"] == ""
    assert second["rejected"] == "" and second["commit"]
    assert not (repo / ".rlp" / "vex.yaml").exists()
    assert "thrown away: it changed files outside the allowed paths: .rlp/vex.yaml" in fake.prompts[1]


@pytest.mark.parametrize(("loops", "env"), [(None, {}), ({"enabled": True}, {"RLP_LOOPS": "off"}), ({"enabled": True, "kinds": ["quality"]}, {})])
def test_a_failing_gate_starts_no_run_when_loops_are_off(tmp_path, monkeypatch, fake_trivy, fake, loops, env):
    repo = make_repo(tmp_path, monkeypatch, "patch", loops=loops)
    run_gates(repo)
    fake.steps = script("patch")
    report = run_loop(repo, env=env)
    assert report.outcome == "disabled" and report.exit_code == 0
    assert fake.prompts == []
    assert not (repo / ".rlp" / "loops").exists()
    assert git(repo, "branch", "--list", "rlp/loop/*") == ""


def test_green_gates_have_no_findings(tmp_path, monkeypatch, fake_trivy, fake):
    repo = make_repo(tmp_path, monkeypatch, None, loops={"enabled": True})
    assert set(run_gates(repo).values()) == {"passed"}
    report = run_loop(repo)
    assert report.outcome == "no-findings" and fake.prompts == []


# --- regenerating lockfiles (#80) ---


@pytest.fixture
def uv_case(monkeypatch) -> None:
    """The seeded uv case runs the real uv, offline, against stand-in wheels. `task test` installs uv with the test extras."""
    if shutil.which("uv", path=str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")) is None:
        pytest.skip("uv is not installed (it comes with the test extras: pip install -e '.[test]')")
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("UV_PYTHON_DOWNLOADS", "never")
    monkeypatch.setenv("UV_PYTHON", sys.executable)


PYPROJECT_FIX = json.loads((CASES / "uv-lock" / "agent.json").read_text(encoding="utf-8"))[0]["files"]["pyproject.toml"]


def test_seeded_uv_case_regenerates_the_lockfile_and_ends_fixed(tmp_path, monkeypatch, fake_trivy, fake, uv_case):
    expect = load_case("uv-lock")["expect"]
    repo = make_repo(tmp_path, monkeypatch, "uv-lock", loops={"enabled": True})
    assert run_gates(repo) == {"setup": "passed", "test": "passed", "package": "passed", "trivy": "failed"}
    [finding] = vuln.discover(repo, rlp_doc(repo))
    assert (finding["path"], finding["lock"]) == ("uv.lock", "uv lock")
    fake.steps = script("uv-lock")
    report = run_loop(repo)
    assert report.outcome == expect["outcome"] == "fixed", report.reason
    state = validate_state(repo / report.state_file)
    assert len(state["iterations"]) == expect["iterations"]
    [it] = state["iterations"]
    assert it["agent"]["changedFiles"] == ["pyproject.toml"]
    assert it["afterAttempt"] == [{"component": "api", "command": "uv lock", "exitCode": 0, "files": ["uv.lock"]}]
    assert {g["stage"]: g["status"] for g in it["gates"]} == dict.fromkeys(GATES, "passed")
    # uv sync --locked passed in setup, and the regenerated lockfile is in the iteration's commit
    assert git(repo, "show", "--name-only", "--format=", it["commit"]).splitlines() == ["pyproject.toml", "uv.lock"]
    lock = git(repo, "show", f"{state['branch']}:uv.lock")
    assert 'name = "cryptography"\nversion = "42.0.4"' in lock and "42.0.0" not in lock
    prompt = fake.prompts[0]
    assert "After your change the loop runs `uv lock` in the repo root" in prompt and "not by editing the lockfile" in prompt


def test_a_failing_lock_command_throws_the_attempt_away_and_reaches_the_next_prompt(tmp_path, monkeypatch, fake_trivy, fake, uv_case):
    repo = make_repo(tmp_path, monkeypatch, "uv-lock", loops={"enabled": True})
    run_gates(repo)
    unresolvable = PYPROJECT_FIX.replace("42.0.4", "99.0")
    fake.steps = [agent.FakeStep(files={"pyproject.toml": unresolvable}, summary="pinned 99.0", cost_usd=0.05), *script("uv-lock")]
    report = run_loop(repo)
    assert report.outcome == "fixed", report.reason
    first, second = validate_state(repo / report.state_file)["iterations"]
    assert first["rejected"].startswith("the lock command `uv lock` for component api failed (exit 1)")
    assert first["commit"] == "" and first["gates"] == [] and first["afterAttempt"][0]["exitCode"] == 1
    assert second["rejected"] == "" and second["commit"]
    assert "thrown away: the lock command `uv lock` for component api failed (exit 1)" in fake.prompts[1]
    assert "```text" in fake.prompts[1] and "cryptography==99.0" in fake.prompts[1]
    # the thrown-away attempt left nothing behind
    assert git(repo, "show", f"{validate_state(repo / report.state_file)['branch']}~1:pyproject.toml") == git(repo, "show", "main:pyproject.toml")


def test_a_regenerated_lockfile_that_moves_a_direct_dependency_a_major_escalates(tmp_path, monkeypatch, fake_trivy, fake, uv_case):
    repo = make_repo(tmp_path, monkeypatch, "uv-lock", loops={"enabled": True})
    run_gates(repo)
    loose = PYPROJECT_FIX.replace("==42.0.4", ">=42.0.4")  # uv lock picks the newest match, 43.0.1
    fake.steps = [agent.FakeStep(files={"pyproject.toml": loose}, summary="loosened the pin", cost_usd=0.05)]
    report = run_loop(repo)
    assert report.outcome == "escalated", report.reason
    assert "dependency cryptography 42.0.0 -> 43.0.1 in uv.lock" in report.reason
    state = validate_state(repo / report.state_file)
    assert all(g["status"] == "passed" for g in state["iterations"][-1]["gates"])


def lock_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    return tracked(tmp_path, {"Dockerfile": "FROM python:3.13-slim\n", **files})


def test_lock_commands_come_from_rlp_yaml_or_a_tracked_uv_lock(tmp_path):
    root = lock_repo(tmp_path, {"pyproject.toml": "x\n", "uv.lock": "x\n", "web/package.json": "{}\n", "job/requirements.txt": "a==1\n"})
    doc = {"components": [{"name": "api"}, {"name": "web", "path": "web", "lock": "npm install --package-lock-only"}, {"name": "job", "path": "job"}]}
    assert vuln.lock_commands(root, doc) == [
        {"component": "api", "path": ".", "command": "uv lock"},
        {"component": "web", "path": "web", "command": "npm install --package-lock-only"},
    ]
    doc["components"][0]["lock"] = "uv lock --upgrade-package cryptography"
    assert vuln.lock_commands(root, doc)[0]["command"] == "uv lock --upgrade-package cryptography"


def python_command(code: str) -> str:
    """A lock command line that runs Python code; quoted so it splits the same way on every OS."""
    return shlex.join([sys.executable, "-c", code])


def lock_doc(command: str) -> dict:
    return {"components": [{"name": "api", "lock": command}]}


def test_the_lock_command_runs_only_when_a_root_manifest_changed(tmp_path):
    root = lock_repo(tmp_path, {"requirements.txt": "a==1\n", "deps.lock": "1\n", "src/app.py": "x\n"})
    bump = python_command("open('deps.lock', 'w').write('2\\n')")
    allowed = ["Dockerfile", "deps.lock", "requirements.txt"]
    assert vuln.regenerate_locks(root, lock_doc(bump), ["Dockerfile"], allowed) == loop.AfterAttempt()
    assert (root / "deps.lock").read_text(encoding="utf-8") == "1\n"
    out = vuln.regenerate_locks(root, lock_doc(bump), ["requirements.txt"], allowed)
    assert out.rejected == "" and out.files == ["deps.lock"]
    assert out.steps == [{"component": "api", "command": bump, "exitCode": 0, "files": ["deps.lock"]}]


def test_a_lock_command_that_changes_files_outside_the_allowed_paths_is_rejected(tmp_path):
    root = lock_repo(tmp_path, {"requirements.txt": "a==1\n", "src/app.py": "x\n"})
    stray = python_command("open('src/app.py', 'w').write('y')")
    out = vuln.regenerate_locks(root, lock_doc(stray), ["requirements.txt"], ["Dockerfile", "requirements.txt"])
    assert out.rejected.endswith("changed files outside the allowed paths: src/app.py") and out.files == ["src/app.py"]
    missing = vuln.regenerate_locks(root, lock_doc("no-such-lock-tool --flag"), ["requirements.txt"], ["requirements.txt"])
    assert missing.rejected.startswith("the lock command `no-such-lock-tool --flag` for component api failed (exit 127)")
    assert "command not found: no-such-lock-tool" in missing.rejected


# --- discovery details ---


def write_scan(root: Path, name: str, results: list[dict], status: str = "failed") -> None:
    reports = root / pipeline.REPORTS
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"{name}.json").write_text(json.dumps({"Results": results}), encoding="utf-8")
    scans = [{"target": name, "json": (pipeline.REPORTS / f"{name}.json").as_posix()}]
    (root / pipeline.RESULTS).write_text(json.dumps([{"stage": "trivy", "status": status, "detail": "1 finding", "data": {"scans": scans}}]), encoding="utf-8")


def vuln_entry(vid: str, pkg: str, installed: str, fixed: str, severity: str = "HIGH") -> dict:
    return {"VulnerabilityID": vid, "PkgName": pkg, "InstalledVersion": installed, "FixedVersion": fixed, "Severity": severity}


def tracked(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    git(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "files")
    return tmp_path


DOC = {"components": [{"name": "api", "artifact": {"kind": "container-image"}}]}


def test_vex_covered_unfixed_and_low_findings_are_left_out(tmp_path):
    root = tracked(tmp_path, {"requirements.txt": "a==1.0\nb==1.0\nc==1.0\nd==1.0\n", "Dockerfile": "FROM python:3.13-slim\n",
                              ".rlp/vex.yaml": "exceptions:\n  - id: CVE-1\n    reason: r\n    expires: '2099-01-01'\n  - id: CVE-4\n    reason: r\n    expires: '2000-01-01'\n"})
    write_scan(root, "trivy-fs", [{"Target": "requirements.txt", "Class": "lang-pkgs", "Vulnerabilities": [
        vuln_entry("CVE-1", "a", "1.0", "1.1"), vuln_entry("CVE-2", "b", "1.0", ""), vuln_entry("CVE-3", "c", "1.0", "1.1", "MEDIUM"),
        vuln_entry("CVE-4", "d", "1.0", "1.1")]}])
    [f] = vuln.discover(root, DOC)
    assert (f["rule"], f["package"], f["path"], f["direct"], f["major"]) == ("CVE-4", "d", "requirements.txt", True, False)


def test_an_image_package_declared_by_the_app_points_at_its_manifest(tmp_path):
    root = tracked(tmp_path, {"requirements.txt": "requests==2.31.0\n", "Dockerfile": "FROM python:3.13-slim\n"})
    write_scan(root, "trivy-image-api", [{"Target": "Python", "Class": "lang-pkgs", "Vulnerabilities": [
        vuln_entry("CVE-9", "requests", "2.31.0", "2.32.0"), vuln_entry("CVE-8", "pip", "24.0", "25.0")]}])
    by_pkg = {f["package"]: f for f in vuln.discover(root, DOC)}
    assert (by_pkg["requests"]["path"], by_pkg["requests"]["source"]) == ("requirements.txt", "dependency")
    assert (by_pkg["pip"]["path"], by_pkg["pip"]["source"], by_pkg["pip"]["major"]) == ("Dockerfile", "base-image", True)
    # pip came with the base image, so its major bump is not a direct dependency's: no preflight escalation
    assert vuln.preflight(root, DOC, list(by_pkg.values())) == ""


def test_a_failed_trivy_stage_without_reports_still_reports_its_detail(tmp_path):
    root = tracked(tmp_path, {"Dockerfile": "FROM x:1\n"})
    (root / pipeline.RESULTS).parent.mkdir(parents=True)
    (root / pipeline.RESULTS).write_text(json.dumps([{"stage": "trivy", "status": "failed", "detail": "image hello-api:dev not found"}]))
    [f] = vuln.discover(root, DOC)
    assert f["message"] == "the trivy stage failed: image hello-api:dev not found"


def test_a_passed_trivy_stage_has_no_findings(tmp_path):
    root = tracked(tmp_path, {"requirements.txt": "a==1.0\n"})
    write_scan(root, "trivy-fs", [{"Target": "requirements.txt", "Vulnerabilities": [vuln_entry("CVE-1", "a", "1.0", "1.1")]}], status="passed")
    assert vuln.discover(root, DOC) == []


# --- allowed paths ---


def test_allowed_paths_are_manifests_and_dockerfiles_never_vex_or_gate_config(tmp_path):
    files = {f: "x\n" for f in ["rlp.yaml", "Taskfile.yml", "pyproject.toml", "uv.lock", "Dockerfile", "requirements.txt", ".coveragerc",
                                ".rlp/vex.yaml", ".trivyignore", ".github/workflows/ci.yml", "src/app/main.py", "web/package.json",
                                "web/Dockerfile.web", "web/src/index.ts", "tools/requirements-dev.txt", "policy/requirements.txt"]}
    root = tracked(tmp_path, files)
    doc = {"components": [{"name": "api", "artifact": {"kind": "container-image"}},
                          {"name": "web", "path": "web", "artifact": {"kind": "container-image", "dockerfile": "Dockerfile.web"}}]}
    write_scan(root, "trivy-fs", [{"Target": t} for t in ["tools/requirements-dev.txt", "policy/requirements.txt", ".rlp/vex.yaml"]])
    assert vuln.allowed_paths(root, doc) == ["Dockerfile", "pyproject.toml", "requirements.txt", "tools/requirements-dev.txt",
                                             "uv.lock", "web/Dockerfile.web", "web/package.json"]


def test_no_manifests_is_an_error(tmp_path):
    root = tracked(tmp_path, {"rlp.yaml": "x\n", "src/main.py": "x\n"})
    with pytest.raises(loop.LoopError, match="no tracked dependency manifests"):
        vuln.allowed_paths(root, DOC)


# --- versions and manifests ---


@pytest.mark.parametrize(("installed", "fixed", "expected"), [
    ("42.0.0", "42.0.4", False),
    ("1.7.1", "2.4.0", True),
    ("1.7.1", "1.7.9, 2.4.0", False),
    ("v1.2.0", "v2.0.1", True),
    ("2.5.0-1", "2.5.0-1+deb12u1", False),
    ("1:2.0-1", "1:3.0-1", True),
    ("abc", "1.0", False),
])
def test_needs_major(installed, fixed, expected):
    assert vuln.needs_major(installed, fixed) is expected


def test_pins_read_requirements_pyproject_uv_lock_package_json_and_dockerfiles():
    assert vuln.pins("requirements.txt", "# c\nRequests[socks]==2.31.0  # x\n-r other.txt\nflask>=3.0\nzope.interface\n") == {
        "requests": "2.31.0", "flask": "3.0", "zope-interface": ""}
    pyproject = '[project]\ndependencies = ["fastapi==0.115.0", "uvicorn[standard]>=0.30"]\n[dependency-groups]\ndev = ["pytest==8.3.0"]\n'
    assert vuln.pins("pyproject.toml", pyproject) == {"fastapi": "0.115.0", "uvicorn": "0.30", "pytest": "8.3.0"}
    lock = '[[package]]\nname = "Jinja2"\nversion = "3.1.4"\n'
    assert vuln.pins("uv.lock", lock) == {"jinja2": "3.1.4"}
    assert vuln.pins("package.json", '{"dependencies": {"express": "^4.19.2"}}') == {"express": "4.19.2"}
    assert vuln.pins("Dockerfile", "FROM --platform=linux/amd64 python:3.13-slim AS build\nFROM gcr.io/x/y:1.2@sha256:ab\nFROM scratch\n") == {
        "python": "3.13-slim", "gcr.io/x/y": "1.2"}
    assert vuln.pins("pyproject.toml", "not toml [") == {}


def test_major_bumps_count_direct_dependencies_and_base_images_only(tmp_path):
    root = tracked(tmp_path, {
        "requirements.txt": "cryptography==42.0.0\npyjwt==1.7.1\n",
        "pyproject.toml": '[project]\ndependencies = ["jinja2==3.1.4"]\n',
        "uv.lock": '[[package]]\nname = "jinja2"\nversion = "3.1.4"\n[[package]]\nname = "markupsafe"\nversion = "2.1.5"\n',
        "Dockerfile": "FROM python:3.13-slim\n",
    })
    git(root, "switch", "-q", "-c", "loop")
    (root / "requirements.txt").write_text("cryptography==42.0.4\npyjwt==1.7.1\n", encoding="utf-8")
    (root / "uv.lock").write_text('[[package]]\nname = "jinja2"\nversion = "3.1.5"\n[[package]]\nname = "markupsafe"\nversion = "3.0.2"\n', encoding="utf-8")
    commit(root, "patch bumps, and a transitive major")
    assert vuln.major_bumps(root, "main") == []
    assert vuln.review(root, "main") == ""
    (root / "requirements.txt").write_text("cryptography==42.0.4\npyjwt==2.4.0\n", encoding="utf-8")
    (root / "Dockerfile").write_text("FROM python:4.0-slim\n", encoding="utf-8")
    commit(root, "majors")
    assert vuln.major_bumps(root, "main") == ["base image python 3.13-slim -> 4.0-slim in Dockerfile", "dependency pyjwt 1.7.1 -> 2.4.0 in requirements.txt"]
    assert vuln.review(root, "main").startswith("the verify stages passed, but the fix is a major-version bump")


# --- the prompt ---


def test_prompt_shows_failed_gate_output_and_a_rejected_attempt():
    p = loop.PromptInput("vuln", 2, 3, [{"stage": "trivy", "message": "the trivy stage failed: boom"}],
                         [{"stage": "setup", "status": "failed", "detail": "pip: no matching distribution for cryptography==99"}],
                         ["requirements.txt"], rejected="it changed files outside the allowed paths: .rlp/vex.yaml")
    text = vuln.build_prompt(p)
    assert "attempt 2 of 3" in text and "## (no manifest)\n\n- the trivy stage failed: boom" in text
    assert "thrown away: it changed files outside the allowed paths: .rlp/vex.yaml" in text
    assert "- setup:\n\n  ```text\n  pip: no matching distribution for cryptography==99\n  ```" in text


def test_vuln_is_registered_with_its_verify_stages_and_hooks():
    kind = loop.KINDS["vuln"]
    assert kind.verify_stages == ("setup", "test", "package", "trivy")
    assert kind.build_prompt is vuln.build_prompt and kind.preflight is vuln.preflight and kind.review is vuln.review
    assert loop.KINDS["quality"].preflight is None and loop.KINDS["quality"].review is None
    assert ".rlp/vex.yaml" in kind.guarded and loop.KINDS["quality"].guarded == ()
    assert kind.after_attempt is vuln.regenerate_locks and loop.KINDS["quality"].after_attempt is None


def test_guarded_files_are_put_back_whether_changed_created_or_deleted(tmp_path):
    (tmp_path / ".rlp").mkdir()
    (tmp_path / ".rlp" / "vex.yaml").write_text("exceptions: []\n", encoding="utf-8")
    (tmp_path / "trivy.yaml").write_text("severity: HIGH\n", encoding="utf-8")
    before = loop.snapshot_guarded(tmp_path, [".rlp/vex.yaml", ".trivyignore", "trivy.yaml", "trivy.yml"])
    (tmp_path / ".rlp" / "vex.yaml").write_text("exceptions: [{id: CVE-1}]\n", encoding="utf-8")
    (tmp_path / ".trivyignore").write_text("CVE-1\n", encoding="utf-8")
    (tmp_path / "trivy.yaml").unlink()
    assert loop.restore_guarded(tmp_path, before) == [".rlp/vex.yaml", ".trivyignore", "trivy.yaml"]
    assert (tmp_path / ".rlp" / "vex.yaml").read_text(encoding="utf-8") == "exceptions: []\n"
    assert not (tmp_path / ".trivyignore").exists() and (tmp_path / "trivy.yaml").is_file()
    assert loop.restore_guarded(tmp_path, before) == []


# --- the workflow ---


def test_workflow_respects_the_toggle_and_opens_a_pr_only_when_fixed():
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert "workflow_call" in wf[True]
    [job] = wf["jobs"].values()
    assert "vars.RLP_LOOPS" in job["if"]
    assert job["env"]["RLP_LOOPS"] == "${{ vars.RLP_LOOPS }}"
    steps = {s.get("id") or s.get("name"): s for s in job["steps"]}
    assert "rlp loop status" in steps["toggle"]["run"] and '"vuln"' in steps["toggle"]["run"]
    after = job["steps"][job["steps"].index(steps["toggle"]) + 1:]
    assert after and all("steps.toggle.outputs.enabled == 'true'" in s.get("if", "") for s in after if s.get("name") != "Upload loop state")
    for stage in GATES:
        assert stage in steps["Gates"]["run"]
    assert "rlp loop vuln" in steps["loop"]["run"]
    pr = steps["pr"]
    assert "steps.loop.outputs.outcome == 'fixed'" in pr["if"] and "rlp loop pr" in pr["run"]
    assert "rlp loop escalate" in steps["escalate"]["run"]
    for s in job["steps"]:
        uses = s.get("uses", "")
        if uses:
            assert "@" in uses and len(uses.split("@")[1].split()[0]) == 40, f"{uses} is not pinned to a commit"
