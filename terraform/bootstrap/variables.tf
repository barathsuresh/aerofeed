variable "region" {
  type    = string
  default = "us-east-1"
}

variable "github_owner" {
  description = "GitHub organization or user that owns the repository."
  type        = string
}

variable "github_repo" {
  description = "GitHub repository name."
  type        = string
  default     = "aerofeed"
}

variable "github_owner_id" {
  description = "Numeric GitHub owner id, used when GitHub issues ID-qualified OIDC subjects."
  type        = string
  default     = ""
}

variable "github_repo_id" {
  description = "Numeric GitHub repository id, used when GitHub issues ID-qualified OIDC subjects."
  type        = string
  default     = ""
}

variable "github_branch" {
  description = "Only this branch may assume the deploy role."
  type        = string
  default     = "cloud/aws"
}

variable "github_environment" {
  description = "GitHub Actions environment the deploy job runs in."
  type        = string
  default     = "production"
}

variable "role_name" {
  type    = string
  default = "aerofeed-github-actions-deploy"
}

variable "state_bucket_name" {
  description = "S3 bucket for the app stack Terraform state. Must be globally unique."
  type        = string
}

variable "lock_table_name" {
  description = "DynamoDB table used by the S3 backend for state locking."
  type        = string
  default     = "aerofeed-terraform-locks"
}

variable "github_oidc_thumbprint" {
  description = "Thumbprint for token.actions.githubusercontent.com."
  type        = string
  default     = "6938fd4d98bab03faadb97b34396831e3780aea1"
}
