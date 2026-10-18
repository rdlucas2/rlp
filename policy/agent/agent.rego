# Anti-cheat rules: an agent-authored change may not weaken a gate (#50).
#
# "Verifiers the agent can't edit" (docs/PLAN.md). These rules read the
# rlp_change document that `rlp policy check` adds to conftest's combined input
# (cli/src/rlp_cli/change.py): the PR author and labels, the files changed since
# the baseline commit with their added and removed lines, rlp.yaml and Taskfiles
# before and after, and test counts and coverage from base and head results.
#
# A change is agent-authored when it carries the agent:authored label or its
# author is listed in data.platform.agent_authors. Human changes pass these rules.
package rlp.agent

import data.rlp.lib

# --- input ---

changes := [entry.contents.rlp_change |
	is_array(input)
	some entry in input
	is_object(entry.contents)
	is_object(entry.contents.rlp_change)
]

agent_label := "agent:authored"

agent_authored(c) if agent_label in c.labels

agent_authored(c) if c.author in object.get(data.platform, "agent_authors", [])

agent_changes := [c | some c in changes; agent_authored(c)]

changed_files := [f | some c in agent_changes; some f in c.files]

# {"path", "before", "after"} for each rlp.yaml and Taskfile in an agent change.
contracts := [object.union(doc, {"path": path}) | some c in agent_changes; some path, doc in c.contracts]

# --- RLP-A001: protected gate files ---

protected_prefixes := [".github/workflows/", "policy/"]

protected_paths := {".rlp/vex.yaml"}

# Pre-commit, Semgrep, Trivy and coverage settings, wherever they live.
protected_names := {
	".pre-commit-config.yaml",
	".semgrep.yml",
	".semgrep.yaml",
	".semgrepignore",
	"trivy.yaml",
	"trivy.yml",
	".trivyignore",
	".trivyignore.yaml",
	".coveragerc",
}

protected(path) if {
	some prefix in protected_prefixes
	startswith(path, prefix)
}

protected(path) if path in protected_paths

protected(path) if lib.basename(path) in protected_names

protected(path) if contains(concat("", ["/", path]), "/.semgrep/")

verb := {"A": "adds", "D": "deletes"}

edits(status) := verb[status]

edits(status) := "edits" if not verb[status]

deny contains lib.msg(
	"RLP-A001", f.path, "(file)",
	sprintf("an agent-authored change %v a protected gate file", [edits(f.status)]),
	sprintf("revert the change to %v; gate configuration changes only by a human", [f.path]),
) if {
	some f in changed_files
	protected(f.path)
}

# --- RLP-A002: the quality block of rlp.yaml ---

quality(doc) := q if {
	is_object(doc)
	q := doc.quality
}

quality(doc) := null if {
	is_object(doc)
	not "quality" in object.keys(doc)
}

quality(doc) := null if not is_object(doc)

deny contains lib.msg(
	"RLP-A002", path, "quality",
	"an agent-authored change modifies the quality block",
	sprintf("restore the quality block of %v; quality settings change only by a human", [path]),
) if {
	some doc in contracts
	lib.basename(doc.path) == "rlp.yaml"
	path := doc.path
	quality(doc.before) != quality(doc.after)
}

# --- RLP-A003: the test verb (Taskfile test task, rlp.yaml verbs.test) ---

taskfile_test(doc) := object.get(doc, ["tasks", "test"], null) if is_object(doc)

taskfile_test(doc) := null if not is_object(doc)

deny contains lib.msg(
	"RLP-A003", path, "tasks.test",
	"an agent-authored change modifies the test task",
	sprintf("restore the test task in %v; the test verb changes only by a human", [path]),
) if {
	some doc in contracts
	lib.basename(doc.path) in lib.taskfile_names
	path := doc.path
	taskfile_test(doc.before) != taskfile_test(doc.after)
}

# Component name -> the verbs.test overrides of the components with that name
# (null when one has none). A list, so duplicate names (RLP-X004) can't conflict.
component_names(doc) := {c.name | some c in doc.components; is_object(c); is_string(c.name)}

verb_overrides(doc) := {name: overrides |
	is_object(doc)
	some name in component_names(doc)
	overrides := [object.get(c, ["verbs", "test"], null) | some c in doc.components; is_object(c); c.name == name]
}

verb_overrides(doc) := {} if not is_object(doc)

deny contains lib.msg(
	"RLP-A003", path, sprintf("components[%v].verbs.test", [name]),
	"an agent-authored change modifies the test verb override",
	sprintf("restore verbs.test of component %v; the test verb changes only by a human", [name]),
) if {
	some doc in contracts
	lib.basename(doc.path) == "rlp.yaml"
	path := doc.path
	before := verb_overrides(doc.before)
	after := verb_overrides(doc.after)
	some name in (object.keys(before) | object.keys(after))
	object.get(before, name, null) != object.get(after, name, null)
}

