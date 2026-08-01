output "bucket_name" { value = aws_s3_bucket.site.id }
output "bucket_arn" { value = aws_s3_bucket.site.arn }

output "distribution_id" { value = aws_cloudfront_distribution.site.id }

output "cloudfront_domain" {
  description = "Where the site is served from."
  value       = aws_cloudfront_distribution.site.domain_name
}

output "site_url" {
  value = "https://${aws_cloudfront_distribution.site.domain_name}"
}
