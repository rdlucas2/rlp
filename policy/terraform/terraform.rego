# Terraform rules, evaluated on plan JSON (`terraform show -json plan.tfplan`).
# Run with conftest --combine; every input file with resource_changes is a plan.
package rlp.terraform

import data.rlp.lib

plans[path] := doc if {
	some path, doc in lib.files
	is_array(doc.resource_changes)
}

# Resources the plan creates or updates, with their planned attributes.
changes contains {
	"file": path,
	"address": rc.address,
	"type": rc.type,
	"after": rc.change.after,
	"unknown": as_object(object.get(rc.change, "after_unknown", null)),
} if {
	some path, plan in plans
	some rc in plan.resource_changes
	some action in rc.change.actions
	action in {"create", "update"}
	is_object(rc.change.after)
}

# --- required tags ---

deny contains lib.msg(
	"RLP-T001", r.file, sprintf("%v.tags", [r.address]),
	sprintf("missing required tags %v", [lib.sorted_list(absent)]),
	sprintf("add %v to the resource's tags or to the provider's default_tags", [lib.sorted_list(absent)]),
) if {
	some r in changes
	taggable(r.after)
	present := {k | some k, _ in effective_tags(r.after)}
	absent := {t | some t in data.platform.required_tags} - present
	count(absent) > 0
}

taggable(after) if "tags" in object.keys(after)

taggable(after) if "tags_all" in object.keys(after)

effective_tags(after) := object.union(as_object(object.get(after, "tags", null)), as_object(object.get(after, "tags_all", null)))

as_object(x) := x if is_object(x)

as_object(x) := {} if not is_object(x)

# --- no public S3 ---

public_acls := {"public-read", "public-read-write", "authenticated-read"}

block_settings := ["block_public_acls", "block_public_policy", "ignore_public_acls", "restrict_public_buckets"]

deny contains lib.msg(
	"RLP-T002", r.file, sprintf("%v.acl", [r.address]),
	sprintf("grants the public ACL %q", [r.after.acl]),
	"use acl = \"private\" (or drop the ACL) and share data through CloudFront or presigned URLs",
) if {
	some r in changes
	r.type in {"aws_s3_bucket_acl", "aws_s3_bucket"}
	r.after.acl in public_acls
}

deny contains lib.msg(
	"RLP-T002", r.file, sprintf("%v.%v", [r.address, setting]),
	"is not true, so the bucket can be made public",
	sprintf("set %v = true", [setting]),
) if {
	some r in changes
	r.type == "aws_s3_bucket_public_access_block"
	some setting in block_settings
	object.get(r.after, setting, false) != true
}

deny contains lib.msg(
	"RLP-T002", r.file, r.address,
	"the plan creates an S3 bucket but no aws_s3_bucket_public_access_block",
	"add an aws_s3_bucket_public_access_block for the bucket with all four settings true",
) if {
	some r in changes
	r.type == "aws_s3_bucket"
	not has_access_block(r.file)
}

has_access_block(file) if {
	some r in changes
	r.file == file
	r.type == "aws_s3_bucket_public_access_block"
}

# --- encryption at rest ---

# Resource type -> attribute that must be true. S3 encrypts every object by
# default (SSE-S3), so buckets are not listed.
encryption_flags := {
	"aws_db_instance": "storage_encrypted",
	"aws_rds_cluster": "storage_encrypted",
	"aws_ebs_volume": "encrypted",
	"aws_efs_file_system": "encrypted",
	"aws_elasticache_replication_group": "at_rest_encryption_enabled",
}

deny contains lib.msg(
	"RLP-T003", r.file, sprintf("%v.%v", [r.address, flag]),
	"is not true, so data is stored unencrypted",
	sprintf("set %v = true", [flag]),
) if {
	some r in changes
	flag := encryption_flags[r.type]
	object.get(r.after, flag, false) != true
}

deny contains lib.msg(
	"RLP-T003", r.file, sprintf("%v.sqs_managed_sse_enabled", [r.address]),
	"the queue has neither SQS-managed nor KMS encryption",
	"set sqs_managed_sse_enabled = true or kms_master_key_id",
) if {
	some r in changes
	r.type == "aws_sqs_queue"
	object.get(r.after, "sqs_managed_sse_enabled", false) != true
	not key_set(r)
}

deny contains lib.msg(
	"RLP-T003", r.file, sprintf("%v.kms_master_key_id", [r.address]),
	"the topic is not encrypted with KMS",
	"set kms_master_key_id, for example to alias/aws/sns",
) if {
	some r in changes
	r.type == "aws_sns_topic"
	not key_set(r)
}

# A KMS key is set, or will be once a key created in the same plan exists.
key_set(r) if {
	key := object.get(r.after, "kms_master_key_id", null)
	is_string(key)
	key != ""
}

key_set(r) if r.unknown.kms_master_key_id == true
