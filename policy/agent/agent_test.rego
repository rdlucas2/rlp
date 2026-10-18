package rlp.agent_test

import data.rlp.agent
import data.rlp.lib_test.ids

# A combined input holding only the change document.
with_change(c) := [{"path": "/tmp/rlp-change.json", "contents": {"rlp_change": object.union(base_change, c)}}]

base_change := {
	"baseline": "0123456789abcdef",
	"author": "rdlucas2",
	"labels": ["agent:authored"],
	"files": [],
	"contracts": {},
	"results": {"base": null, "head": null},
}

human := {"labels": []}

file(path, added) := {"path": path, "status": "M", "added": [{"line": i + 1, "text": t} | some i, t in added], "removed": []}

removed(path, lines) := {"path": path, "status": "M", "added": [], "removed": lines}

rlp_doc(minimum) := {"apiVersion": "rlp/v1alpha1", "kind": "Repo", "components": [{"name": "api"}], "quality": {"coverage": {"minimum": minimum}}}

test_no_change_document_passes if {
	count(agent.deny) == 0 with input as [{"path": "rlp.yaml", "contents": rlp_doc(80)}]
}

test_clean_agent_change_passes if {
	c := {"files": [file("src/app.py", ["def handler():", "    return 1"]), file("tests/test_app.py", ["def test_handler():"])]}
	count(agent.deny) == 0 with input as with_change(c)
}

# --- A001 ---

test_a001_policy_bundle_edit if {
	c := {"files": [file("policy/lib/lib.rego", ["x := 1"])]}
	some m in agent.deny with input as with_change(c)
	m == "[RLP-A001] policy/lib/lib.rego (file): an agent-authored change edits a protected gate file. Fix: revert the change to policy/lib/lib.rego; gate configuration changes only by a human."
}

test_a001_every_protected_path if {
	paths := [
		".github/workflows/ci.yml", "policy/platform/data.yaml", ".rlp/vex.yaml", ".pre-commit-config.yaml",
		"svc/.pre-commit-config.yaml", ".semgrepignore", ".semgrep/rules.yml", "trivy.yaml", ".trivyignore", ".coveragerc",
	]
	c := {"files": [file(p, []) | some p in paths]}
	denied := agent.deny with input as with_change(c)
	count(denied) == count(paths)
	ids(denied) == {"RLP-A001"}
}

test_a001_deleting_vex_file if {
	c := {"files": [{"path": ".rlp/vex.yaml", "status": "D", "added": [], "removed": ["exceptions: []"]}]}
	some m in agent.deny with input as with_change(c)
	contains(m, "an agent-authored change deletes a protected gate file")
}

test_a001_ordinary_files_pass if {
	c := {"files": [file("src/policy_helpers.py", []), file("docs/policy.md", []), file("README.md", [])]}
	count(agent.deny) == 0 with input as with_change(c)
}

test_a001_human_passes if {
	c := object.union({"files": [file("policy/lib/lib.rego", [])]}, human)
	count(agent.deny) == 0 with input as with_change(c)
}

test_a001_listed_agent_author_without_label if {
	c := {"labels": [], "author": "rlp-agent[bot]", "files": [file("policy/lib/lib.rego", [])]}
	ids(agent.deny) == {"RLP-A001"} with input as with_change(c) with data.platform.agent_authors as ["rlp-agent[bot]"]
}

# --- A002 ---

test_a002_lowered_coverage_minimum if {
	c := {"files": [file("rlp.yaml", ["    minimum: 70"])], "contracts": {"rlp.yaml": {"before": rlp_doc(80), "after": rlp_doc(70)}}}
	some m in agent.deny with input as with_change(c)
	m == "[RLP-A002] rlp.yaml quality: an agent-authored change modifies the quality block. Fix: restore the quality block of rlp.yaml; quality settings change only by a human."
}

