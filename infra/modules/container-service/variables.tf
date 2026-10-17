variable "environment" {
  description = "Environment name, for example dev. Prefixes names and selects the platform's SSM parameters."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,15}$", var.environment))
    error_message = "environment must be 1-16 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "repo" {
  description = "metadata.name of the rlp.yaml the component belongs to."
  type        = string
}

variable "component" {
  description = "Component name from rlp.yaml."
  type        = string
}

variable "image" {
  description = "Full image reference to run, for example ghcr.io/owner/repo/hello-api-api:abc123."
  type        = string
}

variable "port" {
  description = "runtime.port: the port the container listens on."
  type        = number
}

variable "health_path" {
  description = "Path the load balancer checks: runtime.health.readiness, else liveness, else /."
  type        = string
  default     = "/"
}

variable "cpu" {
  description = "Fargate task CPU units."
  type        = number
  default     = 256
}

variable "memory" {
  description = "Fargate task memory (MiB)."
  type        = number
  default     = 512
}

variable "desired_count" {
  description = "Number of tasks to run."
  type        = number
  default     = 1
}

variable "config" {
  description = "Plain config entries with a default: name => value. Each becomes an SSM parameter."
  type        = map(string)
  default     = {}
}

variable "unset_config" {
  description = "Plain config entries with no default. Each becomes an SSM parameter holding a placeholder for an operator to overwrite."
  type        = list(string)
  default     = []
}

variable "secrets" {
  description = "Names of secret: true config entries. Each becomes an empty Secrets Manager secret; an operator puts its value."
  type        = list(string)
  default     = []
}

variable "service_config" {
  description = "Config entries derived from a backing service (from: service:<name>): env var name => service name. In S0 each is a bring-your-own Secrets Manager secret holding the connection string."
  type        = map(string)
  default     = {}
}

variable "environment_variables" {
  description = "Extra non-secret environment variables for the container."
  type        = map(string)
  default     = {}
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention."
  type        = number
  default     = 30
}

variable "secret_recovery_window_days" {
  description = "Days a deleted secret can be restored. 0 deletes at once, so a torn-down environment can be recreated with the same names."
  type        = number
  default     = 7
}
