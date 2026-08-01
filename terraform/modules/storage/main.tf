# DynamoDB: last-known aircraft state, and the WebSocket subscriber registry.
#
# Both on-demand: traffic idles at zero when nobody is connected, and provisioned
# capacity would bill continuously for that idleness.

resource "aws_dynamodb_table" "positions" {
  name         = var.positions_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "icao24"

  attribute {
    name = "icao24"
    type = "S"
  }

  # TTL is a garbage collector, never a correctness mechanism: AWS only commits
  # to deleting expired items "within a few days". Every read path is already
  # correct with expired items present — a stale position reads as "changed" to
  # the delta filter, which is the desired outcome anyway.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    # Off: every item is reconstructable from the next poll, and the data is
    # worthless the moment it is stale. Backups would protect nothing.
    enabled = false
  }

  tags = var.tags
}

resource "aws_dynamodb_table" "connections" {
  name         = var.connections_table_name
  billing_mode = "PAY_PER_REQUEST"

  # Composite, not connection_id alone: one client covers up to
  # MAX_CELLS_PER_CLIENT region cells at a wide zoom, which is one row per cell.
  # A bare connection_id key would hold a single cell and silently collapse a
  # zoomed-out client's coverage.
  hash_key  = "connection_id"
  range_key = "region_cell"

  attribute {
    name = "connection_id"
    type = "S"
  }

  attribute {
    name = "region_cell"
    type = "S"
  }

  # The reverse lookup — "who is watching this cell" — runs on every poll and
  # must never be a table scan.
  global_secondary_index {
    name            = "region_cell-index"
    hash_key        = "region_cell"
    range_key       = "connection_id"
    projection_type = "INCLUDE"
    # connected_at is the only non-key attribute the ConnectionStore contract
    # returns, so projecting it keeps list_connections_by_cell a single read.
    non_key_attributes = ["connected_at"]
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  tags = var.tags
}
