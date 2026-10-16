package rlp.terraform_test

import data.rlp.lib_test.ids
import data.rlp.terraform

tags := {"rlp:project": "hello", "rlp:environment": "dev", "rlp:managed-by": "terraform"}

rc(type, name, after) := {
	"address": sprintf("%v.%v", [type, name]),
	"type": type,
	"change": {"actions": ["create"], "after": after, "after_unknown": {}},
}

plan(changes) := [{"path": "plan.json", "contents": {"format_version": "1.2", "resource_changes": changes}}]

bucket := rc("aws_s3_bucket", "assets", {"bucket": "hello-assets", "tags": tags, "tags_all": tags})

block := rc("aws_s3_bucket_public_access_block", "assets", {
	"block_public_acls": true,
	"block_public_policy": true,
	"ignore_public_acls": true,
	"restrict_public_buckets": true,
})

test_compliant_plan_passes if {
	db := rc("aws_db_instance", "main", {"storage_encrypted": true, "tags": tags, "tags_all": tags})
	queue := rc("aws_sqs_queue", "jobs", {"sqs_managed_sse_enabled": true, "tags": tags, "tags_all": tags})
	count(terraform.deny) == 0 with input as plan([bucket, block, db, queue])
}

test_non_plan_input_is_ignored if {
	count(terraform.deny) == 0 with input as [{"path": "rlp.yaml", "contents": {"kind": "Repo"}}]
}

test_deletes_are_ignored if {
	gone := {"address": "aws_ebs_volume.old", "type": "aws_ebs_volume", "change": {"actions": ["delete"], "after": null}}
	count(terraform.deny) == 0 with input as plan([gone])
}

test_t001_missing_tags if {
	bare := rc("aws_s3_bucket", "assets", {"bucket": "hello-assets", "tags": {"rlp:project": "hello"}, "tags_all": {"rlp:project": "hello"}})
	some m in terraform.deny with input as plan([bare, block])
	startswith(m, "[RLP-T001] plan.json aws_s3_bucket.assets.tags: missing required tags rlp:environment, rlp:managed-by.")
	ids(terraform.deny) == {"RLP-T001"} with input as plan([bare, block])
}

test_t001_default_tags_count if {
	defaulted := rc("aws_s3_bucket", "assets", {"bucket": "hello-assets", "tags": null, "tags_all": tags})
	count(terraform.deny) == 0 with input as plan([defaulted, block])
}

test_t001_reads_tags_from_data if {
	ids(terraform.deny) == {"RLP-T001"} with input as plan([bucket, block]) with data.platform.required_tags as ["cost-center"]
}

test_t002_public_acl if {
	acl := rc("aws_s3_bucket_acl", "assets", {"acl": "public-read"})
	ids(terraform.deny) == {"RLP-T002"} with input as plan([bucket, block, acl])
}

test_t002_private_acl_ok if {
	acl := rc("aws_s3_bucket_acl", "assets", {"acl": "private"})
	count(terraform.deny) == 0 with input as plan([bucket, block, acl])
}

test_t002_weak_access_block if {
	weak := rc("aws_s3_bucket_public_access_block", "assets", object.union(block.change.after, {"restrict_public_buckets": false}))
	some m in terraform.deny with input as plan([bucket, weak])
	startswith(m, "[RLP-T002] plan.json aws_s3_bucket_public_access_block.assets.restrict_public_buckets: is not true")
}

test_t002_bucket_without_access_block if {
	ids(terraform.deny) == {"RLP-T002"} with input as plan([bucket])
}

test_t003_unencrypted_rds if {
	db := rc("aws_db_instance", "main", {"storage_encrypted": false, "tags": tags})
	some m in terraform.deny with input as plan([db])
	startswith(m, "[RLP-T003] plan.json aws_db_instance.main.storage_encrypted: is not true")
}

test_t003_unencrypted_queue if {
	queue := rc("aws_sqs_queue", "jobs", {"sqs_managed_sse_enabled": false, "tags": tags})
	ids(terraform.deny) == {"RLP-T003"} with input as plan([queue])
}

test_t003_topic_with_pending_kms_key if {
	topic := {
		"address": "aws_sns_topic.events",
		"type": "aws_sns_topic",
		"change": {"actions": ["create"], "after": {"tags": tags}, "after_unknown": {"kms_master_key_id": true}},
	}
	count(terraform.deny) == 0 with input as plan([topic])
}

test_t003_topic_without_kms if {
	topic := rc("aws_sns_topic", "events", {"tags": tags})
	ids(terraform.deny) == {"RLP-T003"} with input as plan([topic])
}
