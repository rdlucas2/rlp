variable "region" {
  description = "AWS region for the state bucket. IAM and the OIDC provider are global."
  type        = string
  default     = "us-east-1"
}

variable "state_bucket" {
  description = "Name of the Terraform state bucket to create. Bucket names are global, so include something unique, such as the account id."
  type        = string
}

variable "github_repositories" {
  description = "GitHub repositories (owner/name) whose pipelines may assume the rlp roles."
  type        = list(string)
}

variable "deploy_branch" {
  description = "The only branch whose pipeline may assume rlp-deploy."
  type        = string
  default     = "main"
}
