terraform {
  required_version = ">= 1.10"

  backend "s3" {}

  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.0" }
  }

  # Backend settings come from -backend-config in the deploy/destroy workflows.
  # Locking is S3-native (use_lockfile), so no DynamoDB table is involved.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = local.tags
  }
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id

  tags = {
    project   = "aerofeed"
    managedby = "terraform"
  }

  # Everything Lambda runs. Rebuilt by Terraform so a code edit is picked up by
  # `apply` — no separate zip-and-upload step to forget.
  source_dirs = ["core", "local", "aws", "lambdas"]

  # Static, not derived from module outputs. for_each keys must be known at
  # plan time, and anything read back from a resource attribute is not — so
  # deriving these from module.compute would make `plan` fail before anything
  # exists. They are fixed names anyway.
  function_keys = ["poller", "processor", "connect", "subscribe", "default", "disconnect", "grace-check"]
  table_names   = [var.positions_table_name, var.connections_table_name]
}

# --- deployment package -------------------------------------------------------

data "archive_file" "lambda_package" {
  for_each = toset(local.function_keys)

  type        = "zip"
  source_dir  = "${var.lambda_package_root}/${each.key}"
  output_path = "${path.module}/.build/aerofeed-${each.key}.zip"
}

# --- data plane ---------------------------------------------------------------

module "storage" {
  source                 = "./modules/storage"
  positions_table_name   = var.positions_table_name
  connections_table_name = var.connections_table_name
  tags                   = local.tags
}

module "streaming" {
  source = "./modules/streaming"
  tags   = local.tags
}

# --- realtime transport -------------------------------------------------------

# Created before compute: compute needs this API's endpoint for postToConnection,
# and the Scheduler role ARN for iam:PassRole. The reverse dependency — routes
# needing Lambda ARNs — is resolved in this file rather than inside the module,
# which is what keeps the two from forming a cycle.
module "realtime" {
  source     = "./modules/realtime"
  account_id = local.account_id

  # Referenced by ARN string rather than module.compute output, deliberately:
  # taking the output here would make realtime depend on compute, and compute
  # already depends on realtime. The name is deterministic, so the ARN is too.
  grace_check_function_arn = "arn:aws:lambda:${var.region}:${local.account_id}:function:aerofeed-grace-check"

  tags = local.tags
}

# --- scheduling ---------------------------------------------------------------

# The rule ARN is needed by compute's IAM (EnableRule/DisableRule), and the rule
# needs the poller's ARN as its target. Same shape of cycle, same resolution:
# the ARN is deterministic from the name.
locals {
  poll_rule_arn      = "arn:aws:events:${var.region}:${local.account_id}:rule/${var.poll_rule_name}"
  grace_schedule_arn = "arn:aws:scheduler:${var.region}:${local.account_id}:schedule/default/aerofeed-grace-check"
}

module "scheduling" {
  source    = "./modules/scheduling"
  rule_name = var.poll_rule_name

  poller_function_arn  = module.compute.function_arns["poller"]
  poller_function_name = module.compute.function_names["poller"]

  tags = local.tags
}

# --- compute ------------------------------------------------------------------

module "compute" {
  source     = "./modules/compute"
  region     = var.region
  account_id = local.account_id

  package_paths = { for k, pkg in data.archive_file.lambda_package : k => pkg.output_path }
  package_hashes = {
    for k, pkg in data.archive_file.lambda_package : k => pkg.output_base64sha256
  }

  grid_size_degrees    = var.grid_size_degrees
  max_cells_per_client = var.max_cells_per_client

  positions_table_arn   = module.storage.positions_table_arn
  connections_table_arn = module.storage.connections_table_arn
  connections_index_arn = module.storage.connections_index_arn
  stream_arn            = module.streaming.stream_arn
  dlq_arn               = module.streaming.dlq_arn

  poll_rule_arn      = local.poll_rule_arn
  poll_rule_name     = var.poll_rule_name
  grace_schedule_arn = local.grace_schedule_arn
  scheduler_role_arn = module.realtime.scheduler_role_arn
  ws_endpoint        = module.realtime.management_endpoint

  tags = local.tags
}

# --- WebSocket routes ---------------------------------------------------------
#
# The join between realtime and compute. Here, not in either module, because
# each side needs something from the other and Terraform forbids module cycles.

locals {
  ws_routes = {
    connect    = "$connect"
    disconnect = "$disconnect"
    # Matched against $request.body.type. Without this route the message is
    # dropped and a deployed client can never leave its first cell.
    subscribe = "subscribe"
    default   = "$default"
  }
}

resource "aws_apigatewayv2_integration" "ws" {
  for_each = local.ws_routes

  api_id           = module.realtime.api_id
  integration_type = "AWS_PROXY"
  integration_uri  = module.compute.invoke_arns[each.key]
}

resource "aws_apigatewayv2_route" "ws" {
  for_each = local.ws_routes

  api_id    = module.realtime.api_id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.ws[each.key].id}"
}

resource "aws_lambda_permission" "apigw" {
  for_each = local.ws_routes

  statement_id  = "apigw-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = module.compute.function_names[each.key]
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${module.realtime.execution_arn}/*"
}

# --- observability ------------------------------------------------------------

module "monitoring" {
  source      = "./modules/monitoring"
  region      = var.region
  alarm_email = var.alarm_email

  function_names = local.function_keys
  table_names    = local.table_names
  stream_name    = module.streaming.stream_name
  dlq_name       = module.streaming.dlq_name
  api_id         = module.realtime.api_id
  stage_name     = module.realtime.stage_name

  tags = local.tags
}

# --- frontend -----------------------------------------------------------------

module "frontend" {
  source      = "./modules/frontend"
  bucket_name = "${var.frontend_bucket_prefix}-${local.account_id}"
  source_dir  = var.frontend_source_dir

  # The reason config.json exists: the browser reads this at runtime, so no
  # endpoint is ever hardcoded into app.js.
  websocket_url      = module.realtime.websocket_url
  deployment_version = module.compute.versions["processor"]

  tags = local.tags
}
