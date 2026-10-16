# Platform rules: limits the platform sets, read from policy/platform/data.yaml.
package rlp.contract.platform

import data.rlp.lib

vex_file := ".rlp/vex.yaml"

day_ns := ((24 * 60) * 60) * 1000000000

# quality.coverage.minimum may not be below the platform floor.
deny contains lib.msg(
	"RLP-L001", "rlp.yaml", "quality.coverage.minimum",
	sprintf("is %v, below the platform floor of %v", [minimum, floor]),
	sprintf("raise quality.coverage.minimum to at least %v", [floor]),
) if {
	minimum := lib.rlp.quality.coverage.minimum
	floor := data.platform.coverage_floor
	is_number(minimum)
	minimum < floor
}

# A secret config entry never carries a default value.
deny contains lib.msg(
	"RLP-L002", "rlp.yaml", sprintf("components[%d].config[%d].default", [i, j]),
	sprintf("%v is secret: true but has a default value", [entry.name]),
	"remove the default; secrets come from the secret store, locally and in AWS",
) if {
	some i, c in lib.rlp.components
	some j, entry in c.config
	entry.secret == true
	object.get(entry, "default", null) != null
}

# Every VEX exception says why it exists ...
deny contains lib.msg(
	"RLP-L003", vex_file, sprintf("exceptions[%d].reason", [i]),
	sprintf("exception %v has no reason", [label(e)]),
	"state why the vulnerability does not apply or is accepted",
) if {
	some i, e in exceptions
	not non_blank(object.get(e, "reason", null))
}

# ... has an expiry date ...
deny contains lib.msg(
	"RLP-L004", vex_file, sprintf("exceptions[%d].expires", [i]),
	sprintf("exception %v has no valid expiry date", [label(e)]),
	"set expires to a date in YYYY-MM-DD form",
) if {
	some i, e in exceptions
	not expires_ns(e)
}

# ... and that date has not passed. An exception is valid through its expiry day.
deny contains lib.msg(
	"RLP-L005", vex_file, sprintf("exceptions[%d].expires", [i]),
	sprintf("exception %v expired on %v", [label(e), substring(e.expires, 0, 10)]),
	"fix or upgrade the vulnerable dependency, or re-review the exception and set a new expiry date",
) if {
	some i, e in exceptions
	time.now_ns() >= expires_ns(e) + day_ns
}

exceptions := e if {
	e := lib.files[vex_file].exceptions
	is_array(e)
}

# YAML loaders may turn an unquoted date into a timestamp string; accept both.
expires_ns(e) := time.parse_ns("2006-01-02", substring(e.expires, 0, 10)) if {
	is_string(e.expires)
	regex.match(`^[0-9]{4}-[0-9]{2}-[0-9]{2}`, e.expires)
}

label(e) := e.id if is_string(e.id)

label(e) := "(no id)" if not is_string(object.get(e, "id", null))

non_blank(x) if {
	is_string(x)
	trim_space(x) != ""
}
