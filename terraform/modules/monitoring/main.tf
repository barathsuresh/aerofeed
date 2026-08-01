# Alarms, their delivery channel, and the dashboard.
#
# Every alarm uses treat_missing_data = "notBreaching": this pipeline is idle by
# design whenever nobody is connected, and "no data" must not read as "broken".

resource "aws_sns_topic" "alarms" {
  name = var.topic_name
  tags = var.tags
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email

  # An email subscription is not active until the recipient clicks the
  # confirmation link. Terraform cannot do that, so this resource shows as
  # created while the subscription sits in PendingConfirmation and every alarm
  # notifies nobody. Check the inbox after the first apply.
  lifecycle {
    ignore_changes = [id]
  }
}

# --- Lambda errors, one alarm per function -----------------------------------

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each = toset(var.function_names)

  alarm_name        = "aerofeed-${each.key}-errors"
  alarm_description = "aerofeed-${each.key} returned errors in the last 5 minutes"

  namespace   = "AWS/Lambda"
  metric_name = "Errors"
  statistic   = "Sum"
  dimensions  = { FunctionName = "aerofeed-${each.key}" }

  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
  tags          = var.tags
}

# --- the processor falling behind --------------------------------------------

resource "aws_cloudwatch_metric_alarm" "iterator_age" {
  alarm_name        = "aerofeed-kinesis-iterator-age"
  alarm_description = "processor is more than ${var.iterator_age_threshold_ms / 1000}s behind the stream"

  namespace   = "AWS/Kinesis"
  metric_name = "GetRecords.IteratorAgeMilliseconds"
  statistic   = "Maximum"
  dimensions  = { StreamName = var.stream_name }

  period              = 300
  evaluation_periods  = 1
  threshold           = var.iterator_age_threshold_ms
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
  tags          = var.tags
}

# --- DynamoDB throttling ------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "dynamo_throttled" {
  for_each = toset(var.table_names)

  alarm_name        = "aerofeed-${each.key}-throttled"
  alarm_description = "${each.key} is throttling requests"

  namespace   = "AWS/DynamoDB"
  metric_name = "ThrottledRequests"
  statistic   = "Sum"
  dimensions  = { TableName = each.key }

  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
  tags          = var.tags
}

# --- dashboard ----------------------------------------------------------------

locals {
  lambda_metric = { for m in ["Invocations", "Errors"] : m =>
    [for f in var.function_names : ["AWS/Lambda", m, "FunctionName", "aerofeed-${f}"]]
  }

  dashboard = {
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title   = "Lambda invocations", view = "timeSeries", stacked = false
          region  = var.region, period = 300, stat = "Sum"
          metrics = local.lambda_metric["Invocations"]
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title   = "Lambda errors", view = "timeSeries", stacked = false
          region  = var.region, period = 300, stat = "Sum"
          metrics = local.lambda_metric["Errors"]
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6
        properties = {
          title  = "Kinesis iterator age", view = "timeSeries"
          region = var.region, period = 300, stat = "Maximum"
          metrics = [
            ["AWS/Kinesis", "GetRecords.IteratorAgeMilliseconds", "StreamName", var.stream_name],
            [".", "IncomingRecords", ".", ".", { stat = "Sum", yAxis = "right" }],
          ]
          annotations = { horizontal = [{ label = "alarm", value = var.iterator_age_threshold_ms }] }
        }
      },
      {
        type = "metric", x = 12, y = 6, width = 12, height = 6
        properties = {
          title  = "DynamoDB consumed capacity", view = "timeSeries"
          region = var.region, period = 300, stat = "Sum"
          # concat, not flatten: flatten() recurses and would collapse each
          # ["AWS/DynamoDB", "Consumed...", "TableName", t] into loose strings.
          # CloudWatch requires an array of arrays and rejects the whole
          # dashboard otherwise.
          metrics = concat(
            [for t in var.table_names : ["AWS/DynamoDB", "ConsumedReadCapacityUnits", "TableName", t]],
            [for t in var.table_names : ["AWS/DynamoDB", "ConsumedWriteCapacityUnits", "TableName", t]],
          )
        }
      },
      {
        type = "metric", x = 0, y = 12, width = 12, height = 6
        properties = {
          title  = "WebSocket connections", view = "timeSeries"
          region = var.region, period = 300, stat = "Sum"
          metrics = [
            ["AWS/ApiGateway", "ConnectCount", "ApiId", var.api_id, "Stage", var.stage_name],
            [".", "DisconnectCount", ".", ".", ".", "."],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 12, width = 12, height = 6
        properties = {
          title  = "WebSocket messages", view = "timeSeries"
          region = var.region, period = 300, stat = "Sum"
          metrics = [
            ["AWS/ApiGateway", "MessageCount", "ApiId", var.api_id, "Stage", var.stage_name],
            [".", "IntegrationError", ".", ".", ".", "."],
            [".", "ClientError", ".", ".", ".", "."],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 18, width = 24, height = 6
        properties = {
          title   = "DLQ depth (records that exhausted retries)", view = "timeSeries"
          region  = var.region, period = 300, stat = "Maximum"
          metrics = [["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", var.dlq_name]]
        }
      },
    ]
  }
}

resource "aws_cloudwatch_dashboard" "aerofeed" {
  dashboard_name = var.dashboard_name
  dashboard_body = jsonencode(local.dashboard)
}

# --- account budget -----------------------------------------------------------
#
# The alarms above watch the pipeline; this watches the bill. They fail
# differently: a stuck EventBridge Rule, a stream left provisioned after a
# partial destroy, or an idle Kinesis on-demand stream at ~$0.04/stream-hour
# (~$29/month, the largest standing cost here) all cost money while every
# CloudWatch alarm stays green. Nothing else in this architecture would notice.
#
# Scoped to the whole account rather than to the project tag: the failure being
# guarded against is an orphaned resource, and an orphan is exactly the thing
# likely to have lost its tags.
resource "aws_budgets_budget" "monthly" {
  name         = var.budget_name
  budget_type  = "COST"
  limit_amount = var.budget_limit_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Actual spend, on the way up. Catches something already running.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alarm_email]
  }

  # Forecast, so a stream switched on early in the month reports before the
  # month's spend has actually landed.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alarm_email]
  }
}
