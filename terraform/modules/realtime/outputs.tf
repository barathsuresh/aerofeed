output "api_id" { value = aws_apigatewayv2_api.ws.id }
output "api_arn" { value = aws_apigatewayv2_api.ws.arn }
output "execution_arn" { value = aws_apigatewayv2_api.ws.execution_arn }
output "stage_name" { value = aws_apigatewayv2_stage.prod.name }

output "websocket_url" {
  description = "What a browser connects to."
  value       = "${aws_apigatewayv2_api.ws.api_endpoint}/${aws_apigatewayv2_stage.prod.name}"
}

output "management_endpoint" {
  description = "https:// form, for postToConnection. Same host, different scheme."
  value       = replace("${aws_apigatewayv2_api.ws.api_endpoint}/${aws_apigatewayv2_stage.prod.name}", "wss://", "https://")
}

output "scheduler_role_arn" { value = aws_iam_role.scheduler.arn }
