variable "rule_name" {
  type    = string
  default = "aerofeed-poll-schedule"
}

variable "schedule_expression" {
  description = "How often to poll while clients are connected."
  type        = string
  default     = "rate(1 minute)"
}

variable "poller_function_arn" { type = string }
variable "poller_function_name" { type = string }

variable "tags" {
  type    = map(string)
  default = {}
}
