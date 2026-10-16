# Structure rules: required fields, enum values and apiVersion, re-checked in
# Rego so CI has a single gate. They mirror schemas/v1alpha1; the JSON Schema
# stays the authoring aid with editor support.
package rlp.contract.structure

import data.rlp.lib

contract_kinds := {"rlp.yaml": "Repo", "rlp-template.yaml": "Template"}

# --- apiVersion and kind (both files) ---

deny contains lib.msg(
	"RLP-S001", file, "apiVersion",
	sprintf("is %v, not %v", [describe(object.get(doc, "apiVersion", null)), lib.api_version]),
	sprintf("set apiVersion: %v", [lib.api_version]),
) if {
	some file, _ in contract_kinds
	doc := lib.files[file]
	object.get(doc, "apiVersion", null) != lib.api_version
}

deny contains lib.msg(
	"RLP-S002", file, "kind",
	sprintf("is %v, not %v", [describe(object.get(doc, "kind", null)), kind]),
	sprintf("set kind: %v in %v", [kind, file]),
) if {
	some file, kind in contract_kinds
	doc := lib.files[file]
	object.get(doc, "kind", null) != kind
}

# --- required fields ---

deny contains lib.msg("RLP-S003", file, field, "is required but missing or empty", sprintf("set %v", [field])) if {
	some [file, field] in missing
}

missing contains ["rlp.yaml", field] if {
	lib.has("rlp.yaml")
	some field in ["metadata.name", "metadata.project"]
	not filled(lib.rlp, split(field, "."))
}

missing contains ["rlp.yaml", "components"] if {
	lib.has("rlp.yaml")
	not non_empty_array(object.get(lib.rlp, "components", null))
}

missing contains ["rlp.yaml", sprintf("components[%d].%v", [i, field])] if {
	some i, c in components
	some field in ["name", "language", "artifact.kind", "runtime.kind"]
	not filled(c, split(field, "."))
}

missing contains ["rlp-template.yaml", field] if {
	lib.has("rlp-template.yaml")
	some field in ["metadata.name", "metadata.version"]
	not filled(lib.template, split(field, "."))
}

missing contains ["rlp-template.yaml", "produces.components"] if {
	lib.has("rlp-template.yaml")
	not non_empty_array(object.get(object.get(lib.template, "produces", {}), "components", null))
}

missing contains ["rlp-template.yaml", sprintf("produces.components[%d].%v", [i, field])] if {
	some i, c in template_components
	some field in ["language", "artifact", "runtime"]
	not filled(c, [field])
}

# --- enums ---

deny contains lib.msg(
	"RLP-S004", file, field,
	sprintf("is %v, not one of %v", [describe(value), lib.sorted_list(allowed)]),
	sprintf("set %v to one of %v", [field, lib.sorted_list(allowed)]),
) if {
	some [file, field, value, allowed] in enum_values
	not value in allowed
}

enum_values contains ["rlp.yaml", sprintf("components[%d].artifact.kind", [i]), c.artifact.kind, lib.artifact_kinds] if {
	some i, c in components
}

enum_values contains ["rlp.yaml", sprintf("components[%d].runtime.kind", [i]), c.runtime.kind, lib.runtime_kinds] if {
	some i, c in components
}

enum_values contains ["rlp.yaml", sprintf("components[%d].reports.coverage.format", [i]), c.reports.coverage.format, lib.coverage_formats] if {
	some i, c in components
}

enum_values contains ["rlp.yaml", sprintf("components[%d].services[%d].kind", [i, j]), s.kind, lib.service_kinds] if {
	some i, c in components
	some j, s in c.services
}

enum_values contains ["rlp-template.yaml", sprintf("produces.components[%d].artifact", [i]), c.artifact, lib.artifact_kinds] if {
	some i, c in template_components
}

enum_values contains ["rlp-template.yaml", sprintf("produces.components[%d].runtime", [i]), c.runtime, lib.runtime_kinds] if {
	some i, c in template_components
}

# --- fields a runtime kind needs ---

runtime_needs := {"container-service": "port", "function": "handler"}

deny contains lib.msg(
	"RLP-S005", "rlp.yaml", sprintf("components[%d].runtime.%v", [i, field]),
	sprintf("a %v runtime needs %v", [kind, field]),
	sprintf("set components[%d].runtime.%v", [i, field]),
) if {
	some i, c in components
	kind := c.runtime.kind
	field := runtime_needs[kind]
	not filled(c.runtime, [field])
}

# --- helpers ---

components := c if {
	c := lib.rlp.components
	is_array(c)
}

template_components := c if {
	c := lib.template.produces.components
	is_array(c)
}

filled(doc, path) if {
	value := object.get(doc, path, null)
	value != null
	value != ""
}

non_empty_array(x) if {
	is_array(x)
	count(x) > 0
}

describe(null) := "missing"

describe(x) := sprintf("%q", [x]) if is_string(x)

describe(x) := sprintf("%v", [x]) if {
	x != null
	not is_string(x)
}
