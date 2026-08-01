# The WebSocket API, and the role EventBridge Scheduler assumes to fire the
# one-shot grace check.
#
# Routes and integrations live in the ROOT module, not here. They are the join
# between this module and ./compute, and putting them in either one creates a
# module cycle: routes need Lambda invoke ARNs from compute, while compute needs
# this API's endpoint for its Management API calls. Root depends on both, so it
# is the only place the two can meet.

resource "aws_apigatewayv2_api" "ws" {
  name          = var.api_name
  protocol_type = "WEBSOCKET"
  # $request.body.type, not the conventional $request.body.action: the client
  # sends {"type": "subscribe", ...} and local/local_ws_server.py reads the same
  # field. Matching here keeps one message shape working against both
  # transports — with "action" every subscribe fell through to a $default route
  # that does not exist and was silently dropped, so panning did nothing once
  # deployed while working perfectly locally.
  route_selection_expression = "$request.body.type"
  tags                       = var.tags
}

resource "aws_apigatewayv2_stage" "prod" {
  api_id      = aws_apigatewayv2_api.ws.id
  name        = var.stage_name
  auto_deploy = true

  default_route_settings {
    # Metrics feed the dashboard's connection and message widgets.
    detailed_metrics_enabled = true
    throttling_burst_limit   = var.throttling_burst_limit
    throttling_rate_limit    = var.throttling_rate_limit
  }

  tags = var.tags
}

# --- Scheduler execution role ------------------------------------------------

data "aws_iam_policy_document" "scheduler_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    # Without this, any account able to create a schedule could assume this
    # role. Confused-deputy protection, per the Scheduler docs.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = var.scheduler_role_name
  assume_role_policy = data.aws_iam_policy_document.scheduler_trust.json
  tags               = var.tags
}

data "aws_iam_policy_document" "scheduler_invoke" {
  statement {
    sid     = "InvokeGraceCheckOnly"
    actions = ["lambda:InvokeFunction"]
    # Exactly one function. The schedule exists to call grace-check and nothing
    # else, so the role can call grace-check and nothing else.
    resources = [var.grace_check_function_arn]
  }
}

resource "aws_iam_role_policy" "scheduler_invoke" {
  name   = "invoke-grace-check"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler_invoke.json
}
