output "topic_arn" { value = aws_sns_topic.alarms.arn }
output "dashboard_name" { value = aws_cloudwatch_dashboard.aerofeed.dashboard_name }

output "dashboard_url" {
  value = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards/dashboard/${aws_cloudwatch_dashboard.aerofeed.dashboard_name}"
}

output "alarm_names" {
  value = concat(
    [for a in aws_cloudwatch_metric_alarm.lambda_errors : a.alarm_name],
    [aws_cloudwatch_metric_alarm.iterator_age.alarm_name],
    [for a in aws_cloudwatch_metric_alarm.dynamo_throttled : a.alarm_name],
  )
}

output "budget_name" { value = aws_budgets_budget.monthly.name }
