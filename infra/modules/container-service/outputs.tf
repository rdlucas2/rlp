output "url" {
  description = "Base URL of the shared load balancer. Send the host output as the Host header."
  value       = "http://${data.aws_ssm_parameter.platform["alb_dns_name"].value}"
}

output "host" {
  description = "Host header the listener routes to this service."
  value       = local.host
}

output "health_path" {
  description = "Path the load balancer and rlp's smoke check request."
  value       = var.health_path
}

output "service_name" {
  description = "ECS service name."
  value       = aws_ecs_service.this.name
}

output "log_group" {
  description = "CloudWatch Logs group for the container's output."
  value       = aws_cloudwatch_log_group.this.name
}

output "secrets" {
  description = "Secrets Manager secret names an operator must put values in, by env var."
  value       = { for k, s in aws_secretsmanager_secret.this : k => s.name }
}

output "unset_parameters" {
  description = "SSM parameters created with a placeholder, by env var."
  value       = { for k, p in aws_ssm_parameter.unset : k => p.name }
}
