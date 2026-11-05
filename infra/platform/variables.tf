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

variable "public_hosts" {
  description = <<-EOT
    Real DNS names to serve over HTTPS, each mapped to one component: hostname => { repo, component, authenticate, open_paths }.
    rlp requests an ACM certificate for every name and adds an HTTPS listener; you create the DNS records yourself (see docs/aws.md).
    authenticate = true puts a Cognito sign-in in front of the component on that name, except for open_paths and the component's
    health path, which machines (CLI, webhooks, the deploy check) reach with their own credentials.
  EOT
  type = map(object({
    repo         = string
    component    = string
    authenticate = optional(bool, true)
    open_paths   = optional(list(string), [])
  }))
  default = {}

  validation {
    condition     = alltrue([for host in keys(var.public_hosts) : can(regex("^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]{2,63}$", host))])
    error_message = "Each public_hosts key must be a lowercase DNS name, for example rlp.example.com."
  }

  validation {
    condition     = length(distinct([for h in values(var.public_hosts) : "${h.repo}/${h.component}"])) == length(var.public_hosts)
    error_message = "A component can have one public host."
  }

  # A listener rule takes at most 5 condition values: the host, the health path and up to 3 open paths.
  validation {
    condition     = alltrue([for h in values(var.public_hosts) : length(h.open_paths) <= 3])
    error_message = "Each public host takes at most 3 open_paths."
  }
}

variable "auth_users" {
  description = "Email addresses to create Cognito users for. Each gets an invitation with a temporary password. Sign-up is closed: nobody else can create an account."
  type        = list(string)
  default     = []
}

variable "auth_domain_prefix" {
  description = "Prefix of the Cognito hosted sign-in domain (<prefix>.auth.<region>.amazoncognito.com). Empty uses rlp-<environment>-<account id>."
  type        = string
  default     = ""
}

variable "auth_session_timeout" {
  description = "Seconds a sign-in lasts before the load balancer sends the browser back to Cognito."
  type        = number
  default     = 43200
}
