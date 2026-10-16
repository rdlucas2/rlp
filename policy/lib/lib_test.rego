package rlp.lib_test

import data.rlp.lib

# Rule ids in a set of deny messages, for compact assertions in every package's tests.
ids(messages) := {id |
	some m in messages
	id := regex.find_n(`RLP-[A-Z][0-9]{3}`, m, 1)[0]
}

test_norm if {
	lib.norm(".\\svc\\Taskfile.yml") == "svc/Taskfile.yml"
	lib.norm("./rlp.yaml") == "rlp.yaml"
}

test_join if {
	lib.join(".", "Taskfile.yml") == "Taskfile.yml"
	lib.join("svc/", "Taskfile.yml") == "svc/Taskfile.yml"
}

test_files_keyed_by_normalized_path if {
	lib.files["svc/x.yaml"] == {"a": 1} with input as [{"path": ".\\svc\\x.yaml", "contents": {"a": 1}}]
}

test_multi_document_file_becomes_a_list if {
	lib.files["k8s.yaml"] == [{"kind": "Service"}, {"kind": "Deployment"}] with input as [
		{"path": "k8s.yaml", "contents": {"kind": "Service"}},
		{"path": "k8s.yaml", "contents": {"kind": "Deployment"}},
	]
}

test_multi_document_file_does_not_break_contract_rules if {
	count(data.rlp.contract.presence.deny) == 0 with input as [
		{"path": "rlp.yaml", "contents": {"apiVersion": "rlp/v1alpha1"}},
		{"path": "Taskfile.yml", "contents": {}},
		{"path": "results.json", "contents": {"stage": "a"}},
		{"path": "results.json", "contents": {"stage": "b"}},
	]
}

test_files_empty_without_combined_input if {
	count(lib.files) == 0 with input as {"apiVersion": "rlp/v1alpha1"}
}

test_taskfile_prefers_go_task_order if {
	lib.taskfile(".") == "Taskfile.yml" with input as [
		{"path": "taskfile.yaml", "contents": {}},
		{"path": "Taskfile.yml", "contents": {}},
	]
}

test_ids_extracts_rule_ids if {
	ids({"[RLP-P001] rlp.yaml (file): x. Fix: y."}) == {"RLP-P001"}
}

test_platform_data_is_loaded if {
	data.platform.coverage_floor >= 0
	count(data.platform.required_tags) > 0
}