test_a002_removed_quality_block if {
	after := json.remove(rlp_doc(80), ["quality"])
	c := {"contracts": {"rlp.yaml": {"before": rlp_doc(80), "after": after}}}
	ids(agent.deny) == {"RLP-A002"} with input as with_change(c)
}

test_a002_other_rlp_yaml_edits_pass if {
	after := json.patch(rlp_doc(80), [{"op": "add", "path": "/metadata", "value": {"description": "new"}}])
	c := {"contracts": {"rlp.yaml": {"before": rlp_doc(80), "after": after}}}
	count(agent.deny) == 0 with input as with_change(c)
}

test_a002_human_passes if {
	c := object.union({"contracts": {"rlp.yaml": {"before": rlp_doc(80), "after": rlp_doc(70)}}}, human)
	count(agent.deny) == 0 with input as with_change(c)
}

# --- A003 ---

taskfile(test_cmds) := {"version": "3", "tasks": {"lint": {"cmds": ["ruff check"]}, "test": {"cmds": test_cmds}}}

test_a003_changed_test_task if {
	c := {"contracts": {"Taskfile.yml": {"before": taskfile(["pytest"]), "after": taskfile(["pytest -k 'not slow'"])}}}
	some m in agent.deny with input as with_change(c)
	startswith(m, "[RLP-A003] Taskfile.yml tasks.test: an agent-authored change modifies the test task.")
}

test_a003_other_task_edits_pass if {
	after := json.patch(taskfile(["pytest"]), [{"op": "replace", "path": "/tasks/lint/cmds", "value": ["ruff check --fix"]}])
	c := {"contracts": {"svc/Taskfile.yml": {"before": taskfile(["pytest"]), "after": after}}}
	count(agent.deny) == 0 with input as with_change(c)
}

test_a003_verb_override_in_rlp_yaml if {
	after := json.patch(rlp_doc(80), [{"op": "add", "path": "/components/0/verbs", "value": {"test": ["true"]}}])
	c := {"contracts": {"rlp.yaml": {"before": rlp_doc(80), "after": after}}}
	some m in agent.deny with input as with_change(c)
	startswith(m, "[RLP-A003] rlp.yaml components[api].verbs.test: an agent-authored change modifies the test verb override.")
}

test_a003_duplicate_component_names_do_not_conflict if {
	before := json.patch(rlp_doc(80), [{"op": "add", "path": "/components/-", "value": {"name": "api"}}])
	after := json.patch(before, [{"op": "add", "path": "/components/1/verbs", "value": {"test": ["true"]}}])
	c := {"contracts": {"rlp.yaml": {"before": before, "after": after}}}
	ids(agent.deny) == {"RLP-A003"} with input as with_change(c)
}

test_a003_human_passes if {
	c := object.union({"contracts": {"Taskfile.yml": {"before": taskfile(["pytest"]), "after": taskfile(["true"])}}}, human)
	count(agent.deny) == 0 with input as with_change(c)
}

# --- A004 ---

test_a004_nosemgrep if {
	c := {"files": [file("src/app.py", ["import os", "os.system(cmd)  # nosemgrep"])]}
	some m in agent.deny with input as with_change(c)
	m == "[RLP-A004] src/app.py line 2: an agent-authored change adds a nosemgrep suppression. Fix: fix the finding instead of suppressing it, or ask a human to accept it."
}

test_a004_each_suppression if {
	lines := [
		"x = 1  # NOSONAR", "import os  # noqa: F401", "y: int = 'a'  # type: ignore[assignment]",
		"if debug:  # pragma: no cover", "// eslint-disable-next-line no-eval", "// @ts-ignore",
		"# pylint: disable=broad-except", "subprocess.call(x)  # nosec B603", "/* istanbul ignore next */",
		"@SuppressWarnings(\"unchecked\")", "token = 'abc'  # gitleaks:allow", "// nolint:errcheck",
	]
	c := {"files": [file("src/app.py", lines)]}
	denied := agent.deny with input as with_change(c)
	count(denied) == count(lines)
	ids(denied) == {"RLP-A004"}
}

