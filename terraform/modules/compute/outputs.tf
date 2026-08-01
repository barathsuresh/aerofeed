output "function_names" {
  value = { for k, f in aws_lambda_function.this : k => f.function_name }
}

output "function_arns" {
  value = { for k, f in aws_lambda_function.this : k => f.arn }
}

output "invoke_arns" {
  description = "For API Gateway integrations, which need the invoke ARN, not the function ARN."
  value       = { for k, f in aws_lambda_function.this : k => f.invoke_arn }
}

output "versions" {
  description = "Published version number per function — the deployment identity."
  value       = { for k, f in aws_lambda_function.this : k => f.version }
}

output "role_arns" {
  value = { for k, r in aws_iam_role.this : k => r.arn }
}
