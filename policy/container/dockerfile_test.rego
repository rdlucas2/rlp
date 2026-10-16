package rlp.container_test

import data.rlp.container
import data.rlp.lib_test.ids

ins(stage, cmd, value) := {"Cmd": cmd, "Flags": [], "JSON": false, "Stage": stage, "SubCmd": "", "Value": value}

dockerfile(instructions) := [{"path": "Dockerfile", "contents": instructions}]

good := [
	ins(0, "from", ["python:3.13-slim", "AS", "build"]),
	ins(0, "run", ["pip install ."]),
	ins(1, "from", ["build"]),
	ins(1, "user", ["10001"]),
]

test_good_dockerfile_passes if {
	count(container.deny) == 0 with input as dockerfile(good)
}

test_non_dockerfile_input_is_ignored if {
	count(container.deny) == 0 with input as [{"path": "rlp.yaml", "contents": {"kind": "Repo"}}]
}

test_d001_untagged_base if {
	some m in container.deny with input as dockerfile([ins(0, "from", ["python"]), ins(0, "user", ["app"])])
	startswith(m, `[RLP-D001] Dockerfile stage 0 FROM: base image "python" has no tag`)
	ids(container.deny) == {"RLP-D001"} with input as dockerfile([ins(0, "from", ["python"]), ins(0, "user", ["app"])])
}

test_d001_latest_tag if {
	ids(container.deny) == {"RLP-D001"} with input as dockerfile([ins(0, "from", ["ghcr.io/org/app:latest"]), ins(0, "user", ["app"])])
}

test_d001_registry_port_is_not_a_tag if {
	ids(container.deny) == {"RLP-D001"} with input as dockerfile([ins(0, "from", ["localhost:5000/app"]), ins(0, "user", ["app"])])
}

test_d001_digest_ok if {
	count(container.deny) == 0 with input as dockerfile([ins(0, "from", ["python@sha256:0123abcd"]), ins(0, "user", ["app"])])
}

test_d001_scratch_ok if {
	count(container.deny) == 0 with input as dockerfile([ins(0, "from", ["scratch"]), ins(0, "user", ["10001"])])
}

test_d002_no_user if {
	some m in container.deny with input as dockerfile([ins(0, "from", ["python:3.13"])])
	startswith(m, "[RLP-D002] Dockerfile stage 0 USER: the final stage sets no USER")
}

test_d002_root_user if {
	ids(container.deny) == {"RLP-D002"} with input as dockerfile(array.concat(good, [ins(1, "user", ["root"])]))
}

test_d002_user_in_earlier_stage_only if {
	df := [
		ins(0, "from", ["python:3.13", "AS", "build"]),
		ins(0, "user", ["app"]),
		ins(1, "from", ["python:3.13-slim"]),
	]
	ids(container.deny) == {"RLP-D002"} with input as dockerfile(df)
}

test_d001_build_arg_base_is_skipped if {
	count(container.deny) == 0 with input as dockerfile([ins(0, "from", ["${BASE_IMAGE}"]), ins(0, "user", ["app"])])
}
