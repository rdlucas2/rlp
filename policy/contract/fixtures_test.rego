# Shared inputs for the contract tests.
package rlp.contract.fixtures_test

tasks := {v: {"cmds": ["true"]} | some v in ["setup", "lint", "test", "build", "package", "run", "smoke"]}

taskfile := {"path": "Taskfile.yml", "contents": {"version": "3", "tasks": tasks}}

component := {
	"name": "api",
	"language": "python",
	"artifact": {"kind": "container-image"},
	"runtime": {"kind": "container-service", "port": 8000},
}

rlp_doc := {
	"apiVersion": "rlp/v1alpha1",
	"kind": "Repo",
	"metadata": {"name": "hello", "project": "hello"},
	"components": [component],
}

repo := [{"path": "rlp.yaml", "contents": rlp_doc}, taskfile]

# repo with rlp.yaml replaced by doc
repo_with(doc) := [{"path": "rlp.yaml", "contents": doc}, taskfile]

template_doc := {
	"apiVersion": "rlp/v1alpha1",
	"kind": "Template",
	"metadata": {"name": "python-fastapi", "version": "0.1.0"},
	"produces": {"components": [{"language": "python", "artifact": "container-image", "runtime": "container-service"}]},
	"conformance": {"answers": "tests/default-answers.yml"},
}

template_repo := [
	{"path": "copier.yml", "contents": {}},
	{"path": "rlp-template.yaml", "contents": template_doc},
	{"path": "tests/default-answers.yml", "contents": {}},
]

template_repo_with(doc) := [
	{"path": "copier.yml", "contents": {}},
	{"path": "rlp-template.yaml", "contents": doc},
	{"path": "tests/default-answers.yml", "contents": {}},
]
