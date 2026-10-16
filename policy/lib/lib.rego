# Shared helpers for the rlp policy bundle.
#
# Every package reads conftest's combined input: run conftest with --combine
# from the repo root, so input is an array of {"path", "contents"} entries
# with paths relative to the root.
package rlp.lib

standard_verbs := ["setup", "lint", "test", "build", "package", "run", "smoke"]

api_version := "rlp/v1alpha1"

artifact_kinds := {"container-image", "zip", "jar", "static"}

runtime_kinds := {"container-service", "function", "static-site", "job"}

coverage_formats := {"cobertura", "lcov", "jacoco", "opencover"}

service_kinds := {"postgres", "redis", "s3", "sqs", "sns"}

taskfile_names := ["Taskfile.yml", "Taskfile.yaml", "taskfile.yml", "taskfile.yaml"]

# Normalize a path: forward slashes, no leading "./".
norm(p) := trim_prefix(replace(p, "\\", "/"), "./")

# files[path] is the parsed contents of every input file, keyed by normalized path.
# conftest --combine emits one entry per document, so a multi-document YAML file
# or a top-level JSON array arrives as several entries with the same path; those
# become a list of their documents.
paths := {norm(entry.path) | is_array(input); some entry in input}

files[path] := single_or_list(docs) if {
	some path in paths
	docs := [entry.contents | some entry in input; norm(entry.path) == path]
}

single_or_list(docs) := docs[0] if count(docs) == 1

single_or_list(docs) := docs if count(docs) > 1

has(path) if files[path]

# join(".", "Taskfile.yml") == "Taskfile.yml"; join("svc", "x") == "svc/x".
join(dir, name) := name if norm(dir) in {"", "."}

join(dir, name) := concat("/", [trim_suffix(norm(dir), "/"), name]) if not norm(dir) in {"", "."}

basename(p) := regex.replace(norm(p), `^.*/`, "")

rlp := files["rlp.yaml"]

template := files["rlp-template.yaml"]

is_template_repo if has("rlp-template.yaml")

is_template_repo if has("copier.yml")

is_template_repo if has("copier.yaml")

# taskfile(dir) is the path of the first Taskfile go-task would pick in dir.
taskfile(dir) := [p |
	some name in taskfile_names
	p := join(dir, name)
	has(p)
][0]

component_path(c) := object.get(c, "path", ".")

# A violation message: rule id, file, field path, problem and fix.
msg(id, file, field, problem, fix) := sprintf("[%s] %s %s: %s. Fix: %s.", [id, file, field, problem, fix])

sorted_list(s) := concat(", ", sort([x | some x in s]))
