from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

VERBS = ["setup", "lint", "test", "build", "package", "run", "smoke"]


def write_repo(root: Path, components: list[dict]) -> Path:
    doc = {
        "apiVersion": "rlp/v1alpha1",
        "kind": "Repo",
        "metadata": {"name": "verbs", "project": "verbs"},
        "components": components,
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "rlp.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return root


def component(name: str, path: str = ".", verbs: dict | None = None) -> dict:
    c = {
        "name": name,
        "path": path,
        "language": "python",
        "artifact": {"kind": "container-image"},
        "runtime": {"kind": "container-service", "port": 8000},
    }
    if verbs:
        c["verbs"] = verbs
    return c


def marker(python_cmd: str, name: str) -> str:
    """A verb override that writes `name` into the current directory."""
    return f"{python_cmd} -c \"open('{name}', 'w').write('ran')\""


@pytest.mark.parametrize("verb", VERBS)
def test_override_runs_in_component_dir(rlp, tmp_path, python_cmd, verb):
    repo = write_repo(tmp_path / "repo", [component("api", "services/api", {verb: marker(python_cmd, f"{verb}.out")})])
    (repo / "services" / "api").mkdir(parents=True)
    native = ["--native"] if verb in ("run", "smoke") else []  # without it, run and smoke use the local runtime
    result = rlp(verb, *native, "--path", str(repo))
    assert result.code == 0, result.stdout + result.stderr
    assert (repo / "services" / "api" / f"{verb}.out").read_text() == "ran"


def test_default_runs_task(rlp, tmp_path):
    if shutil.which("task") is None:
        pytest.fail("go-task (`task`) must be on PATH for the CLI tests")
    repo = write_repo(tmp_path / "repo", [component("api")])
    (repo / "Taskfile.yml").write_text(
        'version: "3"\ntasks:\n  test:\n    cmds:\n      - echo from-task > task.out\n', encoding="utf-8"
    )
    result = rlp("test", "--path", str(repo))
    assert result.code == 0, result.stdout + result.stderr
    assert "from-task" in (repo / "task.out").read_text()


def test_runs_every_component_by_default(rlp, tmp_path, python_cmd):
    repo = write_repo(tmp_path / "repo", [
        component("a", "a", {"build": marker(python_cmd, "built")}),
        component("b", "b", {"build": marker(python_cmd, "built")}),
    ])
    (repo / "a").mkdir()
    (repo / "b").mkdir()
    result = rlp("build", "--path", str(repo), "-o", "json")
    assert result.code == 0, result.stderr
    assert [s["component"] for s in result.json["steps"]] == ["a", "b"]
    assert (repo / "a" / "built").exists() and (repo / "b" / "built").exists()


def test_component_filter(rlp, tmp_path, python_cmd):
    repo = write_repo(tmp_path / "repo", [
        component("a", "a", {"build": marker(python_cmd, "built")}),
        component("b", "b", {"build": marker(python_cmd, "built")}),
    ])
    (repo / "a").mkdir()
    (repo / "b").mkdir()
    result = rlp("build", "--path", str(repo), "--component", "b")
    assert result.code == 0
    assert not (repo / "a" / "built").exists()
    assert (repo / "b" / "built").exists()


def test_unknown_component_is_a_usage_error(rlp, tmp_path):
    repo = write_repo(tmp_path / "repo", [component("api")])
    result = rlp("test", "--path", str(repo), "--component", "web")
    assert result.code == 2
    assert "unknown component(s): web" in result.stderr


def test_failing_verb_exits_one_and_reports_exit_code(rlp, tmp_path, python_cmd):
    repo = write_repo(tmp_path / "repo", [component("api", verbs={"lint": f'{python_cmd} -c "raise SystemExit(3)"'})])
    result = rlp("lint", "--path", str(repo), "-o", "json")
    assert result.code == 1
    (step,) = result.json["steps"]
    assert step["exit_code"] == 3
    assert step["cwd"] == "."


def test_json_mode_keeps_stdout_clean(rlp, tmp_path, python_cmd):
    repo = write_repo(tmp_path / "repo", [component("api", verbs={"test": f'{python_cmd} -c "print(42)"'})])
    result = rlp("test", "--path", str(repo), "-o", "json")
    assert result.code == 0
    assert result.json["verb"] == "test"
    assert "42" in result.stderr


def test_missing_command_is_reported(rlp, tmp_path):
    repo = write_repo(tmp_path / "repo", [component("api", verbs={"smoke": "definitely-not-a-command-rlp"})])
    result = rlp("smoke", "--native", "--path", str(repo), "-o", "json")
    assert result.code == 1
    assert result.json["steps"][0]["error"] == "command not found: definitely-not-a-command-rlp"


def test_invalid_contract_stops_before_running(rlp, tmp_path):
    repo = write_repo(tmp_path / "repo", [{"name": "api"}])
    result = rlp("test", "--path", str(repo))
    assert result.code == 2
    assert "rlp.yaml is invalid" in result.stderr


def test_windows_style_override_paths_use_forward_slashes(tmp_path):
    from rlp_cli.verbs import command_for

    c = {"verbs": {"test": '"C:/Program Files/Python/python.exe" -m pytest tests/unit'}}
    assert command_for(c, "test") == ["C:/Program Files/Python/python.exe", "-m", "pytest", "tests/unit"]
    assert command_for({}, "test") == ["task", "test"]


def test_component_dir_handles_nested_posix_paths(tmp_path):
    from rlp_cli.contract import component_dir

    assert component_dir(tmp_path, {"path": "services/api"}) == tmp_path / "services" / "api"
    assert component_dir(tmp_path, {}) == tmp_path
    assert component_dir(tmp_path, {"path": "."}) == tmp_path
