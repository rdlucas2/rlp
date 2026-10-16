# Cross-file rules: references between rlp.yaml and the rest of the repo.
package rlp.contract.crossfile

import data.rlp.lib

components := c if {
	c := lib.rlp.components
	is_array(c)
}

# Every verb a component runs through go-task exists in that component's Taskfile.
deny contains lib.msg(
	"RLP-X001", taskfile, sprintf("tasks.%v", [verb]),
	sprintf("component %v relies on `task %v` but the Taskfile does not define it", [c.name, verb]),
	sprintf("add a %v task to %v, or set components[%d].verbs.%v in rlp.yaml", [verb, taskfile, i, verb]),
) if {
	some i, c in components
	taskfile := lib.taskfile(lib.component_path(c))
	some verb in lib.standard_verbs
	not overridden(c, verb)
	not lib.files[taskfile].tasks[verb]
}

# A component outside the repo root that relies on go-task needs its own Taskfile.
# (A missing root Taskfile is RLP-P002.)
deny contains lib.msg(
	"RLP-X002", "rlp.yaml", sprintf("components[%d].path", [i]),
	sprintf("component %v runs verbs with go-task but %v has no Taskfile", [c.name, dir]),
	sprintf("add %v implementing the standard verbs, or override every verb in components[%d].verbs", [lib.join(dir, "Taskfile.yml"), i]),
) if {
	some i, c in components
	dir := lib.norm(lib.component_path(c))
	not dir in {"", "."}
	not lib.taskfile(dir)
	some verb in lib.standard_verbs
	not overridden(c, verb)
}

# Every config.from: service:<name> names a service the component declares.
deny contains lib.msg(
	"RLP-X003", "rlp.yaml", sprintf("components[%d].config[%d].from", [i, j]),
	sprintf("refers to service %q, which components[%d].services does not declare", [name, i]),
	sprintf("declare a service named %v in components[%d].services, or fix the reference", [name, i]),
) if {
	some i, c in components
	some j, entry in c.config
	startswith(entry.from, "service:")
	name := substring(entry.from, count("service:"), -1)
	not name in {s.name | some s in c.services}
}

# Component names are unique.
deny contains lib.msg(
	"RLP-X004", "rlp.yaml", sprintf("components[%d].name", [j]),
	sprintf("%q is already used by components[%d]", [c.name, i]),
	"give every component a unique name",
) if {
	some i, c in components
	some j, d in components
	i < j
	c.name == d.name
}

overridden(c, verb) if {
	cmd := c.verbs[verb]
	is_string(cmd)
	cmd != ""
}
