# Static site on S3, served through CloudFront with Origin Access Control.
#
# The bucket is fully private: no public access, no website endpoint, no bucket
# policy granting anyone but this distribution. OAC (not the deprecated OAI)
# signs CloudFront's origin requests with SigV4, which is the only path in.

resource "aws_s3_bucket" "site" {
  bucket        = var.bucket_name
  force_destroy = true
  tags          = var.tags
}

resource "aws_s3_bucket_public_access_block" "site" {
  bucket = aws_s3_bucket.site.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "site" {
  bucket = aws_s3_bucket.site.id
  rule {
    # ACLs disabled entirely; ownership is unambiguous and nothing can grant
    # public read by accident.
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "site" {
  bucket = aws_s3_bucket.site.id
  versioning_configuration {
    # A bad frontend deploy is recoverable by restoring the previous object
    # version, without rebuilding anything.
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "site" {
  bucket = aws_s3_bucket.site.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# --- CloudFront ---------------------------------------------------------------

resource "aws_cloudfront_origin_access_control" "site" {
  name                              = "${var.bucket_name}-oac"
  description                       = "OAC for the aerofeed frontend bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "site" {
  enabled             = true
  default_root_object = "index.html"
  comment             = "aerofeed frontend"
  price_class         = var.price_class

  origin {
    domain_name              = aws_s3_bucket.site.bucket_regional_domain_name
    origin_id                = "s3-${aws_s3_bucket.site.id}"
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id
  }

  default_cache_behavior {
    target_origin_id       = "s3-${aws_s3_bucket.site.id}"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    # AWS managed "CachingOptimized".
    cache_policy_id = "658327ea-f89d-4fab-a63d-7e88639e58f6"
  }

  # config.json carries the WebSocket URL and must never be served stale after
  # a redeploy points the frontend at a new API. AWS managed "CachingDisabled".
  ordered_cache_behavior {
    path_pattern           = "/config.json"
    target_origin_id       = "s3-${aws_s3_bucket.site.id}"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    cache_policy_id        = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    # The default *.cloudfront.net certificate. A custom domain would need an
    # ACM certificate in us-east-1 and a Route 53 record.
    cloudfront_default_certificate = true
  }

  tags = var.tags
}

# Only this distribution may read the bucket.
data "aws_iam_policy_document" "bucket" {
  statement {
    sid       = "AllowCloudFrontServicePrincipalReadOnly"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.site.arn}/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.site.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "site" {
  bucket = aws_s3_bucket.site.id
  policy = data.aws_iam_policy_document.bucket.json

  depends_on = [aws_s3_bucket_public_access_block.site]
}

# --- content ------------------------------------------------------------------

locals {
  content_types = {
    ".html" = "text/html"
    ".js"   = "application/javascript"
    ".css"  = "text/css"
    ".json" = "application/json"
  }
  # Static assets, uploaded verbatim. config.json is generated below instead.
  assets = fileset(var.source_dir, "*.{html,js,css}")
}

resource "aws_s3_object" "asset" {
  for_each = local.assets

  bucket       = aws_s3_bucket.site.id
  key          = each.value
  source       = "${var.source_dir}/${each.value}"
  content_type = lookup(local.content_types, regex("\\.[^.]+$", each.value), "application/octet-stream")

  # Re-uploads whenever the file changes, which is what makes `terraform apply`
  # a real frontend deploy rather than a no-op.
  etag = filemd5("${var.source_dir}/${each.value}")
}

# The whole point of this file: the frontend reads its endpoint at runtime
# instead of having a URL baked into app.js. The same bundle works locally and
# deployed, and a rebuilt API changes one generated object rather than source.
resource "aws_s3_object" "config" {
  bucket       = aws_s3_bucket.site.id
  key          = "config.json"
  content_type = "application/json"

  content = jsonencode({
    wsUrl      = var.websocket_url
    generated  = timestamp()
    apiVersion = var.deployment_version
  })

  # timestamp() changes every plan; without this the object is perpetually
  # "changed" and every apply shows spurious drift.
  lifecycle {
    ignore_changes = [content]
  }
}

# Cached HTML and JS would survive a deploy and keep talking to the old API.
resource "aws_cloudfront_monitoring_subscription" "site" {
  count           = var.realtime_metrics ? 1 : 0
  distribution_id = aws_cloudfront_distribution.site.id

  monitoring_subscription {
    realtime_metrics_subscription_config {
      realtime_metrics_subscription_status = "Enabled"
    }
  }
}
