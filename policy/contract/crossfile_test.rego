package rlp.contract.crossfile_test

import data.rlp.contract.crossfile
import data.rlp.contract.fixtures_test as f
import data.rlp.lib_test.ids

with_doc(ops, extra) := array.concat(f.repo_with(json.patch(f.rlp_doc, ops)), extra)

test_repo_passes if {
	count(crossfile.deny) == 0 with input as f.repo
}

test_x001_missing_task if {
	tf := {"path": "Taskfile.yml", "contents": {"version": "3", "tasks": object.remove(f.tasks, ["smoke"])}}
	some m in crossfile.deny with input as [{"path": "rlp.yaml", "contents": f.rlp_doc}, tf]
	startswith(m, "[RLP-X001] Taskfile.yml tasks.smoke: component api relies on `task smoke`")
	count(crossfile.deny) == 1 with input as [{"path": "rlp.yaml", "contents": f.rlp_doc}, tf]
}

test_x001_override_removes_need_for_task if {
	tf := {"path": "Taskfile.yml", "contents": {"version": "3", "tasks": object.remove(f.tasks, ["smoke"])}}
	doc := json.patch(f.rlp_doc, [{"op": "add", "path": "/components/0/verbs", "value": {"smoke": "curl -f localhost:8000/healthz"}}])
	count(crossfile.deny) == 0 with input as [{"path": "rlp.yaml", "contents": doc}, tf]
}

test_x001_uses_component_taskfile if {
	ops := [{"op": "add", "path": "/components/0/path", "value": "svc"}]
	extra := [{"path": "svc/Taskfile.yml", "contents": {"version": "3", "tasks": object.remove(f.tasks, ["lint"])}}]
	ids(crossfile.deny) == {"RLP-X001"} with input as with_doc(ops, extra)
}

test_x002_component_dir_without_taskfile if {
	ids(crossfile.deny) == {"RLP-X002"} with input as with_doc([{"op": "add", "path": "/components/0/path", "value": "svc"}], [])
}

test_x002_not_needed_when_every_verb_is_overridden if {
	verbs := {v: "make " | some v in ["setup", "lint", "test", "build", "package", "run", "smoke"]}
	ops := [
		{"op": "add", "path": "/components/0/path", "value": "svc"},
		{"op": "add", "path": "/components/0/verbs", "value": verbs},
	]
	count(crossfile.deny) == 0 with input as with_doc(ops, [])
}

test_x003_unknown_service if {
	ops := [
		{"op": "add", "path": "/components/0/services", "value": [{"name": "db", "kind": "postgres"}]},
		{"op": "add", "path": "/components/0/config", "value": [{"name": "CACHE_URL", "from": "service:cache"}]},
	]
	some m in crossfile.deny with input as with_doc(ops, [])
	startswith(m, `[RLP-X003] rlp.yaml components[0].config[0].from: refers to service "cache"`)
}

test_x003_declared_service if {
	ops := [
		{"op": "add", "path": "/components/0/services", "value": [{"name": "db", "kind": "postgres"}]},
		{"op": "add", "path": "/components/0/config", "value": [{"name": "DATABASE_URL", "from": "service:db"}]},
	]
	count(crossfile.deny) == 0 with input as with_doc(ops, [])
}

test_x004_duplicate_component_names if {
	ops := [{"op": "add", "path": "/components/-", "value": f.component}]
	some m in crossfile.deny with input as with_doc(ops, [])
	startswith(m, `[RLP-X004] rlp.yaml components[1].name: "api" is already used by components[0]`)
	ids(crossfile.deny) == {"RLP-X004"} with input as with_doc(ops, [])
}
