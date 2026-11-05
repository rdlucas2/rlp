# The shared pieces of one rlp environment on AWS: an ECS cluster and an
# internet-facing load balancer with an HTTP listener. Every container-service
# component in the environment adds its own target group and host-header rule
# to that listener.
#
# With public_hosts set, the stack also serves those real DNS names over
# HTTPS: an ACM certificate, an HTTPS listener, a redirect from HTTP, and a
# Cognito user pool for the names that ask for sign-in. DNS stays wherever the
# domain lives; docs/aws.md lists the records to create.
#
# The outputs are also written to SSM under /rlp/<environment>/platform/, which
# is where the container-service module reads them, so app stacks never need
# this stack's state.
#
# Apply once per environment, as an administrator (see infra/README.md):
#   terraform init -backend-config="bucket=<state bucket>" -backend-config="key=rlp/platform/<env>.tfstate" -backend-config="region=<region>"
#   terraform apply -var environment=<env>

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  backend "s3" {
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      "rlp:project"     = "rlp-platform"
      "rlp:environment" = var.environment
      "rlp:managed-by"  = "rlp"
    }
  }
}

locals {
  name       = "rlp-${var.environment}"
  vpc_id     = var.vpc_id != "" ? var.vpc_id : data.aws_vpc.default[0].id
  subnet_ids = length(var.subnet_ids) > 0 ? var.subnet_ids : sort(data.aws_subnets.default[0].ids)
}

# S0 uses the account's default VPC unless told otherwise: public subnets, no
# NAT gateway, nothing extra to pay for.
data "aws_vpc" "default" {
  count = var.vpc_id == "" ? 1 : 0

  default = true
}

data "aws_subnets" "default" {
  count = length(var.subnet_ids) == 0 ? 1 : 0

  filter {
    name   = "vpc-id"
    values = [local.vpc_id]
  }

  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

resource "aws_ecs_cluster" "this" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "rlp ${var.environment}: the shared load balancer"
  vpc_id      = local.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  for_each = toset(var.allowed_cidrs)

  security_group_id = aws_security_group.alb.id
  description       = "HTTP"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = each.value
}

resource "aws_vpc_security_group_egress_rule" "to_vpc" {
  security_group_id = aws_security_group.alb.id
  description       = "Targets in the VPC"
  ip_protocol       = "tcp"
  from_port         = 1
  to_port           = 65535
  cidr_ipv4         = data.aws_vpc.selected.cidr_block
}

data "aws_vpc" "selected" {
  id = local.vpc_id
}

resource "aws_lb" "this" {
  name                       = local.name
  load_balancer_type         = "application"
  internal                   = false
  security_groups            = [aws_security_group.alb.id]
  subnets                    = local.subnet_ids
  drop_invalid_header_fields = true
}

# Requests whose Host matches no service get a 404.
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "fixed-response"

    fixed_response {
      content_type = "text/plain"
      message_body = "no rlp service for this host"
      status_code  = "404"
    }
  }
}

# --- public hosts: HTTPS and sign-in ---

data "aws_caller_identity" "current" {}

locals {
  public        = length(var.public_hosts) > 0
  hostnames     = sort(keys(var.public_hosts))
  authenticated = sort([for host, h in var.public_hosts : host if h.authenticate])
  auth          = length(local.authenticated) > 0
  auth_domain   = var.auth_domain_prefix != "" ? var.auth_domain_prefix : "${local.name}-${data.aws_caller_identity.current.account_id}"
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  for_each = local.public ? toset(var.allowed_cidrs) : toset([])

  security_group_id = aws_security_group.alb.id
  description       = "HTTPS"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = each.value
}

# DNS validation: the records go wherever the domain's DNS lives (output
# certificate_validation_records). Apply the certificate first with
# -target=aws_acm_certificate.this, create the records, then apply the rest:
# the validation below waits until ACM has issued the certificate.
resource "aws_acm_certificate" "this" {
  count = local.public ? 1 : 0

  domain_name               = local.hostnames[0]
  subject_alternative_names = slice(local.hostnames, 1, length(local.hostnames))
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_acm_certificate_validation" "this" {
  count = local.public ? 1 : 0

  certificate_arn = aws_acm_certificate.this[0].arn

  timeouts {
    create = "2h"
  }
}