# --- RLP-A004: no new suppressions ---

suppressions := {
	"nosemgrep": `(?i)\bnosemgrep\b`,
	"NOSONAR": `(?i)\bNOSONAR\b`,
	"noqa": `(?i)\bnoqa\b`,
	"type: ignore": `#\s*type:\s*ignore`,
	"pragma: no cover": `(?i)pragma:\s*no\s*(cover|branch)`,
	"pylint: disable": `(?i)pylint:\s*disable`,
	"nosec": `(?i)\bnosec\b`,
	"eslint-disable": `eslint-disable`,
	"@ts-ignore": `@ts-(ignore|nocheck|expect-error)\b`,
	"istanbul ignore": `(?i)\b(istanbul|c8|v8)\s+ignore\b`,
	"NOLINT": `\bNOLINT`,
	"nolint": `//\s*nolint\b`,
	"@SuppressWarnings": `@SuppressWarnings\b`,
	"SuppressMessage": `\bSuppressMessage\b`,
	"pragma warning disable": `#pragma\s+warning\s+disable`,
	"ExcludeFromCodeCoverage": `\bExcludeFromCodeCoverage\b`,
	"LCOV_EXCL": `\bLCOV_EXCL`,
	"rubocop:disable": `rubocop:\s*disable`,
	"gitleaks:allow": `gitleaks:allow`,
	"trivy:ignore": `trivy:ignore`,
}

# Prose mentions a suppression without applying it.
prose_suffixes := [".md", ".markdown", ".rst", ".txt"]

prose(path) if {
	some suffix in prose_suffixes
	endswith(lower(path), suffix)
}

deny contains lib.msg(
	"RLP-A004", f.path, sprintf("line %d", [a.line]),
	sprintf("an agent-authored change adds a %v suppression", [name]),
	"fix the finding instead of suppressing it, or ask a human to accept it",
) if {
	some f in changed_files
	not protected(f.path)
	not prose(f.path)
	some a in f.added
	some name, pattern in suppressions
	regex.match(pattern, a.text)
}

# --- RLP-A005 and RLP-A006: no drop in test count or line coverage ---

results_file := ".rlp/pipeline/results.json"

# Base and head results of each agent change that has both.
compared := [c.results | some c in agent_changes; is_object(c.results.base); is_object(c.results.head)]

deny contains lib.msg(
	"RLP-A005", results_file, sprintf("test.%v", [name]),
	sprintf("an agent-authored change drops the test count from %v to %v", [before, after]),
	sprintf("restore the removed tests of component %v", [name]),
) if {
	some r in compared
	count(r.head.tests) > 0
	some name, before in r.base.tests
	after := object.get(r.head.tests, name, 0)
	after < before
}

deny contains lib.msg(
	"RLP-A006", results_file, sprintf("coverage.%v", [name]),
	sprintf("an agent-authored change drops line coverage from %v%% to %v%%", [before, after]),
	sprintf("add tests for component %v so coverage is at least %v%%", [name, before]),
) if {
	some r in compared
	count(r.head.coverage) > 0
	some name, before in r.base.coverage
	after := object.get(r.head.coverage, name, 0)
	after < before
}

# --- RLP-A007: no net loss of test definitions in the diff ---
#
# The policy stage runs before the test stage, so it can't count tests yet; it
# counts test definitions the diff removes and adds instead.

tests_path_pattern := `(?i)(^|/)(tests?|spec|specs|__tests__)/|(^|/)test_[^/]*$|_test\.[a-z0-9]+$|\.(test|spec)\.[a-z0-9]+$|tests?\.(java|kt|cs|scala)$`

definition_patterns := [
	`^\s*(async\s+)?def\s+test`,
	`^\s*(it|test)\s*(\.\s*\w+\s*)?\(`,
	`@(Test|ParameterizedTest|RepeatedTest)\b`,
	`^\s*func\s+Test\w*\s*\(`,
	`\[(Fact|Theory|Test|TestMethod|TestCase)\b`,
	`#\[test\]`,
]

defines_test(text) if {
	some pattern in definition_patterns
	regex.match(pattern, text)
}

changed_test_files := [f |
	some f in changed_files
	regex.match(tests_path_pattern, f.path)
]

removed_tests(f) := count([t | some t in f.removed; defines_test(t)])

added_tests(f) := count([a | some a in f.added; defines_test(a.text)])

net_removed := sum([removed_tests(f) | some f in changed_test_files]) - sum([added_tests(f) | some f in changed_test_files])

deny contains lib.msg(
	"RLP-A007", f.path, "(tests)",
	sprintf("an agent-authored change removes %d test definition(s) here and %d across the change", [removed_tests(f) - added_tests(f), net_removed]),
	"restore the removed tests; a fix may add tests but not delete them",
) if {
	net_removed > 0
	some f in changed_test_files
	removed_tests(f) > added_tests(f)
}
