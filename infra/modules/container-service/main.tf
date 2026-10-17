# One rlp container-service component on ECS Fargate, behind the environment's
# shared load balancer. The shared pieces (cluster, load balancer, listener,
# network) come from infra/platform, which publishes them as SSM parameters
# under /rlp/<environment>/platform/.
#
# Config mapping:
#   plain entry with a default -> SSM parameter (String), injected as a secret ref
#   plain entry, no default     -> SSM parameter with a placeholder an operator overwrites
#   secret: true                -> empty Secrets Manager secret an operator fills
#   from: service:<name>        -> bring-your-own Secrets Manager secret (S0)
# Terraform never holds a secret value: it creates the containers only.

locals {
  name     = "${var.environment}-${var.repo}-${var.component}"
  platform = "/rlp/${var.environment}/platform"
  prefix   = "/rlp/${var.environment}/${var.repo}/${var.component}"
  # Target group names are limited to 32 characters, IAM role names to 64.
  short     = substr(local.name, 0, 50)
  tg_name   = "rlp-${substr(md5(local.name), 0, 24)}"
  log_group = "/rlp/${var.environment}/${var.repo}/${var.component}"
  host      = "${var.component}-${var.repo}.${var.environment}.${local.shared["domain"]}"
  # The platform's published identifiers (cluster, listener, VPC, subnets, DNS
  # names). The provider marks every SSM parameter value sensitive, but these
  # are not secrets: unmarking them keeps plans readable and lets the root
  # module output the service URL.
  shared = { for k, p in data.aws_ssm_parameter.platform : k => nonsensitive(p.value) }
  # Listener rule priorities are unique per listener: derive a stable one from the name.
  priority = parseint(substr(md5(local.name), 0, 4), 16) % 49000 + 1000

  secret_descriptions = merge(
    { for name in var.secrets : name => "rlp ${local.name}: secret config entry ${name}" },
    { for name, service in var.service_config : name => "rlp ${local.name}: connection string for service ${service} (bring your own)" },
  )

  environment = merge(var.environment_variables, {
    RLP_ENVIRONMENT = var.environment
    RLP_COMPONENT   = var.component
  })

  parameter_arns = concat(
    [for p in aws_ssm_parameter.config : p.arn],
    [for p in aws_ssm_parameter.unset : p.arn],
  )
  secret_arns = [for s in aws_secretsmanager_secret.this : s.arn]
}

data "aws_region" "current" {}

data "aws_ssm_parameter" "platform" {
  for_each = toset(["cluster_arn", "listener_arn", "alb_dns_name", "alb_security_group_id", "vpc_id", "subnet_ids", "domain"])

  name = "${local.platform}/${each.key}"
}

# --- config and secrets ---

resource "aws_ssm_parameter" "config" {
  for_each = var.config

  name  = "${local.prefix}/${each.key}"
  type  = "String"
  value = each.value
}

resource "aws_ssm_parameter" "unset" {
  for_each = toset(var.unset_config)

  name        = "${local.prefix}/${each.key}"
  description = "Placeholder: rlp.yaml gives no default. Overwrite with the real value."
  type        = "String"
  value       = "unset"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_secretsmanager_secret" "this" {
  for_each = local.secret_descriptions

  name                    = "rlp/${var.environment}/${var.repo}/${var.component}/${each.key}"
  description             = each.value
  recovery_window_in_days = var.secret_recovery_window_days
}

# --- logs and IAM ---

resource "aws_cloudwatch_log_group" "this" {
  name              = local.log_group
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# The execution role starts the task: writes logs and resolves config and secrets.
resource "aws_iam_role" "execution" {
  name               = "${local.short}-exec"
  path               = "/rlp/"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "execution" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.this.arn}:*"]
  }

  dynamic "statement" {
    for_each = length(local.parameter_arns) > 0 ? [1] : []

    content {
      sid       = "Config"
      actions   = ["ssm:GetParameters"]
      resources = local.parameter_arns
    }
  }

  dynamic "statement" {
    for_each = length(local.secret_arns) > 0 ? [1] : []

    content {
      sid       = "Secrets"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = local.secret_arns
    }
  }
}

resource "aws_iam_role_policy" "execution" {
  name   = "rlp-execution"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution.json
}

# The task role is the app's own identity. It has no permissions yet.
resource "aws_iam_role" "task" {
  name               = "${local.short}-task"
  path               = "/rlp/"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

# --- network and load balancing ---

resource "aws_security_group" "service" {
  name        = "${local.name}-service"
  description = "rlp ${local.name}: traffic from the shared load balancer only"
  vpc_id      = local.shared["vpc_id"]
}

resource "aws_vpc_security_group_ingress_rule" "from_load_balancer" {
  security_group_id            = aws_security_group.service.id
  description                  = "The shared load balancer"
  referenced_security_group_id = local.shared["alb_security_group_id"]
  ip_protocol                  = "tcp"
  from_port                    = var.port
  to_port                      = var.port
}

# Tasks pull their image from a public registry and call AWS APIs.
resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.service.id
  description       = "Image pulls and AWS APIs"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_lb_target_group" "this" {
  name                 = local.tg_name
  port                 = var.port
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = local.shared["vpc_id"]
  deregistration_delay = 30

  health_check {
    path                = var.health_path
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener_rule" "this" {
  listener_arn = local.shared["listener_arn"]
  priority     = local.priority

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this.arn
  }

  condition {
    host_header {
      values = [local.host]
    }
  }
}

# --- task and service ---

resource "aws_ecs_task_definition" "this" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.cpu)
  memory                   = tostring(var.memory)
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name         = var.component
      image        = var.image
      essential    = true
      portMappings = [{ containerPort = var.port, protocol = "tcp" }]
      environment  = [for k in sort(keys(local.environment)) : { name = k, value = local.environment[k] }]
      secrets = concat(
        [for k, p in aws_ssm_parameter.config : { name = k, valueFrom = p.arn }],
        [for k, p in aws_ssm_parameter.unset : { name = k, valueFrom = p.arn }],
        [for k, s in aws_secretsmanager_secret.this : { name = k, valueFrom = s.arn }],
      )
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.this.name
          awslogs-region        = data.aws_region.current.region
          awslogs-stream-prefix = var.component
        }
      }
    }
  ])
}

resource "aws_ecs_service" "this" {
  name                              = local.name
  cluster                           = local.shared["cluster_arn"]
  task_definition                   = aws_ecs_task_definition.this.arn
  desired_count                     = var.desired_count
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = 60
  enable_ecs_managed_tags           = true
  propagate_tags                    = "SERVICE"
  # Apply fails, and the circuit breaker rolls back, if the new tasks never get healthy.
  wait_for_steady_state = true

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # Default-VPC public subnets with public IPs: no NAT gateway to pay for in S0.
  network_configuration {
    subnets          = split(",", local.shared["subnet_ids"])
    security_groups  = [aws_security_group.service.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.this.arn
    container_name   = var.component
    container_port   = var.port
  }

  depends_on = [aws_lb_listener_rule.this]
}
