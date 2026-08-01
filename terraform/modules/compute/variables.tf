variable "region" { type = string }
variable "account_id" { type = string }

variable "package_paths" {
  description = "Path to the built deployment zip per Lambda function."
  type        = map(string)
}

variable "package_hashes" {
  description = "base64sha256 of each zip. Changing it is what triggers a redeploy."
  type        = map(string)
}

variable "memory_size" {
  description = "512MB. The poller holds a few hundred parsed aircraft; below this it slows without saving money, since Lambda bills GB-seconds."
  type        = number
  default     = 512
}

variable "batch_size" {
  description = "Kinesis records per processor invocation."
  type        = number
  default     = 100
}

variable "max_retry_attempts" {
  description = "Bounded on purpose. -1 (unlimited) stalls a shard on a poisoned record."
  type        = number
  default     = 3
}

variable "grid_size_degrees" { type = number }
variable "max_cells_per_client" { type = number }

variable "positions_table_arn" { type = string }
variable "connections_table_arn" { type = string }
variable "connections_index_arn" { type = string }
variable "stream_arn" { type = string }
variable "dlq_arn" { type = string }
variable "poll_rule_arn" { type = string }
variable "poll_rule_name" { type = string }
variable "scheduler_role_arn" { type = string }
variable "grace_schedule_arn" { type = string }

variable "ws_endpoint" {
  description = "https:// form of the WebSocket stage, for the Management API. Empty on the first apply, before the API exists."
  type        = string
  default     = ""
}

variable "tags" {
  type    = map(string)
  default = {}
}
