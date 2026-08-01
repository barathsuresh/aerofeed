variable "positions_table_name" {
  description = "DynamoDB table holding the last-known state per aircraft."
  type        = string
  default     = "aircraft-positions"
}

variable "connections_table_name" {
  description = "DynamoDB table holding WebSocket subscriptions, one row per (connection, cell)."
  type        = string
  default     = "ws-connections"
}

variable "tags" {
  description = "Tags applied to every resource in this module."
  type        = map(string)
  default     = {}
}
