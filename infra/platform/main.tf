# The shared pieces of one rlp environment on AWS: an ECS cluster and an
# internet-facing load balancer with an HTTP listener. Every container-service
# component in the environment adds its own target group and host-header rule
# to that listener.
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

locals {
  parameters = {
    cluster_arn           = aws_ecs_cluster.this.arn
    listener_arn          = aws_lb_listener.http.arn
    alb_dns_name          = aws_lb.this.dns_name
    alb_security_group_id = aws_security_group.alb.id
    vpc_id                = local.vpc_id
    subnet_ids            = join(",", local.subnet_ids)
    domain                = var.domain
  }
}

resource "aws_ssm_parameter" "platform" {
  for_each = local.parameters

  name  = "/rlp/${var.environment}/platform/${each.key}"
  type  = "String"
  value = each.value
}
