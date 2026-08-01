variable "region" { type = string }
variable "alarm_email" { type = string }

variable "function_names" {
  description = "Short names (poller, processor, ...). Prefixed with aerofeed- internally."
  type        = list(string)
}

variable "table_names" { type = list(string) }
variable "stream_name" { type = string }
variable "dlq_name" { type = string }
variable "api_id" { type = string }
variable "stage_name" { type = string }

variable "iterator_age_threshold_ms" {
  description = "How far behind the processor may fall before alarming. 60s: a poll is 60s, so one missed cycle is the natural unit."
  type        = number
  default     = 60000
}

variable "budget_limit_usd" {
  description = "Monthly account spend that trips the budget alert. A fully idle deployment is a few dollars, so 15 leaves room for a busy month without hiding a runaway resource."
  type        = string
  default     = "15"
}

variable "budget_name" {
  type    = string
  default = "aerofeed-monthly"
}

variable "topic_name" {
  type    = string
  default = "aerofeed-alarms"
}

variable "dashboard_name" {
  type    = string
  default = "aerofeed"
}

variable "tags" {
  type    = map(string)
  default = {}
}
