package rlp.contract.presence_test

import data.rlp.contract.fixtures_test as f
import data.rlp.contract.presence
import data.rlp.lib_test.ids

test_app_repo_passes if {
	count(presence.deny) == 0 with input as f.repo
}

test_template_repo_passes if {
	count(presence.deny) == 0 with input as f.template_repo
}

test_p001_missing_rlp_yaml if {
	ids(presence.deny) == {"RLP-P001"} with input as [f.taskfile]
}

test_p002_missing_taskfile if {
	ids(presence.deny) == {"RLP-P002"} with input as [{"path": "rlp.yaml", "contents": f.rlp_doc}]
}

test_p002_accepts_other_taskfile_names if {
	count(presence.deny) == 0 with input as [
		{"path": "rlp.yaml", "contents": f.rlp_doc},
		{"path": "Taskfile.yaml", "contents": {}},
	]
}

templated := object.union(f.rlp_doc, {"metadata": {"name": "hello", "project": "hello", "template": {"source": "gh:rdlucas2/rlp-template-python-fastapi"}}})

test_p003_template_without_answers if {
	ids(presence.deny) == {"RLP-P003"} with input as f.repo_with(templated)
}

test_p003_template_with_answers if {
	count(presence.deny) == 0 with input as array.concat(f.repo_with(templated), [{"path": ".copier-answers.yml", "contents": {}}])
}

test_p004_copier_without_rlp_template if {
	ids(presence.deny) == {"RLP-P004"} with input as [{"path": "copier.yaml", "contents": {}}]
}

test_p005_missing_conformance_answers if {
	doc := json.remove(f.template_doc, ["conformance/answers"])
	ids(presence.deny) == {"RLP-P005"} with input as f.template_repo_with(doc)
}

test_p006_answers_file_absent if {
	doc := json.patch(f.template_doc, [{"op": "replace", "path": "/conformance/answers", "value": "tests/nope.yml"}])
	ids(presence.deny) == {"RLP-P006"} with input as f.template_repo_with(doc)
}

test_message_names_file_field_and_fix if {
	some m in presence.deny with input as [f.taskfile]
	startswith(m, "[RLP-P001] rlp.yaml (file): ")
	contains(m, "Fix: ")
}
