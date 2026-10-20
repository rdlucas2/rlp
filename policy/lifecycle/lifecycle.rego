# Lifecycle entry rules: may a project move to a phase (#91)?
#
# A project moves through proposed, designed, scaffolded, in-dev, live,
# deprecated and retired. The platform (rlp_platform.lifecycle) evaluates this
# package with `opa eval` before every move and records the results with the
# transition. Every rule is a deny rule: one failing rule rejects the move, and
# the moves themselves are a rule (RLP-L101), not code.
#
# Input is conftest's combined form, like every package in the bundle: an array
# of {"path", "contents"} entries. The rules read entries whose contents hold an
# rlp_transition document, which the platform builds from the project record:
#
#   {"rlp_transition": {
#     "project": {"name", "description", "owner", "dashboard_url"},
#     "from": "<current phase>",
#     "to": "<target phase>",
#     "repos": [{"full_name", "default_branch", "builds": <count of builds>,
#                "latest_default_branch_build": {"commit", "passed"} or null}],
#     "teardown": <the retirement workflow's teardown record> or null}}
#
# A repo's own files never carry that key, so `rlp policy check` finds nothing
# here. The platform image ships a copy of this file on its own
# (platform/src/rlp_platform/policy/lifecycle/), so it imports nothing from the
# rest of the bundle; tests/policy/test_lifecycle_copy.py keeps the two equal.
package rlp.lifecycle

phases := ["proposed", "designed", "scaffolded", "in-dev", "live", "deprecated", "retired"]

# moves[from] is every phase a project in `from` may move to: one step
# forward, deprecated from live or in-dev, retired only from deprecated, and
# back from deprecated to live.
moves := {
	"proposed": {"designed"},
	"designed": {"scaffolded"},
	"scaffolded": {"in-dev"},
	"in-dev": {"live", "deprecated"},
	"live": {"deprecated"},
	"deprecated": {"retired", "live"},
	"retired": set(),
}

# entry_rules[to] is every rule a move to `to` must pass, besides RLP-L101.
entry_rules := {
	"proposed": [],
	"designed": ["RLP-L102"],
	"scaffolded": ["RLP-L103"],
	"in-dev": ["RLP-L104"],
	"live": ["RLP-L105", "RLP-L106", "RLP-L107"],
	"deprecated": [],
	"retired": ["RLP-L108"],
}

descriptions := {
	"RLP-L101": "the move is allowed",
	"RLP-L102": "designed needs a description",
	"RLP-L103": "scaffolded needs at least one linked repo",
	"RLP-L104": "in-dev needs a build on record",
	"RLP-L105": "live needs an owner",
	"RLP-L106": "live needs a dashboard URL",
	"RLP-L107": "live needs the latest default-branch build of every linked repo to have passed",
	"RLP-L108": "retired needs the retirement workflow's teardown on record",
}

# --- input ---

transitions contains {"path": entry.path, "t": entry.contents.rlp_transition} if {
	is_array(input)
	some entry in input
	is_object(entry.contents)
	is_object(entry.contents.rlp_transition)
}

msg(id, path, field, problem, fix) := sprintf("[%s] %s %s: %s. Fix: %s.", [id, path, field, problem, fix])

failure(id, x, field, problem, fix) := {"rule": id, "path": x.path, "message": msg(id, x.path, field, problem, fix)}

applies(x, id) if id in object.get(entry_rules, x.t.to, [])

blank(value) if not is_string(value)

blank(value) if trim_space(value) == ""

project(x) := object.get(x.t, "project", {})

name(x) := object.get(project(x), "name", "")

repos(x) := object.get(x.t, "repos", [])

# --- RLP-L101: the move is allowed ---

allowed_move(t) if t.to in object.get(moves, t.from, set())

failures contains failure(
	"RLP-L101", x, "to",
	sprintf("project %q may not move from %q to %q", [name(x), object.get(x.t, "from", null), object.get(x.t, "to", null)]),
	"move one phase forward (proposed, designed, scaffolded, in-dev, live, deprecated, retired), to deprecated from live or in-dev, back to live from deprecated, or to retired only from deprecated",
) if {
	some x in transitions
	not allowed_move(x.t)
}

