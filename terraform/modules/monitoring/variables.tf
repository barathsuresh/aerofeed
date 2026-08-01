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
