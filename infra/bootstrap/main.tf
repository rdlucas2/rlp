# One-time bootstrap of an AWS account for rlp. Run it once, by hand, as an
# administrator (see infra/README.md); the golden pipeline never runs it.
#
# Creates:
#   - the Terraform state bucket (versioned, encrypted, private; Terraform's
#     native S3 lock files do the locking, so no DynamoDB table)
#   - the GitHub Actions OIDC identity provider
#   - rlp-plan: read-only role any ref of the listed repos can assume, for plans on PRs
#   - rlp-deploy: role only the listed repos' default branch can assume, limited
#     to resources tagged rlp:managed-by = rlp, IAM roles under /rlp/ and the
#     app state keys
#
# No long-lived AWS keys are created: the pipeline exchanges its GitHub OIDC
# token for short-lived credentials of one of these roles.
#
# This stack keeps local state (terraform.tfstate here, gitignored). Keep the
# file somewhere safe, or migrate it into the bucket afterwards.

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      "rlp:project"     = "rlp-platform"
      "rlp:environment" = "global"
      "rlp:managed-by"  = "rlp"
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

locals {
  account   = data.aws_caller_identity.current.account_id
  partition = data.aws_partition.current.partition
  # App stacks keep state under rlp/apps/; the platform stacks under rlp/platform/.
  app_state = "${aws_s3_bucket.state.arn}/rlp/apps/*"
  rlp_roles = "arn:${local.partition}:iam::${local.account}:role/rlp/*"

  # The prefix of the OIDC token's sub claim for each repo, in both formats
  # GitHub issues: repo:<owner>/<name> and, with immutable IDs,
  # repo:<owner>@<owner id>/<name>@<repo id>. Owner and repo names can't
  # contain "@", "/" or ":", so the wildcards only match the numeric IDs.
  github_subjects = flatten([
    for repo in var.github_repositories : [
      "repo:${repo}",
      "repo:${split("/", repo)[0]}@*/${split("/", repo)[1]}@*",
    ]
  ])
}

# --- state bucket ---

resource "aws_s3_bucket" "state" {
  bucket = var.state_bucket
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

data "aws_iam_policy_document" "state_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = data.aws_iam_policy_document.state_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.state]
}

# --- GitHub OIDC ---

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "plan_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [for subject in local.github_subjects : "${subject}:*"]
    }
  }
}

data "aws_iam_policy_document" "deploy_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [for subject in local.github_subjects : "${subject}:ref:refs/heads/${var.deploy_branch}"]
    }
  }
}

# --- what a plan may read ---

