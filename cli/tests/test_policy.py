from __future__ import annotations

import shutil

import yaml

from conftest import EXAMPLES, POLICY_FIXTURES, REPO

CASES = yaml.safe_load((REPO / "tests" / "policy" / "cases.yaml").read_text(encoding="utf-8"))["cases"]


def slice0_repo(copy_fixture):
    repo = copy_fixture(POLICY_FIXTURES / "slice0-app")
    shutil.copyfile(EXAMPLES / "rlp.yaml", repo / "rlp.yaml")
    return repo


def test_slice0_example_repo_passes(rlp, copy_fixture):
    repo = slice0_repo(copy_fixture)
    result = rlp("policy", "check", str(repo))
    assert result.code == 0, result.stdout + result.stderr
    assert "no violations" in result.stdout


def test_each_invalid_fixture_fails_its_rule(rlp, copy_fixture):
    for name, case in CASES.items():
        if not case["expect"] or case.get("copy"):
            continue
        repo = copy_fixture(POLICY_FIXTURES / name)
        args = ["policy", "check", str(repo), "-o", "json"]
        for ns in case.get("namespaces") or []:
            args += ["--namespace", ns]
        result = rlp(*args)
        assert result.code == 1, (name, result.stdout, result.stderr)
        assert {v["rule"] for v in result.json["violations"]} == set(case["expect"]), name


def test_each_warning_fixture_warns_and_passes(rlp, copy_fixture):
    warned = 0
    for name, case in CASES.items():
        if not case.get("warn"):
            continue
        repo = copy_fixture(POLICY_FIXTURES / name)
        args = ["policy", "check", str(repo), "-o", "json"]
        for ns in case.get("namespaces") or []:
            args += ["--namespace", ns]
        result = rlp(*args)
        assert result.code == 0, (name, result.stdout, result.stderr)
        assert result.json["violations"] == [], name
        assert {w["rule"] for w in result.json["warnings"]} == set(case["warn"]), name
        warned += 1
    assert warned


def test_warnings_are_printed(rlp, copy_fixture):
    name = next(n for n, c in CASES.items() if c.get("warn"))
    repo = copy_fixture(POLICY_FIXTURES / name)
    result = rlp("policy", "check", str(repo), "--namespace", "rlp.agent")
    assert result.code == 0, result.stdout + result.stderr
    assert f"warning: [{CASES[name]['warn'][0]}]" in result.stdout
    assert "gate changes needing review" in result.stdout
    assert "no violations" in result.stdout


def test_json_violation_fields(rlp, copy_fixture):
    repo = copy_fixture(POLICY_FIXTURES / "x003-unknown-service")
    result = rlp("policy", "check", str(repo), "-o", "json")
    (v,) = result.json["violations"]
    assert v["rule"] == "RLP-X003"
    assert v["file"] == "rlp.yaml"
    assert v["field"] == "components[0].config[0].from"
    assert v["namespace"] == "rlp.contract.crossfile"


def test_dependency_directories_are_ignored(rlp, copy_fixture):
    repo = slice0_repo(copy_fixture)
    junk = repo / "node_modules" / "pkg"
    junk.mkdir(parents=True)
    (junk / "rlp.yaml").write_text("kind: Nonsense\n", encoding="utf-8")
    assert rlp("policy", "check", str(repo)).code == 0


def test_rego_sources_are_not_parsed_as_configuration(rlp, copy_fixture):
    # conftest reads any file with "dockerfile" in its name as a Dockerfile, so a
    # repo that ships Rego (rlp itself: policy/container/dockerfile.rego) failed.
    repo = slice0_repo(copy_fixture)
    rules = repo / "policy" / "container"
    rules.mkdir(parents=True)
    (rules / "dockerfile.rego").write_text("package rlp.container\n", encoding="utf-8")
    assert rlp("policy", "check", str(repo)).code == 0


def test_missing_directory_is_a_usage_error(rlp, tmp_path):
    assert rlp("policy", "check", str(tmp_path / "nope")).code == 2
