package rlp.lifecycle_test

import data.rlp.lifecycle

# --- helpers ---

path := "rlp-transition.json"

green_repo := {"full_name": "acme/api", "default_branch": "main", "builds": 3, "latest_default_branch_build": {"commit": "abc123", "passed": true}}

# A project that meets every entry rule.
ready := {
	"name": "api",
	"description": "The orders API",
	"owner": "rdlucas2",
	"dashboard_url": "https://grafana.example/d/api",
}

doc(from, to, project, repos, teardown) := [{"path": path, "contents": {"rlp_transition": {
	"project": project,
	"from": from,
	"to": to,
	"repos": repos,
	"teardown": teardown,
}}}]

move(from, to) := doc(from, to, ready, [green_repo], {"at": "2026-10-20T12:00:00Z"})

ids(denials) := {regex.find_n(`RLP-L[0-9]{3}`, m, 1)[0] | some m in denials}

denied(input_doc) := result if {
	denials := lifecycle.deny with input as input_doc
	result := ids(denials)
}

# --- input ---

test_repo_files_are_ignored if {
	count(lifecycle.deny) == 0 with input as [{"path": "rlp.yaml", "contents": {"apiVersion": "rlp/v1alpha1"}}]
}

test_non_combined_input_is_ignored if {
	count(lifecycle.deny) == 0 with input as {"rlp_transition": {"from": "proposed", "to": "live"}}
}

# --- RLP-L101: moves ---

test_l101_allows_each_forward_step if {
	every i in numbers.range(0, count(lifecycle.phases) - 2) {
		count(denied(move(lifecycle.phases[i], lifecycle.phases[i + 1]))) == 0
	}
}

test_l101_allows_deprecated_from_in_dev if count(denied(move("in-dev", "deprecated"))) == 0

test_l101_allows_back_to_live_from_deprecated if count(denied(move("deprecated", "live"))) == 0

test_l101_denies_skipping_a_phase if denied(move("proposed", "live")) == {"RLP-L101"}

test_l101_denies_moving_back if denied(move("live", "in-dev")) == {"RLP-L101"}

test_l101_denies_retired_except_from_deprecated if denied(move("live", "retired")) == {"RLP-L101"}

test_l101_denies_leaving_retired if denied(move("retired", "deprecated")) == {"RLP-L101"}

test_l101_denies_staying_put if denied(move("live", "live")) == {"RLP-L101"}

test_l101_denies_an_unknown_phase if denied(move("proposed", "launched")) == {"RLP-L101"}

test_l101_denies_a_missing_from if {
	denied([{"path": path, "contents": {"rlp_transition": {"project": ready, "to": "designed"}}}]) == {"RLP-L101"}
}

test_l101_message_names_both_phases if {
	some m in lifecycle.deny with input as move("proposed", "live")
	m == concat("", [
		`[RLP-L101] rlp-transition.json to: project "api" may not move from "proposed" to "live". `,
		"Fix: move one phase forward (proposed, designed, scaffolded, in-dev, live, deprecated, retired), ",
		"to deprecated from live or in-dev, back to live from deprecated, or to retired only from deprecated.",
	])
}

# --- RLP-L102: designed needs a description ---

test_l102_passes_with_a_description if count(denied(move("proposed", "designed"))) == 0

test_l102_denies_an_empty_description if {
	denied(doc("proposed", "designed", object.union(ready, {"description": "  "}), [], null)) == {"RLP-L102"}
}

test_l102_denies_a_missing_description if {
	denied(doc("proposed", "designed", object.remove(ready, ["description"]), [], null)) == {"RLP-L102"}
}

# --- RLP-L103: scaffolded needs a repo ---

test_l103_passes_with_a_repo if count(denied(move("designed", "scaffolded"))) == 0

test_l103_denies_no_repos if denied(doc("designed", "scaffolded", ready, [], null)) == {"RLP-L103"}

# --- RLP-L104: in-dev needs a build ---

test_l104_passes_with_a_build if count(denied(move("scaffolded", "in-dev"))) == 0

test_l104_denies_no_builds if {
	repo := object.union(green_repo, {"builds": 0, "latest_default_branch_build": null})
	denied(doc("scaffolded", "in-dev", ready, [repo], null)) == {"RLP-L104"}
}

