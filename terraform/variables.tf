variable "region" {
  description = "All resources live in one region; CloudFront is global but its cert would need us-east-1."
  type        = string
  default     = "us-east-1"
}

variable "alarm_email" {
  description = "Where alarm notifications go. Must be confirmed by clicking the link AWS emails after the first apply."
  type        = string
}

variable "package_source_dir" {
  description = "Legacy staged Lambda package contents. Per-function packages are read from .build/lambdas/<function>."
  type        = string
  default     = "../.build/pkg"
}

variable "lambda_package_root" {
  description = "Root containing one staged Lambda package directory per function. Built by scripts/build_package.sh."
  type        = string
  default     = "../.build/lambdas"
}

variable "frontend_source_dir" {
  description = "Directory holding index.html and app.js."
  type        = string
  default     = "../frontend"
}

variable "frontend_bucket_prefix" {
  description = "Account id is appended, since bucket names are globally unique."
  type        = string
  default     = "aerofeed-frontend"
}

variable "positions_table_name" {
  description = "Also used as a for_each key for alarms, so it must be a plain value, not a module output."
  type        = string
  default     = "aircraft-positions"
}

variable "connections_table_name" {
  type    = string
  default = "ws-connections"
}

variable "poll_rule_name" {
  type    = string
  default = "aerofeed-poll-schedule"
}

variable "grid_size_degrees" {
  description = "Must match what the frontend and stored cell keys assume. Changing it orphans every live subscription."
  type        = number
  default     = 5
}

variable "max_cells_per_client" {
  description = "Each cell is one upstream request. The real constraint is upstream: 1 req/s means ~60 distinct cells per 60s cycle for the WHOLE service, so this bounds one client's share of global capacity. See docs/poll-scheduling.md. Must match core.geo.DEFAULT_MAX_CELLS."
  type        = number
  default     = 4
}

variable "budget_limit_usd" {
  description = "Monthly account spend that trips the budget alert. See the cost section in README.md: an idle on-demand Kinesis stream alone is ~$29/month, so this is sized for deploy-demo-destroy, not for a long-running deployment."
  type        = string
  default     = "15"
}
