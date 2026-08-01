# The five handlers, each with its own role.
#
# One role per function, not one shared role: the poller must never be able to
# write DynamoDB, and grace-check must never be able to enable the rule it
# exists to disable. A shared role makes every function as privileged as the
# most privileged one, which here would mean the poller could push to every
# WebSocket client.
#
# No Secrets Manager anywhere. airplanes.live requires no credentials, so there
# is nothing to store and nothing to grant.

locals {
  functions = {
    poller      = { handler = "lambdas.poller_handler.handler", timeout = 60 }
    processor   = { handler = "lambdas.processor_handler.handler", timeout = 60 }
    connect     = { handler = "lambdas.connect_handler.handler", timeout = 10 }
    subscribe   = { handler = "lambdas.subscribe_handler.handler", timeout = 10 }
    default     = { handler = "lambdas.default_handler.handler", timeout = 10 }
    disconnect  = { handler = "lambdas.disconnect_handler.handler", timeout = 10 }
    grace-check = { handler = "lambdas.grace_check_handler.handler", timeout = 10 }
  }
}

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "this" {
  for_each           = local.functions
  name               = "aerofeed-${each.key}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
  tags               = var.tags
}

# CloudWatch Logs only. Everything else is granted per function below.
resource "aws_iam_role_policy_attachment" "basic" {
  for_each   = local.functions
  role       = aws_iam_role.this[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# --- per-function permissions ------------------------------------------------

# poller: writes to Kinesis, reads the cell list to know what to poll.
data "aws_iam_policy_document" "poller" {
  statement {
    sid       = "PutRecords"
    actions   = ["kinesis:PutRecord", "kinesis:PutRecords"]
    resources = [var.stream_arn]
  }
  statement {
    sid       = "ReadActiveCells"
    actions   = ["dynamodb:Scan", "dynamodb:Query"]
    resources = [var.connections_table_arn, var.connections_index_arn]
  }
}

# processor: the only function that pushes to clients.
data "aws_iam_policy_document" "processor" {
  statement {
    sid = "ConsumeStream"
    actions = ["kinesis:GetRecords", "kinesis:GetShardIterator", "kinesis:DescribeStream",
    "kinesis:DescribeStreamSummary", "kinesis:ListShards", "kinesis:ListStreams"]
    resources = [var.stream_arn]
  }
  statement {
    sid       = "Positions"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem"]
    resources = [var.positions_table_arn]
  }
  # Delete as well as read: a 410 from postToConnection reaps the dead
  # connection's rows on the spot.
  statement {
    sid       = "Subscribers"
    actions   = ["dynamodb:Query", "dynamodb:Scan", "dynamodb:DeleteItem"]
    resources = [var.connections_table_arn, var.connections_index_arn]
  }
  statement {
    sid       = "PushToClients"
    actions   = ["execute-api:ManageConnections"]
    resources = ["arn:aws:execute-api:${var.region}:${var.account_id}:*/*/POST/@connections/*"]
  }
  statement {
    sid       = "DeadLetter"
    actions   = ["sqs:SendMessage"]
    resources = [var.dlq_arn]
  }
}

# connect: registers a subscription and starts the clock.
data "aws_iam_policy_document" "connect" {
  statement {
    sid       = "RegisterSubscription"
    actions   = ["dynamodb:PutItem", "dynamodb:Query", "dynamodb:Scan"]
    resources = [var.connections_table_arn, var.connections_index_arn]
  }
  statement {
    sid       = "StartPolling"
    actions   = ["events:EnableRule", "events:DescribeRule"]
    resources = [var.poll_rule_arn]
  }
}

# subscribe: moves a client's coverage as it pans and zooms. No rule toggling —
# the client is already connected, so polling is already on.
data "aws_iam_policy_document" "subscribe" {
  statement {
    sid       = "MoveSubscription"
    actions   = ["dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan"]
    resources = [var.connections_table_arn, var.connections_index_arn]
  }
  # Read-only. The snapshot a joining client receives is a filtered Scan of the
  # positions table — see list_positions_in_cell for why a Scan beats a GSI here.
  statement {
    sid       = "SnapshotPositions"
    actions   = ["dynamodb:Scan"]
    resources = [var.positions_table_arn]
  }
  statement {
    sid       = "ConfirmToClient"
    actions   = ["execute-api:ManageConnections"]
    resources = ["arn:aws:execute-api:${var.region}:${var.account_id}:*/*/POST/@connections/*"]
  }
}

# default: sends a useful error frame for unmatched client messages.
data "aws_iam_policy_document" "default" {
  statement {
    sid       = "ExplainBadMessage"
    actions   = ["execute-api:ManageConnections"]
    resources = ["arn:aws:execute-api:${var.region}:${var.account_id}:*/*/POST/@connections/*"]
  }
}

# disconnect: removes a subscription and arms the grace check.
data "aws_iam_policy_document" "disconnect" {
  statement {
    sid       = "RemoveSubscription"
    actions   = ["dynamodb:Query", "dynamodb:Scan", "dynamodb:DeleteItem"]
    resources = [var.connections_table_arn, var.connections_index_arn]
  }
  statement {
    sid       = "ArmGraceCheck"
    actions   = ["scheduler:CreateSchedule", "scheduler:GetSchedule", "scheduler:DeleteSchedule"]
    resources = [var.grace_schedule_arn]
  }
  # Handing a role to Scheduler is itself a privilege: without this scoping, a
  # compromised disconnect handler could pass any role in the account.
  statement {
    sid       = "PassSchedulerRole"
    actions   = ["iam:PassRole"]
    resources = [var.scheduler_role_arn]
  }
}

# grace-check: read-only, and can only ever turn polling OFF.
data "aws_iam_policy_document" "grace_check" {
  statement {
    sid       = "CountSubscribers"
    actions   = ["dynamodb:Scan", "dynamodb:Query"]
    resources = [var.connections_table_arn, var.connections_index_arn]
  }
  statement {
    sid       = "StopPolling"
    actions   = ["events:DisableRule", "events:DescribeRule"]
    resources = [var.poll_rule_arn]
  }
}

locals {
  policies = {
    poller      = data.aws_iam_policy_document.poller.json
    processor   = data.aws_iam_policy_document.processor.json
    connect     = data.aws_iam_policy_document.connect.json
    subscribe   = data.aws_iam_policy_document.subscribe.json
    default     = data.aws_iam_policy_document.default.json
    disconnect  = data.aws_iam_policy_document.disconnect.json
    grace-check = data.aws_iam_policy_document.grace_check.json
  }
}

resource "aws_iam_role_policy" "this" {
  for_each = local.policies
  name     = "aerofeed-${each.key}"
  role     = aws_iam_role.this[each.key].id
  policy   = each.value
}

# --- functions ---------------------------------------------------------------

resource "aws_lambda_function" "this" {
  for_each = local.functions

  function_name = "aerofeed-${each.key}"
  role          = aws_iam_role.this[each.key].arn
  runtime       = "python3.13"
  handler       = each.value.handler
  timeout       = each.value.timeout
  memory_size   = var.memory_size
  filename      = var.package_paths[each.key]

  # Drives redeployment: the hash changes whenever the built zip changes, so
  # `terraform apply` after a code edit updates the function instead of
  # reporting no changes.
  source_code_hash = var.package_hashes[each.key]

  # Each apply with new code mints an immutable numbered version, so a bad
  # deploy can be rolled back by pointing an alias at the previous number
  # rather than by rebuilding and re-uploading the old zip.
  publish = true

  environment {
    variables = {
      AEROFEED_GRID_SIZE        = tostring(var.grid_size_degrees)
      AEROFEED_MAX_CELLS        = tostring(var.max_cells_per_client)
      AEROFEED_POLL_RULE        = var.poll_rule_name
      AEROFEED_WS_ENDPOINT      = var.ws_endpoint
      AEROFEED_GRACE_TARGET_ARN = "arn:aws:lambda:${var.region}:${var.account_id}:function:aerofeed-grace-check"
      AEROFEED_GRACE_ROLE_ARN   = var.scheduler_role_arn
    }
  }

  tags = var.tags
}

# --- Kinesis -> processor ----------------------------------------------------

resource "aws_lambda_event_source_mapping" "processor" {
  event_source_arn  = var.stream_arn
  function_name     = aws_lambda_function.this["processor"].arn
  starting_position = "LATEST"

  batch_size                         = var.batch_size
  maximum_batching_window_in_seconds = 5

  # Bounded, not the -1 default. Unlimited retries on a genuinely poisoned
  # record stall the shard until the record ages out of the stream.
  maximum_retry_attempts = var.max_retry_attempts

  # A record older than this is not worth retrying — the aircraft has moved on.
  maximum_record_age_in_seconds = 3600

  # Halve the batch on error so a single bad record is isolated rather than
  # condemning the good records sharing its batch.
  bisect_batch_on_function_error = true

  # The handler returns {"batchItemFailures": [...]}. Without this the mapping
  # ignores that response and retries the whole batch.
  function_response_types = ["ReportBatchItemFailures"]

  destination_config {
    on_failure {
      destination_arn = var.dlq_arn
    }
  }
}
