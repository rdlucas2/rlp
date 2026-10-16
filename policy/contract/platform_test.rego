package rlp.contract.platform_test

import data.rlp.contract.fixtures_test as f
import data.rlp.contract.platform
import data.rlp.lib_test.ids

# 2026-10-16T00:00:00Z
now := 1792108800000000000

patched(ops) := f.repo_with(json.patch(f.rlp_doc, ops))

with_vex(exceptions) := array.concat(f.repo, [{"path": ".rlp/vex.yaml", "contents": {"exceptions": exceptions}}])

ok_exception := {"id": "CVE-2026-0001", "reason": "Not reachable from our code path.", "expires": "2026-12-31"}

test_repo_passes if {
	count(platform.deny) == 0 with input as f.repo
}

test_l001_coverage_below_floor if {
	ops := [{"op": "add", "path": "/quality", "value": {"coverage": {"minimum": 10}}}]
	some m in platform.deny with input as patched(ops) with data.platform.coverage_floor as 70
	startswith(m, "[RLP-L001] rlp.yaml quality.coverage.minimum: is 10, below the platform floor of 70.")
}

test_l001_coverage_at_floor if {
	ops := [{"op": "add", "path": "/quality", "value": {"coverage": {"minimum": 70}}}]
	count(platform.deny) == 0 with input as patched(ops) with data.platform.coverage_floor as 70
}

test_l001_reads_floor_from_data if {
	ops := [{"op": "add", "path": "/quality", "value": {"coverage": {"minimum": 70}}}]
	ids(platform.deny) == {"RLP-L001"} with input as patched(ops) with data.platform.coverage_floor as 90
}

test_l002_secret_with_default if {
	ops := [{"op": "add", "path": "/components/0/config", "value": [{"name": "API_TOKEN", "secret": true, "default": "changeme"}]}]
	some m in platform.deny with input as patched(ops)
	startswith(m, "[RLP-L002] rlp.yaml components[0].config[0].default: API_TOKEN is secret: true")
}

test_l002_plain_default_ok if {
	ops := [{"op": "add", "path": "/components/0/config", "value": [{"name": "LOG_LEVEL", "default": "info"}, {"name": "API_TOKEN", "secret": true}]}]
	count(platform.deny) == 0 with input as patched(ops)
}

test_vex_valid if {
	count(platform.deny) == 0 with input as with_vex([ok_exception]) with time.now_ns as now
}

test_l003_missing_reason if {
	ids(platform.deny) == {"RLP-L003"} with input as with_vex([object.remove(ok_exception, ["reason"])]) with time.now_ns as now
}

test_l003_blank_reason if {
	ids(platform.deny) == {"RLP-L003"} with input as with_vex([object.union(ok_exception, {"reason": "  "})]) with time.now_ns as now
}

test_l004_missing_expiry if {
	ids(platform.deny) == {"RLP-L004"} with input as with_vex([object.remove(ok_exception, ["expires"])]) with time.now_ns as now
}

test_l004_unparseable_expiry if {
	ids(platform.deny) == {"RLP-L004"} with input as with_vex([object.union(ok_exception, {"expires": "2026-13-45"})]) with time.now_ns as now
}

test_l005_expired if {
	e := object.union(ok_exception, {"expires": "2026-10-15"})
	some m in platform.deny with input as with_vex([e]) with time.now_ns as now
	startswith(m, "[RLP-L005] .rlp/vex.yaml exceptions[0].expires: exception CVE-2026-0001 expired on 2026-10-15.")
}

test_l005_valid_through_expiry_day if {
	e := object.union(ok_exception, {"expires": "2026-10-16"})
	count(platform.deny) == 0 with input as with_vex([e]) with time.now_ns as now
}

test_l005_accepts_timestamp_form if {
	e := object.union(ok_exception, {"expires": "2026-10-01T00:00:00Z"})
	ids(platform.deny) == {"RLP-L005"} with input as with_vex([e]) with time.now_ns as now
}

test_vex_exception_without_id_is_labelled if {
	some m in platform.deny with input as with_vex([object.remove(ok_exception, ["id", "reason"])]) with time.now_ns as now
	contains(m, "exception (no id) has no reason")
}
