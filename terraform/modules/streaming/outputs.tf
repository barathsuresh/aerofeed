output "stream_name" { value = aws_kinesis_stream.aircraft_states.name }
output "stream_arn" { value = aws_kinesis_stream.aircraft_states.arn }
output "dlq_arn" { value = aws_sqs_queue.processor_dlq.arn }
output "dlq_name" { value = aws_sqs_queue.processor_dlq.name }
output "dlq_url" { value = aws_sqs_queue.processor_dlq.url }
