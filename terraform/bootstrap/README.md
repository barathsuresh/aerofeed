# GitHub Actions bootstrap

This stack is created once with local/admin AWS credentials. It creates:

- GitHub Actions OIDC identity provider
- Branch-scoped deploy/destroy IAM role for `cloud/aws`
- S3 Terraform state bucket
- DynamoDB Terraform lock table

Example:

```bash
terraform -chdir=terraform/bootstrap init
terraform -chdir=terraform/bootstrap apply \
  -var='github_owner=YOUR_GITHUB_OWNER' \
  -var='github_repo=aerofeed' \
  -var='state_bucket_name=aerofeed-tfstate-YOUR_ACCOUNT_ID'
```

Set these GitHub repository variables from the outputs:

- `AWS_DEPLOY_ROLE_ARN` from `github_actions_role_arn`
- `TF_STATE_BUCKET` from `state_bucket_name`
- `TF_LOCK_TABLE` from `lock_table_name`

The deploy and destroy workflows intentionally leave this bootstrap stack in
place so manual deploys can be run again after the app stack is destroyed.
