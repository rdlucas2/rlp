# Container rules, evaluated on Dockerfiles as conftest parses them: a list of
# instructions, each with Cmd, Value and Stage.
package rlp.container

import data.rlp.lib

dockerfiles[path] := doc if {
	some path, doc in lib.files
	is_array(doc)
	count(doc) > 0
	every ins in doc {
		is_string(ins.Cmd)
		is_number(ins.Stage)
	}
}

froms contains {"file": path, "stage": ins.Stage, "image": ins.Value[0], "alias": alias(ins.Value)} if {
	some path, doc in dockerfiles
	some ins in doc
	ins.Cmd == "from"
}

alias(value) := lower(value[2]) if {
	count(value) >= 3
	lower(value[1]) == "as"
}

alias(value) := "" if not count(value) >= 3

# --- base images are pinned ---

deny contains lib.msg(
	"RLP-D001", f.file, sprintf("stage %d FROM", [f.stage]),
	sprintf("base image %q %v", [f.image, problem]),
	"pin the base image to a version tag or a digest, for example python:3.13-slim or python@sha256:...",
) if {
	some f in froms
	not exempt(f)
	problem := unpinned(f.image)
}

# scratch has no tag; a FROM that names an earlier stage is not a base image;
# a build argument is resolved at build time and checked in the build log.
exempt(f) if lower(f.image) == "scratch"

exempt(f) if {
	some g in froms
	g.file == f.file
	g.stage < f.stage
	g.alias != ""
	g.alias == lower(f.image)
}

exempt(f) if contains(f.image, "$")

unpinned(image) := "has no tag, so it means latest" if {
	not contains(image, "@")
	not contains(last_segment(image), ":")
}

unpinned(image) := "uses the latest tag" if {
	not contains(image, "@")
	endswith(last_segment(image), ":latest")
}

last_segment(image) := regex.replace(image, `^.*/`, "")

# --- the final stage runs as a non-root user ---

root_users := {"root", "0", "root:root", "0:0", "root:0", "0:root"}

deny contains lib.msg(
	"RLP-D002", path, sprintf("stage %d USER", [stage]),
	"the final stage sets no USER, so the container runs as root",
	"add a non-root USER (for example USER 10001) after installing packages in the final stage",
) if {
	some path, _ in dockerfiles
	stage := final_stage(path)
	count(users(path, stage)) == 0
}

deny contains lib.msg(
	"RLP-D002", path, sprintf("stage %d USER", [stage]),
	sprintf("the final stage runs as %q", [user]),
	"switch the last USER in the final stage to a non-root user, for example USER 10001",
) if {
	some path, _ in dockerfiles
	stage := final_stage(path)
	u := users(path, stage)
	user := u[count(u) - 1]
	lower(user) in root_users
}

final_stage(path) := max({ins.Stage | some ins in dockerfiles[path]})

users(path, stage) := [ins.Value[0] |
	some ins in dockerfiles[path]
	ins.Stage == stage
	ins.Cmd == "user"
]
