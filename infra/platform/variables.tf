variable "environment" {
  description = "Environment name, for example dev. Must match the deploy-env the golden pipeline is given."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,15}$", var.environment))
    error_message = "environment must be 1-16 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "region" {
  description = "AWS region."
  type        = string
  default     = "us-east-1"
}

variable "vpc_id" {
  description = "VPC to use. Empty uses the account's default VPC."
  type        = string
  default     = ""
}

variable "subnet_ids" {
  description = "Public subnets for the load balancer and tasks (at least two AZs). Empty uses the VPC's default subnets."
  type        = list(string)
  default     = []
}

variable "allowed_cidrs" {
  description = "IPv4 ranges allowed to reach the load balancer on port 80."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "domain" {
  description = "DNS suffix for service hosts: <component>-<repo>.<environment>.<domain>. Without a real zone, clients send the host as a Host header."
  type        = string
  default     = "rlp.internal"
}