test_l104_denies_no_repos if denied(doc("scaffolded", "in-dev", ready, [], null)) == {"RLP-L104"}

# --- RLP-L105, RLP-L106, RLP-L107: live ---

test_live_passes_when_ready if count(denied(move("in-dev", "live"))) == 0

test_l105_denies_no_owner if {
	denied(doc("in-dev", "live", object.union(ready, {"owner": ""}), [green_repo], null)) == {"RLP-L105"}
}

test_l106_denies_no_dashboard if {
	denied(doc("in-dev", "live", object.union(ready, {"dashboard_url": null}), [green_repo], null)) == {"RLP-L106"}
}

test_no_owner_and_no_dashboard_lists_both if {
	project := object.union(ready, {"owner": "", "dashboard_url": null})
	denied(doc("in-dev", "live", project, [green_repo], null)) == {"RLP-L105", "RLP-L106"}
}

test_l107_denies_a_failed_latest_build if {
	red := object.union(green_repo, {"latest_default_branch_build": {"commit": "def456", "passed": false}})
	denied(doc("in-dev", "live", ready, [green_repo, red], null)) == {"RLP-L107"}
}

test_l107_denies_a_repo_without_a_default_branch_build if {
	missing := object.union(green_repo, {"full_name": "acme/web", "latest_default_branch_build": null})
	denied(doc("in-dev", "live", ready, [green_repo, missing], null)) == {"RLP-L107"}
}

test_l107_denies_no_repos if denied(doc("in-dev", "live", ready, [], null)) == {"RLP-L107"}

test_l107_names_each_failing_repo if {
	red := object.union(green_repo, {"full_name": "acme/web", "latest_default_branch_build": {"commit": "def456", "passed": false}})
	missing := object.union(green_repo, {"full_name": "acme/jobs", "latest_default_branch_build": null})
	denials := lifecycle.deny with input as doc("deprecated", "live", ready, [green_repo, red, missing], null)
	count(denials) == 2
	some m1 in denials
	contains(m1, `repos[1].latest_default_branch_build: the latest build of repo "acme/web" on "main" (commit def456) failed`)
	some m2 in denials
	contains(m2, `repos[2].latest_default_branch_build: repo "acme/jobs" has no build on its default branch "main"`)
}

# --- RLP-L108: retired needs the teardown ---

test_l108_passes_with_a_teardown if count(denied(move("deprecated", "retired"))) == 0

test_l108_denies_no_teardown if denied(doc("deprecated", "retired", ready, [green_repo], null)) == {"RLP-L108"}

# --- no entry rules for deprecated ---

test_deprecated_has_no_entry_rules if {
	denied(doc("live", "deprecated", {"name": "api"}, [], null)) == set()
}

# --- results ---

test_results_list_every_checked_rule_in_order if {
	project := object.union(ready, {"owner": "", "dashboard_url": null})
	results := lifecycle.results[path] with input as doc("in-dev", "live", project, [green_repo], null)
	[r.rule | some r in results] == ["RLP-L101", "RLP-L105", "RLP-L106", "RLP-L107"]
	[r.passed | some r in results] == [true, false, false, true]
	results[0].messages == []
	count(results[1].messages) == 1
	results[1].description == "live needs an owner"
}

test_results_for_an_allowed_move_all_pass if {
	results := lifecycle.results[path] with input as move("proposed", "designed")
	results == [
		{"rule": "RLP-L101", "description": "the move is allowed", "passed": true, "messages": []},
		{"rule": "RLP-L102", "description": "designed needs a description", "passed": true, "messages": []},
	]
}

test_results_for_an_unknown_phase_hold_only_the_moves_rule if {
	results := lifecycle.results[path] with input as move("proposed", "launched")
	[r.rule | some r in results] == ["RLP-L101"]
	results[0].passed == false
}

test_every_rule_has_a_description if {
	rules := {id | some ids in lifecycle.entry_rules; some id in ids}
	every id in rules | {"RLP-L101"} {
		lifecycle.descriptions[id]
	}
}

test_every_phase_has_moves_and_entry_rules if {
	every p in lifecycle.phases {
		lifecycle.moves[p]
		lifecycle.entry_rules[p]
	}
}
