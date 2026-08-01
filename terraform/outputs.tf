output "websocket_url" {
  description = "Plug into the frontend, or connect with websocat. Also written to config.json automatically."
  value       = module.realtime.websocket_url
}

output "cloudfront_domain" {
  description = "The frontend's domain name."
  value       = module.frontend.cloudfront_domain
}

output "cloudfront_distribution_id" {
  description = "CloudFront distribution id, used by deploy workflow invalidations."
  value       = module.frontend.distribution_id
}

output "site_url" {
  description = "Open this in a browser."
  value       = module.frontend.site_url
}

output "frontend_bucket" { value = module.frontend.bucket_name }
output "dashboard_url" { value = module.monitoring.dashboard_url }
output "alarm_topic_arn" { value = module.monitoring.topic_arn }
output "dlq_url" { value = module.streaming.dlq_url }

output "lambda_versions" {
  description = "Published version per function — the deployment identity."
  value       = module.compute.versions
}

output "poll_rule_name" {
  description = "Toggled at runtime by connect/grace-check; Terraform ignores its state."
  value       = module.scheduling.rule_name
}
