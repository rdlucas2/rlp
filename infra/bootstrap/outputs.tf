# Set these as repository variables on each project repo (see infra/README.md), or
# let `rlp onboard OWNER/REPO` set them.

output "state_bucket" {
  description = "RLP_TF_STATE_BUCKET"
  value       = aws_s3_bucket.state.bucket
}

output "plan_role_arn" {
  description = "RLP_AWS_PLAN_ROLE_ARN"
  value       = aws_iam_role.plan.arn
}

output "deploy_role_arn" {
  description = "RLP_AWS_DEPLOY_ROLE_ARN"
  value       = aws_iam_role.deploy.arn
}

output "region" {
  description = "RLP_AWS_REGION"
  value       = var.region
}

# Read back by `rlp onboard`, which re-applies the stack with every input unchanged
# except the repository it adds.

output "github_repositories" {
  description = "Repositories whose pipelines may assume the rlp roles."
  value       = var.github_repositories
}

output "deploy_branch" {
  description = "The only branch whose pipeline may assume rlp-deploy."
  value       = var.deploy_branch
}
