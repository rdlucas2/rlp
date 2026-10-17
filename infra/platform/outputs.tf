output "cluster_arn" {
  description = "Shared ECS cluster."
  value       = aws_ecs_cluster.this.arn
}

output "alb_dns_name" {
  description = "DNS name of the shared load balancer."
  value       = aws_lb.this.dns_name
}

output "listener_arn" {
  description = "HTTP listener that services add host-header rules to."
  value       = aws_lb_listener.http.arn
}

output "parameter_path" {
  description = "SSM path the container-service module reads these values from."
  value       = "/rlp/${var.environment}/platform/"
}
