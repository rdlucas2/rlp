# Set these as repository variables on each project repo (see infra/README.md).

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
