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

  validation {
    condition     = alltrue([for repo in var.github_repositories : can(regex("^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$", repo))])
    error_message = "Each entry must be owner/name, for example rdlucas2/hello-api."
  }
}

variable "deploy_branch" {
  description = "The only branch whose pipeline may assume rlp-deploy."
  type        = string
  default     = "main"
}
