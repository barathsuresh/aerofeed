output "positions_table_name" { value = aws_dynamodb_table.positions.name }
output "positions_table_arn" { value = aws_dynamodb_table.positions.arn }
output "connections_table_name" { value = aws_dynamodb_table.connections.name }
output "connections_table_arn" { value = aws_dynamodb_table.connections.arn }

output "connections_index_arn" {
  description = "GSI ARN. Query and Scan permissions must name the index explicitly."
  value       = "${aws_dynamodb_table.connections.arn}/index/region_cell-index"
}
