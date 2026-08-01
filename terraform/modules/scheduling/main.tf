# The poller's clock. Disabled at rest — this is the entire cost argument:
# nobody connected means no invocations, no upstream calls, no Kinesis writes.

resource "aws_cloudwatch_event_rule" "poll" {
  name                = var.rule_name
  description         = "aerofeed: poll active cells; toggled by connect and grace-check"
  schedule_expression = var.schedule_expression

  # Created disabled and left that way by Terraform. connect_handler enables it
  # on the first connection; grace_check_handler disables it once the store has
  # been empty for the grace period.
  #
  # This is deliberately drift Terraform must not fight: the rule's state is
  # runtime state, not configuration. Without the lifecycle block below, every
  # `terraform apply` while someone is connected would disable polling and
  # black out live clients.
  state = "DISABLED"

  lifecycle {
    ignore_changes = [state]
  }

  tags = var.tags
}

resource "aws_cloudwatch_event_target" "poller" {
  rule      = aws_cloudwatch_event_rule.poll.name
  target_id = "poller"
  arn       = var.poller_function_arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "eventbridge-poll"
  action        = "lambda:InvokeFunction"
  function_name = var.poller_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.poll.arn
}