resource "aws_lb_listener" "https" {
  count = local.public ? 1 : 0

  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.this[0].certificate_arn

  default_action {
    type = "fixed-response"

    fixed_response {
      content_type = "text/plain"
      message_body = "no rlp service for this host"
      status_code  = "404"
    }
  }
}

# A public host is HTTPS only: plain HTTP for it is redirected. Priorities
# 1-999 on the HTTP listener are the platform's; components use 1000 and up.
resource "aws_lb_listener_rule" "to_https" {
  for_each = var.public_hosts

  listener_arn = aws_lb_listener.http.arn
  priority     = index(local.hostnames, each.key) + 1

  action {
    type = "redirect"

    redirect {
      protocol    = "HTTPS"
      port        = "443"
      status_code = "HTTP_301"
    }
  }

  condition {
    host_header {
      values = [each.key]
    }
  }
}

# Sign-in for public hosts with authenticate = true. The load balancer runs
# the OIDC flow against Cognito's hosted UI and only forwards signed-in
# requests. Nobody can sign up: users are the auth_users list, or ones an
# administrator adds in the console.
resource "aws_cognito_user_pool" "this" {
  count = local.auth ? 1 : 0

  name                     = local.name
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  mfa_configuration        = "OPTIONAL"

  software_token_mfa_configuration {
    enabled = true
  }

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length                   = 14
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 7
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  username_configuration {
    case_sensitive = false
  }
}

resource "aws_cognito_user_pool_domain" "this" {
  count = local.auth ? 1 : 0

  domain       = local.auth_domain
  user_pool_id = aws_cognito_user_pool.this[0].id
}

resource "aws_cognito_user_pool_client" "alb" {
  count = local.auth ? 1 : 0

  name                                 = "${local.name}-alb"
  user_pool_id                         = aws_cognito_user_pool.this[0].id
  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = [for host in local.authenticated : "https://${host}/oauth2/idpresponse"]
  prevent_user_existence_errors        = "ENABLED"
}

resource "aws_cognito_user" "this" {
  for_each = local.auth ? toset(var.auth_users) : toset([])

  user_pool_id             = aws_cognito_user_pool.this[0].id
  username                 = each.value
  desired_delivery_mediums = ["EMAIL"]

  attributes = {
    email          = each.value
    email_verified = "true"
  }
}

# --- what app stacks read ---

locals {
  parameters = merge(
    {
      cluster_arn           = aws_ecs_cluster.this.arn
      listener_arn          = aws_lb_listener.http.arn
      alb_dns_name          = aws_lb.this.dns_name
      alb_security_group_id = aws_security_group.alb.id
      vpc_id                = local.vpc_id
      subnet_ids            = join(",", local.subnet_ids)
      domain                = var.domain
    },
    # Only what this environment has: SSM rejects empty values.
    { for k, v in { https_listener_arn = try(aws_lb_listener.https[0].arn, "") } : k => v if local.public },
    {
      for k, v in {
        auth_user_pool_arn       = try(aws_cognito_user_pool.this[0].arn, "")
        auth_user_pool_client_id = try(aws_cognito_user_pool_client.alb[0].id, "")
        auth_user_pool_domain    = try(aws_cognito_user_pool_domain.this[0].domain, "")
        auth_session_timeout     = tostring(var.auth_session_timeout)
      } : k => v if local.auth
    },
  )

  # One parameter per exposed component, /rlp/<env>/public/<repo>/<component>,
  # which the container-service module looks for.
  public_parameters = {
    for host, h in var.public_hosts : "${h.repo}/${h.component}" => jsonencode({
      host         = host
      authenticate = h.authenticate
      open_paths   = h.open_paths
    })
  }
}

resource "aws_ssm_parameter" "platform" {
  for_each = local.parameters

  name  = "/rlp/${var.environment}/platform/${each.key}"
  type  = "String"
  value = each.value
}

resource "aws_ssm_parameter" "public" {
  for_each = local.public_parameters

  name  = "/rlp/${var.environment}/public/${each.key}"
  type  = "String"
  value = each.value
}
