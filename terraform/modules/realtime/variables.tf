variable "account_id" { type = string }

variable "api_name" {
  type    = string
  default = "aerofeed-ws"
}

variable "stage_name" {
  type    = string
  default = "prod"
}

variable "scheduler_role_name" {
  type    = string
  default = "aerofeed-scheduler-role"
}

variable "grace_check_function_arn" {
  description = "The only function the Scheduler role may invoke."
  type        = string
}

variable "throttling_burst_limit" {
  type    = number
  default = 100
}

variable "throttling_rate_limit" {
  type    = number
  default = 50
}

variable "tags" {
  type    = map(string)
  default = {}
}