data "aws_iam_policy_document" "read" {
  statement {
    sid = "Describe"
    actions = [
      "ec2:Describe*",
      "ecs:Describe*",
      "ecs:List*",
      "elasticloadbalancing:Describe*",
      "logs:DescribeLogGroups",
      "logs:ListTagsForResource",
      "logs:ListTagsLogGroup",
      "secretsmanager:DescribeSecret",
      "secretsmanager:GetResourcePolicy",
      "ssm:DescribeParameters",
      "sts:GetCallerIdentity",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "RlpParameters"
    actions   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath", "ssm:ListTagsForResource"]
    resources = ["arn:${local.partition}:ssm:*:${local.account}:parameter/rlp/*"]
  }

  statement {
    sid       = "RlpRoles"
    actions   = ["iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies"]
    resources = [local.rlp_roles]
  }

  statement {
    sid       = "StateList"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]
  }

  statement {
    sid       = "StateRead"
    actions   = ["s3:GetObject"]
    resources = [local.app_state]
  }

  # Terraform's native S3 locking writes and removes a .tflock object.
  statement {
    sid       = "StateLock"
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.state.arn}/rlp/apps/*.tflock"]
  }
}

# --- what a deploy may change ---

data "aws_iam_policy_document" "deploy" {
  source_policy_documents = [data.aws_iam_policy_document.read.json]

  statement {
    sid       = "StateWrite"
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = [local.app_state]
  }

  # Create only resources that carry rlp:managed-by = rlp.
  statement {
    sid = "CreateTagged"
    actions = [
      "ec2:CreateSecurityGroup",
      "ec2:CreateTags",
      "ecs:CreateService",
      "ecs:RegisterTaskDefinition",
      "ecs:TagResource",
      "elasticloadbalancing:AddTags",
      "elasticloadbalancing:CreateRule",
      "elasticloadbalancing:CreateTargetGroup",
      "logs:CreateLogGroup",
      "logs:TagLogGroup",
      "logs:TagResource",
      "secretsmanager:CreateSecret",
      "secretsmanager:TagResource",
      "ssm:AddTagsToResource",
      "ssm:PutParameter",
    ]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/rlp:managed-by"
      values   = ["rlp"]
    }
  }

  # EC2 also authorizes CreateSecurityGroup against the VPC, which carries no
  # rlp tag; the new security group itself must still be tagged (CreateTagged).
  statement {
    sid       = "SecurityGroupsInVpcs"
    actions   = ["ec2:CreateSecurityGroup"]
    resources = ["arn:${local.partition}:ec2:*:${local.account}:vpc/*"]
  }

  # Rule changes are also authorized against the rule itself, which is new or
  # untagged (the default egress rule). Each of these actions also requires
  # the security group, so ManageTagged still limits them to rlp-tagged groups.
  statement {
    sid = "SecurityGroupRules"
    actions = [
      "ec2:AuthorizeSecurityGroupEgress",
      "ec2:AuthorizeSecurityGroupIngress",
      "ec2:ModifySecurityGroupRules",
      "ec2:RevokeSecurityGroupEgress",
      "ec2:RevokeSecurityGroupIngress",
      "ec2:UpdateSecurityGroupRuleDescriptionsEgress",
      "ec2:UpdateSecurityGroupRuleDescriptionsIngress",
    ]
    resources = ["arn:${local.partition}:ec2:*:${local.account}:security-group-rule/*"]
  }

  # Change and delete only resources that already carry the tag.
  statement {
    sid = "ManageTagged"
    actions = [
      "ec2:AuthorizeSecurityGroupEgress",
      "ec2:AuthorizeSecurityGroupIngress",
      "ec2:DeleteSecurityGroup",
      "ec2:DeleteTags",
      "ec2:ModifySecurityGroupRules",
      "ec2:RevokeSecurityGroupEgress",
      "ec2:RevokeSecurityGroupIngress",
      "ec2:UpdateSecurityGroupRuleDescriptionsEgress",
      "ec2:UpdateSecurityGroupRuleDescriptionsIngress",
      "ecs:DeleteService",
      "ecs:UntagResource",
      "ecs:UpdateService",
      "elasticloadbalancing:DeleteRule",
      "elasticloadbalancing:DeleteTargetGroup",
      "elasticloadbalancing:ModifyRule",
      "elasticloadbalancing:ModifyTargetGroup",
      "elasticloadbalancing:ModifyTargetGroupAttributes",
      "elasticloadbalancing:RemoveTags",
      "elasticloadbalancing:SetRulePriorities",
      "logs:DeleteLogGroup",
      "logs:DeleteRetentionPolicy",
      "logs:PutRetentionPolicy",
      "logs:UntagLogGroup",
      "logs:UntagResource",
      "secretsmanager:DeleteSecret",
      "secretsmanager:RestoreSecret",
      "secretsmanager:UntagResource",
      "secretsmanager:UpdateSecret",
      "ssm:DeleteParameter",
      "ssm:PutParameter",
      "ssm:RemoveTagsFromResource",
    ]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/rlp:managed-by"
      values   = ["rlp"]
    }
  }

  # Task definitions have no resource-level permissions for these.
  statement {
    sid       = "TaskDefinitions"
    actions   = ["ecs:DeregisterTaskDefinition", "ecs:DescribeTaskDefinition"]
    resources = ["*"]
  }

  # IAM: only roles under the /rlp/ path, and only passed to ECS tasks.
  statement {
    sid = "RlpRolesWrite"
    actions = [
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:DeleteRolePolicy",
      "iam:PutRolePolicy",
      "iam:TagRole",
      "iam:UntagRole",
      "iam:UpdateAssumeRolePolicy",
      "iam:UpdateRole",
      "iam:UpdateRoleDescription",
      "iam:ListInstanceProfilesForRole",
    ]
    resources = [local.rlp_roles]
  }

  statement {
    sid       = "PassRlpRolesToEcs"
    actions   = ["iam:PassRole"]
    resources = [local.rlp_roles]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_policy" "read" {
  name        = "rlp-plan"
  description = "rlp: what a Terraform plan of an app stack may read"
  policy      = data.aws_iam_policy_document.read.json
}

resource "aws_iam_policy" "deploy" {
  name        = "rlp-deploy"
  description = "rlp: what the golden pipeline may change, limited to rlp-tagged resources"
  policy      = data.aws_iam_policy_document.deploy.json
}

resource "aws_iam_role" "plan" {
  name                 = "rlp-plan"
  description          = "rlp: Terraform plans from GitHub Actions on pull requests"
  assume_role_policy   = data.aws_iam_policy_document.plan_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role" "deploy" {
  name                 = "rlp-deploy"
  description          = "rlp: Terraform apply from GitHub Actions on the default branch"
  assume_role_policy   = data.aws_iam_policy_document.deploy_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "plan" {
  role       = aws_iam_role.plan.name
  policy_arn = aws_iam_policy.read.arn
}

resource "aws_iam_role_policy_attachment" "deploy" {
  role       = aws_iam_role.deploy.name
  policy_arn = aws_iam_policy.deploy.arn
}
