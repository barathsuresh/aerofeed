# Kinesis: the buffer between the poller and the processor.
# SQS: where records go once the event source mapping gives up on them.

resource "aws_kinesis_stream" "aircraft_states" {
  name = var.stream_name

  # On-demand: shard count follows traffic, which swings between zero (nobody
  # connected) and a few hundred records a second (a busy cell).
  #
  # Cost note: on-demand bills ~$0.04 per stream-hour whether or not anything
  # is written — roughly $29/month for an idle stream. That is the single
  # largest standing cost in this architecture. A provisioned single shard is
  # ~$11/month and handles 1MB/s and 1000 rec/s, comfortably above the observed
  # peak of ~150KB per poll. Switch if the idle cost matters more than the
  # elasticity.
  stream_mode_details {
    stream_mode = "ON_DEMAND"
  }

  # 24h is the default and is plenty: a record older than one poll interval is
  # already stale, and the DLQ holds failures separately for 14 days.
  retention_period = var.retention_hours

  tags = var.tags
}

resource "aws_sqs_queue" "processor_dlq" {
  name = var.dlq_name

  # 14 days, the maximum. A DLQ message is a bug report — it needs to survive a
  # weekend and a holiday, not just an on-call shift.
  message_retention_seconds = 1209600

  # Explicit, matching what the queue already has. Terraform's default is
  # 262144, so omitting this silently downgrades an existing 1MB queue on the
  # first apply. Kinesis failure records are ~1KB of metadata, so the size is
  # not load-bearing — but a silent shrink is still a change nobody asked for.
  max_message_size = 1048576

  tags = var.tags
}
