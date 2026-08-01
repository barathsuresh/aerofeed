terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

locals {
  # A job that declares an `environment:` gets an environment subject instead of
  # a ref subject, so both forms have to be trusted. GitHub can also append
  # immutable numeric ids to the owner and repo names; those variants are added
  # when the ids are supplied.
  repo_prefixes = compact([
    "repo:${var.github_owner}/${var.github_repo}",
    var.github_owner_id != "" && var.github_repo_id != "" ? "repo:${var.github_owner}@${var.github_owner_id}/${var.github_repo}@${var.github_repo_id}" : "",
  ])

  repo_subjects = flatten([
    for prefix in local.repo_prefixes : [
      "${prefix}:ref:refs/heads/${var.github_branch}",
      "${prefix}:environment:${var.github_environment}",
    ]
  ])

  tags = {
    project   = "aerofeed"
    managedby = "terraform"
    scope     = "github-oidc-bootstrap"
  }
}

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [var.github_oidc_thumbprint]

  tags = local.tags
}

resource "aws_s3_bucket" "state" {
  bucket        = var.state_bucket_name
  force_destroy = true
  tags          = local.tags
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket = aws_s3_bucket.state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_dynamodb_table" "locks" {
  name         = var.lock_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  tags = local.tags
}

data "aws_iam_policy_document" "github_actions_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = local.repo_subjects
    }
  }
}

resource "aws_iam_role" "github_actions_deploy" {
  name               = var.role_name
  assume_role_policy = data.aws_iam_policy_document.github_actions_trust.json
  tags               = local.tags
}

data "aws_iam_policy_document" "deploy" {
  statement {
    sid = "TerraformStateAndCallerIdentity"
    actions = [
      "sts:GetCallerIdentity",
      "iam:GetOpenIDConnectProvider",
      "iam:ListOpenIDConnectProviders",
    ]
    resources = ["*"]
  }

  statement {
    sid = "TerraformRemoteStateBucket"
    actions = [
      "s3:DeleteObject",
      "s3:GetObject",
      "s3:ListBucket",
      "s3:PutObject",
    ]
    resources = [
      aws_s3_bucket.state.arn,
      "${aws_s3_bucket.state.arn}/*",
    ]
  }

  statement {
    sid = "TerraformRemoteStateLock"
    actions = [
      "dynamodb:DeleteItem",
      "dynamodb:DescribeTable",
      "dynamodb:GetItem",
      "dynamodb:PutItem",
    ]
    resources = [aws_dynamodb_table.locks.arn]
  }

  statement {
    sid = "ReadAwsManagedPolicies"
    actions = [
      "iam:GetPolicy",
      "iam:GetPolicyVersion",
      "iam:ListPolicyVersions",
    ]
    resources = ["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"]
  }

  statement {
    sid = "ManageAerofeedIam"
    actions = [
      "iam:AttachRolePolicy",
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:DeleteRolePolicy",
      "iam:DetachRolePolicy",
      "iam:GetPolicy",
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:ListAttachedRolePolicies",
      "iam:ListInstanceProfilesForRole",
      "iam:ListRolePolicies",
      "iam:PassRole",
      "iam:PutRolePolicy",
      "iam:TagRole",
      "iam:UntagRole",
      "iam:UpdateAssumeRolePolicy",
    ]
    resources = [
      "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/aerofeed-*",
    ]
  }

  statement {
    sid = "ManageAerofeedDataPlane"
    actions = [
      "apigateway:*",
      "cloudfront:*",
      "cloudwatch:*",
      "dynamodb:*",
      "events:*",
      "kinesis:*",
      "lambda:*",
      "logs:*",
      "scheduler:*",
      "sns:*",
      "sqs:*",
    ]
    resources = ["*"]
  }

  # Budgets is account-scoped and its ARNs are not regional, so it does not
  # belong in the data-plane statement above.
  statement {
    sid = "ManageAerofeedBudget"
    actions = [
      "budgets:CreateBudget",
      "budgets:DeleteBudget",
      "budgets:DescribeBudget",
      "budgets:ModifyBudget",
      "budgets:ViewBudget",
    ]
    resources = ["arn:aws:budgets::${data.aws_caller_identity.current.account_id}:budget/*"]
  }

  statement {
    sid     = "ManageAerofeedFrontendBucket"
    actions = ["s3:*"]
    resources = [
      "arn:aws:s3:::aerofeed-frontend-*",
      "arn:aws:s3:::aerofeed-frontend-*/*",
    ]
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "aerofeed-deploy"
  role   = aws_iam_role.github_actions_deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}

output "github_actions_role_arn" {
  description = "Set this as the GitHub repository variable AWS_DEPLOY_ROLE_ARN."
  value       = aws_iam_role.github_actions_deploy.arn
}

output "state_bucket_name" {
  description = "Set this as the GitHub repository variable TF_STATE_BUCKET."
  value       = aws_s3_bucket.state.id
}

output "lock_table_name" {
  description = "Set this as the GitHub repository variable TF_LOCK_TABLE."
  value       = aws_dynamodb_table.locks.name
}