# --- RLP-L102: designed needs a description ---

failures contains failure(
	"RLP-L102", x, "project.description",
	sprintf("project %q has no description, which designed needs", [name(x)]),
	"describe the project before moving it to designed",
) if {
	some x in transitions
	applies(x, "RLP-L102")
	blank(object.get(project(x), "description", null))
}

# --- RLP-L103: scaffolded needs a linked repo ---

failures contains failure(
	"RLP-L103", x, "repos",
	sprintf("project %q has no linked repo, which scaffolded needs", [name(x)]),
	"scaffold a repo whose rlp.yaml names the project in metadata.project, or link an existing one",
) if {
	some x in transitions
	applies(x, "RLP-L103")
	count(repos(x)) == 0
}

# --- RLP-L104: in-dev needs a build ---

failures contains failure(
	"RLP-L104", x, "repos",
	sprintf("project %q has no build on record, which in-dev needs", [name(x)]),
	"run the pipeline on one of the project's repos so a build is recorded",
) if {
	some x in transitions
	applies(x, "RLP-L104")
	count([r | some r in repos(x); object.get(r, "builds", 0) > 0]) == 0
}

# --- RLP-L105: live needs an owner ---

failures contains failure(
	"RLP-L105", x, "project.owner",
	sprintf("project %q has no owner, which live needs", [name(x)]),
	"set the project's owner",
) if {
	some x in transitions
	applies(x, "RLP-L105")
	blank(object.get(project(x), "owner", null))
}

# --- RLP-L106: live needs a dashboard URL ---

failures contains failure(
	"RLP-L106", x, "project.dashboard_url",
	sprintf("project %q has no dashboard URL, which live needs", [name(x)]),
	"set the project's dashboard URL",
) if {
	some x in transitions
	applies(x, "RLP-L106")
	blank(object.get(project(x), "dashboard_url", null))
}

# --- RLP-L107: live needs a green pipeline on every repo's default branch ---

failures contains failure(
	"RLP-L107", x, "repos",
	sprintf("project %q has no linked repo, so no default-branch build has passed, which live needs", [name(x)]),
	"link the project's repos and run their pipelines on the default branch",
) if {
	some x in transitions
	applies(x, "RLP-L107")
	count(repos(x)) == 0
}

failures contains failure(
	"RLP-L107", x, sprintf("repos[%d].latest_default_branch_build", [i]),
	sprintf("repo %q has no build on its default branch %q, which live needs", [r.full_name, object.get(r, "default_branch", "")]),
	"run the pipeline on the repo's default branch until it passes",
) if {
	some x in transitions
	applies(x, "RLP-L107")
	some i, r in repos(x)
	not is_object(object.get(r, "latest_default_branch_build", null))
}

failures contains failure(
	"RLP-L107", x, sprintf("repos[%d].latest_default_branch_build", [i]),
	sprintf("the latest build of repo %q on %q (commit %s) failed, which live needs to have passed", [r.full_name, object.get(r, "default_branch", ""), object.get(build, "commit", "unknown")]),
	"fix the default branch until its pipeline passes",
) if {
	some x in transitions
	applies(x, "RLP-L107")
	some i, r in repos(x)
	build := object.get(r, "latest_default_branch_build", null)
	is_object(build)
	object.get(build, "passed", false) != true
}

# --- RLP-L108: retired needs the teardown recorded ---

failures contains failure(
	"RLP-L108", x, "teardown",
	sprintf("project %q has no teardown on record, which retired needs", [name(x)]),
	"run the retirement workflow, which tears the project down and records it",
) if {
	some x in transitions
	applies(x, "RLP-L108")
	not is_object(object.get(x.t, "teardown", null))
}

# --- results ---

deny contains f.message if some f in failures

# results[path] lists every rule the move at `path` was checked against, in
# rule order, with whether it passed and the messages of any failure.
results[path] := rules if {
	some x in transitions
	path := x.path
	rules := [{"rule": id, "description": descriptions[id], "passed": count(messages) == 0, "messages": messages} |
		some id in array.concat(["RLP-L101"], object.get(entry_rules, object.get(x.t, "to", null), []))
		messages := sort([f.message | some f in failures; f.path == path; f.rule == id])
	]
}
