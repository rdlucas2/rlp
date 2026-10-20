"""Run the policy bundle over the fixture repos in tests/policy/fixtures.

Each case in cases.yaml is a small repo. The check is the same one
`rlp policy check` will run: conftest --combine from the repo root with the
bundle in policy/ as policy and data. A case passes when the set of rule ids
in the failures equals its `expect` list exactly and the set in the warnings
equals its `warn` list exactly, so an invalid fixture trips its own rule and
nothing else.

Fixture files that conftest would parse on its own (Dockerfiles, Terraform plan
JSON, change and transition documents) are stored under other names, so
`rlp policy check` on the rlp repo doesn't report the fixtures' deliberate
violations (#62). restore_names() gives them their real names in the temp copy
each test checks.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "policy"
HERE = Path(__file__).resolve().parent
CASES = yaml.safe_load((HERE / "cases.yaml").read_text(encoding="utf-8"))["cases"]

RULE_ID = re.compile(r"RLP-[A-Z][0-9]{3}")
MESSAGE = re.compile(r"^\[(RLP-[A-Z][0-9]{3})\] (\S+) (.+?): (.+)\. Fix: (.+)\.$")

# Stored name -> real name. conftest reads any file with "dockerfile" in its
# name as a Dockerfile, so the stored name avoids that word.
STORED_NAMES = {"Containerfile.fixture": "Dockerfile", "plan.json.fixture": "plan.json", "rlp-change.json.fixture": "rlp-change.json", "rlp-transition.json.fixture": "rlp-transition.json"}


def restore_names(repo: Path) -> None:
    for stored, real in STORED_NAMES.items():
        for path in list(repo.rglob(stored)):
            path.rename(path.with_name(real))


def tool(name: str) -> str:
    exe = ".exe" if os.name == "nt" else ""
    local = ROOT / ".tools" / "bin" / f"{name}{exe}"
    if local.exists():
        return str(local)
    found = shutil.which(name)
    if found is None:
        pytest.fail(f"{name} not found; run `task test` to install the pinned version into .tools/bin")
    return found


def conftest(repo: Path, namespaces: list[str] | None) -> tuple[list[str], list[str]]:
    args = [tool("conftest"), "test", "--combine", "--no-color", "--output", "json"]
    args += ["--policy", str(POLICY), "--data", str(POLICY)]
    if namespaces:
        for ns in namespaces:
            args += ["--namespace", ns]
    else:
        args.append("--all-namespaces")
    args.append(".")
    result = subprocess.run(args, cwd=repo, capture_output=True, text=True, check=False)
    assert result.returncode in (0, 1), result.stderr or result.stdout
    report = json.loads(result.stdout)
    assert not any(r.get("exceptions") for r in report), report
    failures = [f["msg"] for r in report for f in (r.get("failures") or [])]
    warnings = [w["msg"] for r in report for w in (r.get("warnings") or [])]
    return failures, warnings


@pytest.mark.parametrize("name", sorted(CASES))
def test_case(name: str, tmp_path: Path) -> None:
    case = CASES[name]
    repo = tmp_path / name
    shutil.copytree(HERE / "fixtures" / name, repo)
    restore_names(repo)
    for dest, src in (case.get("copy") or {}).items():
        target = repo / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / src, target)

    failures, warnings = conftest(repo, case.get("namespaces"))

    assert {RULE_ID.search(m).group(0) for m in failures} == set(case["expect"]), failures
    assert {RULE_ID.search(m).group(0) for m in warnings} == set(case.get("warn") or []), warnings
    for m in failures + warnings:
        assert MESSAGE.match(m), f"message does not name file, field and fix: {m}"


def test_fixtures_use_stored_names() -> None:
    # A fixture stored under its real name would be checked by `rlp policy check`
    # on the rlp repo itself, and the golden pipeline would fail on it.
    real = set(STORED_NAMES.values())
    offenders = [p for p in (HERE / "fixtures").rglob("*") if p.is_file() and (p.name in real or "dockerfile" in p.name.lower())]
    assert not offenders, f"rename these to their stored names {sorted(STORED_NAMES)}: {offenders}"


def test_every_rule_has_a_failing_fixture() -> None:
    defined = {
        rule
        for path in POLICY.rglob("*.rego")
        if not path.name.endswith("_test.rego")
        for rule in RULE_ID.findall(path.read_text(encoding="utf-8"))
    }
    covered = {rule for case in CASES.values() for rule in [*case["expect"], *(case.get("warn") or [])]}
    assert defined, "no rule ids found in policy/"
    assert defined == covered


def test_valid_examples_are_covered() -> None:
    copied = {src for case in CASES.values() if not case["expect"] and not case.get("warn") for src in (case.get("copy") or {}).values()}
    assert {"examples/rlp.yaml", "examples/rlp-template.yaml"} <= copied


def test_rego_enums_match_json_schema() -> None:
    query = "x := {k: data.rlp.lib[k] | some k in [\"artifact_kinds\", \"runtime_kinds\", \"coverage_formats\", \"service_kinds\", \"api_version\"]}"
    result = subprocess.run(
        [tool("opa"), "eval", "--format", "json", "--data", str(POLICY), query],
        capture_output=True, text=True, check=True,
    )
    rego = json.loads(result.stdout)["result"][0]["bindings"]["x"]
    schema = json.loads((ROOT / "schemas" / "v1alpha1" / "repo.schema.json").read_text(encoding="utf-8"))
    defs = schema["$defs"]
    assert set(rego["artifact_kinds"]) == set(defs["artifact"]["properties"]["kind"]["enum"])
    assert set(rego["runtime_kinds"]) == set(defs["runtime"]["properties"]["kind"]["enum"])
    assert set(rego["coverage_formats"]) == set(defs["reports"]["properties"]["coverage"]["properties"]["format"]["enum"])
    assert set(rego["service_kinds"]) == set(defs["service"]["properties"]["kind"]["enum"])
    assert rego["api_version"] == schema["properties"]["apiVersion"]["const"]
