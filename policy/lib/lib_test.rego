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
