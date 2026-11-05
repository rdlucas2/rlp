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

output "https_listener_arn" {
  description = "HTTPS listener for public_hosts, or null without any."
  value       = local.public ? aws_lb_listener.https[0].arn : null
}

output "certificate_validation_records" {
  description = "CNAME records that prove you own the public hosts. Create them where the domain's DNS lives; ACM issues the certificate once they resolve."
  value = local.public ? distinct([
    for o in aws_acm_certificate.this[0].domain_validation_options : {
      name  = o.resource_record_name
      type  = o.resource_record_type
      value = o.resource_record_value
    }
  ]) : []
}

output "dns_records" {
  description = "CNAME records that point each public host at the load balancer."
  value       = [for host in local.hostnames : { name = host, type = "CNAME", value = aws_lb.this.dns_name }]
}

output "auth_user_pool_id" {
  description = "Cognito user pool the sign-in uses, or null when no public host asks for sign-in. Add users with aws cognito-idp admin-create-user."
  value       = local.auth ? aws_cognito_user_pool.this[0].id : null
}

output "auth_sign_in_domain" {
  description = "Cognito hosted sign-in domain, or null."
  value       = local.auth ? "https://${aws_cognito_user_pool_domain.this[0].domain}.auth.${var.region}.amazoncognito.com" : null
}
