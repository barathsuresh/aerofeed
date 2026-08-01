variable "bucket_name" {
  description = "Globally unique. Suffix with the account id to avoid collisions."
  type        = string
}

variable "source_dir" {
  description = "Directory holding index.html and app.js."
  type        = string
}

variable "websocket_url" {
  description = "wss:// URL written into config.json for the browser to read at runtime."
  type        = string
}

variable "deployment_version" {
  description = "Recorded in config.json so a loaded page can be traced to a deploy."
  type        = string
  default     = "dev"
}

variable "price_class" {
  description = "PriceClass_100 is North America and Europe — cheapest, and enough for a demo."
  type        = string
  default     = "PriceClass_100"
}

variable "realtime_metrics" {
  description = "CloudFront additional metrics. Costs extra per distribution; off by default."
  type        = bool
  default     = false
}

variable "tags" {
  type    = map(string)
  default = {}
}
