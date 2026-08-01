variable "stream_name" {
  description = "Kinesis stream carrying parsed aircraft states."
  type        = string
  default     = "aerofeed-aircraft-states"
}

variable "dlq_name" {
  description = "SQS queue receiving Kinesis batches that exhausted their retries."
  type        = string
  default     = "kinesis-processor-dlq"
}

variable "retention_hours" {
  description = "Kinesis retention. 24h default; longer costs more and buys nothing here."
  type        = number
  default     = 24
}

variable "tags" {
  type    = map(string)
  default = {}
}
