package rlp.contract.structure_test

import data.rlp.contract.fixtures_test as f
import data.rlp.contract.structure
import data.rlp.lib_test.ids

patched(ops) := f.repo_with(json.patch(f.rlp_doc, ops))

template_patched(ops) := f.template_repo_with(json.patch(f.template_doc, ops))

test_repo_passes if {
	count(structure.deny) == 0 with input as f.repo
}

test_template_passes if {
	count(structure.deny) == 0 with input as f.template_repo
}

test_full_repo_passes if {
	doc := json.patch(f.rlp_doc, [{"op": "add", "path": "/components/0", "value": {
		"name": "web",
		"language": "typescript",
		"artifact": {"kind": "static", "path": "dist"},
		"runtime": {"kind": "static-site"},
		"reports": {"coverage": {"format": "lcov", "path": "coverage/lcov.info"}},
		"services": [{"name": "assets", "kind": "s3"}],
	}}])
	count(structure.deny) == 0 with input as f.repo_with(doc)
}

test_s001_unknown_api_version if {
	ids(structure.deny) == {"RLP-S001"} with input as patched([{"op": "replace", "path": "/apiVersion", "value": "rlp/v2"}])
}

test_s001_template_unknown_api_version if {
	ids(structure.deny) == {"RLP-S001"} with input as template_patched([{"op": "replace", "path": "/apiVersion", "value": "v1"}])
}

test_s001_message_points_at_field if {
	some m in structure.deny with input as patched([{"op": "remove", "path": "/apiVersion"}])
	startswith(m, "[RLP-S001] rlp.yaml apiVersion: is missing, not rlp/v1alpha1.")
}

test_s002_wrong_kind if {
	ids(structure.deny) == {"RLP-S002"} with input as patched([{"op": "replace", "path": "/kind", "value": "Template"}])
}

test_s003_missing_project if {
	ids(structure.deny) == {"RLP-S003"} with input as patched([{"op": "remove", "path": "/metadata/project"}])
}

test_s003_empty_components if {
	ids(structure.deny) == {"RLP-S003"} with input as patched([{"op": "replace", "path": "/components", "value": []}])
}

test_s003_component_without_runtime if {
	some m in structure.deny with input as patched([{"op": "remove", "path": "/components/0/runtime"}])
	startswith(m, "[RLP-S003] rlp.yaml components[0].runtime.kind:")
	ids(structure.deny) == {"RLP-S003"} with input as patched([{"op": "remove", "path": "/components/0/runtime"}])
}

test_s003_template_without_version if {
	ids(structure.deny) == {"RLP-S003"} with input as template_patched([{"op": "remove", "path": "/metadata/version"}])
}

test_s004_unknown_artifact_kind if {
	some m in structure.deny with input as patched([{"op": "replace", "path": "/components/0/artifact/kind", "value": "wheel"}])
	startswith(m, `[RLP-S004] rlp.yaml components[0].artifact.kind: is "wheel", not one of container-image, jar, static, zip.`)
}

test_s004_unknown_runtime_kind if {
	ids(structure.deny) == {"RLP-S004"} with input as patched([{"op": "replace", "path": "/components/0/runtime/kind", "value": "vm"}])
}

test_s004_unknown_coverage_format if {
	ops := [{"op": "add", "path": "/components/0/reports", "value": {"coverage": {"format": "clover", "path": "c.xml"}}}]
	ids(structure.deny) == {"RLP-S004"} with input as patched(ops)
}

test_s004_unknown_service_kind if {
	ops := [{"op": "add", "path": "/components/0/services", "value": [{"name": "q", "kind": "kafka"}]}]
	ids(structure.deny) == {"RLP-S004"} with input as patched(ops)
}

test_s004_template_unknown_runtime if {
	ids(structure.deny) == {"RLP-S004"} with input as template_patched([{"op": "replace", "path": "/produces/components/0/runtime", "value": "vm"}])
}

test_s005_container_service_without_port if {
	ids(structure.deny) == {"RLP-S005"} with input as patched([{"op": "remove", "path": "/components/0/runtime/port"}])
}

test_s005_function_without_handler if {
	ops := [{"op": "replace", "path": "/components/0/runtime", "value": {"kind": "function"}}]
	ids(structure.deny) == {"RLP-S005"} with input as patched(ops)
}

test_s005_function_with_handler if {
	ops := [{"op": "replace", "path": "/components/0/runtime", "value": {"kind": "function", "handler": "app.handler"}}]
	count(structure.deny) == 0 with input as patched(ops)
}

test_s003_template_without_produced_components if {
	ids(structure.deny) == {"RLP-S003"} with input as template_patched([{"op": "remove", "path": "/produces/components"}])
}

test_s003_template_component_without_runtime if {
	ids(structure.deny) == {"RLP-S003"} with input as template_patched([{"op": "remove", "path": "/produces/components/0/runtime"}])
}
