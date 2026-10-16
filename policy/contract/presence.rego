# Presence rules: the contract files a repo must carry.
package rlp.contract.presence

import data.rlp.lib

deny contains lib.msg(
	"RLP-P001", "rlp.yaml", "(file)",
	"no rlp.yaml at the repo root",
	"add rlp.yaml at the repo root (see examples/rlp.yaml), or render the repo from an rlp template",
) if {
	not lib.is_template_repo
	not lib.has("rlp.yaml")
}

deny contains lib.msg(
	"RLP-P002", "Taskfile.yml", "(file)",
	"no Taskfile at the repo root",
	"add Taskfile.yml implementing the standard verbs (setup, lint, test, build, package, run, smoke)",
) if {
	lib.has("rlp.yaml")
	not lib.taskfile(".")
}

deny contains lib.msg(
	"RLP-P003", ".copier-answers.yml", "(file)",
	sprintf("rlp.yaml sets metadata.template (%v) but .copier-answers.yml is missing", [lib.rlp.metadata.template.source]),
	"commit the .copier-answers.yml that copier wrote, or remove metadata.template if the repo was not rendered from a template",
) if {
	lib.rlp.metadata.template
	not lib.has(".copier-answers.yml")
}

deny contains lib.msg(
	"RLP-P004", "rlp-template.yaml", "(file)",
	"the repo has a copier config but no rlp-template.yaml",
	"add rlp-template.yaml at the repo root (see examples/rlp-template.yaml)",
) if {
	some f in {"copier.yml", "copier.yaml"}
	lib.has(f)
	not lib.has("rlp-template.yaml")
}

deny contains lib.msg(
	"RLP-P005", "rlp-template.yaml", "conformance.answers",
	"is not set",
	"set conformance.answers to the Copier answers file the conformance loop renders with, for example tests/default-answers.yml",
) if {
	lib.has("rlp-template.yaml")
	not answers_set
}

deny contains lib.msg(
	"RLP-P006", "rlp-template.yaml", "conformance.answers",
	sprintf("points at %v, which is not in the repo", [lib.template.conformance.answers]),
	"add that answers file or point conformance.answers at an existing one",
) if {
	answers_set
	not lib.has(lib.norm(lib.template.conformance.answers))
}

answers_set if {
	is_string(lib.template.conformance.answers)
	lib.template.conformance.answers != ""
}