test_a004_prose_and_ordinary_code_pass if {
	c := {"files": [
		file("docs/quality-gates.md", ["A `# nosemgrep` comment silences a finding."]),
		file("src/app.py", ["def ignore_case(type_name):", "    return type_name.lower()"]),
	]}
	count(agent.deny) == 0 with input as with_change(c)
}

test_a004_human_passes if {
	c := object.union({"files": [file("src/app.py", ["os.system(cmd)  # nosemgrep"])]}, human)
	count(agent.deny) == 0 with input as with_change(c)
}

# --- A005 and A006 ---

results(tests, coverage) := {"tests": tests, "coverage": coverage}

test_a005_test_count_drop if {
	c := {"results": {"base": results({"api": 12}, {}), "head": results({"api": 11}, {})}}
	some m in agent.deny with input as with_change(c)
	m == "[RLP-A005] .rlp/pipeline/results.json test.api: an agent-authored change drops the test count from 12 to 11. Fix: restore the removed tests of component api."
}

test_a005_more_tests_pass if {
	c := {"results": {"base": results({"api": 12}, {}), "head": results({"api": 13}, {})}}
	count(agent.deny) == 0 with input as with_change(c)
}

test_a005_needs_both_results if {
	c := {"results": {"base": null, "head": results({"api": 1}, {})}}
	count(agent.deny) == 0 with input as with_change(c)
}

test_a006_coverage_drop if {
	c := {"results": {"base": results({}, {"api": 91.5}), "head": results({}, {"api": 90.25})}}
	some m in agent.deny with input as with_change(c)
	m == "[RLP-A006] .rlp/pipeline/results.json coverage.api: an agent-authored change drops line coverage from 91.5% to 90.25%. Fix: add tests for component api so coverage is at least 91.5%."
}

test_a006_same_coverage_passes if {
	c := {"results": {"base": results({}, {"api": 91.5}), "head": results({}, {"api": 91.5})}}
	count(agent.deny) == 0 with input as with_change(c)
}

test_a005_a006_human_passes if {
	c := object.union({"results": {"base": results({"api": 12}, {"api": 90}), "head": results({"api": 1}, {"api": 10})}}, human)
	count(agent.deny) == 0 with input as with_change(c)
}

# --- A007 ---

test_a007_removed_test if {
	c := {"files": [removed("tests/test_app.py", ["def test_handler():", "    assert handler() == 1"])]}
	some m in agent.deny with input as with_change(c)
	m == "[RLP-A007] tests/test_app.py (tests): an agent-authored change removes 1 test definition(s) here and 1 across the change. Fix: restore the removed tests; a fix may add tests but not delete them."
}

test_a007_each_language if {
	c := {"files": [
		removed("src/app_test.go", ["func TestHandler(t *testing.T) {"]),
		removed("web/app.test.ts", ["  it('renders', () => {", "test('works', async () => {"]),
		removed("src/test/java/AppTest.java", ["    @Test"]),
		removed("tests/AppTests.cs", ["    [Fact]"]),
	]}
	denied := agent.deny with input as with_change(c)
	count(denied) == 4
	ids(denied) == {"RLP-A007"}
}

test_a007_moved_test_passes if {
	c := {"files": [
		removed("tests/test_old.py", ["def test_handler():"]),
		file("tests/test_new.py", ["def test_handler():"]),
	]}
	count(agent.deny) == 0 with input as with_change(c)
}

test_a007_removed_helper_passes if {
	c := {"files": [removed("tests/conftest.py", ["def make_client():"]), removed("src/app.py", ["def test_mode():"])]}
	count(agent.deny) == 0 with input as with_change(c)
}

test_a007_human_passes if {
	c := object.union({"files": [removed("tests/test_app.py", ["def test_handler():"])]}, human)
	count(agent.deny) == 0 with input as with_change(c)
}
