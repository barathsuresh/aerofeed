# networking — deliberately empty

No VPC, no subnets, no security groups, no NAT.

Every component of this architecture is an AWS-managed service reached over a
public endpoint: Lambda, DynamoDB, Kinesis, SQS, SNS, API Gateway, CloudFront.
None of them requires network plumbing to talk to each other, and the only
outbound dependency — `api.airplanes.live` — is a public HTTPS endpoint.

Putting the Lambdas in a VPC would make things strictly worse:

* A Lambda in a VPC has no route to the internet without a **NAT Gateway**, at
  roughly **$32/month plus data processing** — for an architecture whose entire
  idle cost is otherwise near zero. That is more than every other resource here
  combined.
* Reaching DynamoDB, Kinesis, SQS and SNS privately would then need four
  **VPC endpoints**, about **$7/month each**.
* Cold starts grow while ENIs attach.

The reason to accept that cost is a private resource — an RDS instance, an
ElastiCache cluster, something in a data centre. There is none here, and the
cache decision was explicitly DynamoDB + TTL rather than ElastiCache precisely
to avoid needing a VPC.

Verified empirically in phase 4: a throwaway Lambda with no VPC configuration
reached `api.airplanes.live` in 0.63s, HTTP 200, 258 aircraft.

If a private dependency ever appears, this module gets a VPC with private
subnets, a NAT Gateway (or VPC endpoints where the service supports them), and
`vpc_config` blocks on the affected functions in `../compute`.
