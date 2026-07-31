# AWS phase 4 — real services, still a plain process

Same pipeline as `run_local.py`, with three substitutions and nothing else:

| local | AWS |
|---|---|
| `SqlitePositionStore` | `DynamoPositionStore` → `aircraft-positions` |
| `SqliteConnectionStore` | `DynamoConnectionStore` → `ws-connections` |
| `LocalStream` (asyncio.Queue) | `KinesisStream` → `aerofeed-aircraft-states` |

Poller, processor, delta filter, scheduler and WebSocket server are untouched —
they only ever saw the protocols in `core/storage_interface.py`.

Not yet Lambda. This phase exists to prove the AWS-specific behaviour in
isolation: DynamoDB round-trips and TTL, GSI queries, Kinesis partitioning and
per-aircraft ordering. Phase 5 wraps it in handlers and API Gateway.

```
python run_aws.py          # checks the resources exist, then runs
```

## Creating the resources

Terraform comes later. For now:

```bash
aws dynamodb create-table --table-name aircraft-positions \
  --attribute-definitions AttributeName=icao24,AttributeType=S \
  --key-schema AttributeName=icao24,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST --tags Key=project,Value=aerofeed

aws dynamodb update-time-to-live --table-name aircraft-positions \
  --time-to-live-specification Enabled=true,AttributeName=expires_at

aws dynamodb create-table --table-name ws-connections \
  --attribute-definitions AttributeName=connection_id,AttributeType=S \
                          AttributeName=region_cell,AttributeType=S \
  --key-schema AttributeName=connection_id,KeyType=HASH \
                AttributeName=region_cell,KeyType=RANGE \
  --global-secondary-indexes '[{
      "IndexName":"region_cell-index",
      "KeySchema":[{"AttributeName":"region_cell","KeyType":"HASH"},
                   {"AttributeName":"connection_id","KeyType":"RANGE"}],
      "Projection":{"ProjectionType":"INCLUDE","NonKeyAttributes":["connected_at"]}}]' \
  --billing-mode PAY_PER_REQUEST --tags Key=project,Value=aerofeed

aws dynamodb update-time-to-live --table-name ws-connections \
  --time-to-live-specification Enabled=true,AttributeName=expires_at

aws kinesis create-stream --stream-name aerofeed-aircraft-states \
  --stream-mode-details StreamMode=ON_DEMAND
```

## Why `ws-connections` has a sort key

The spec said PK `connection_id`. That alone holds **one cell per connection**,
and since the multi-cell viewport work a single client covers up to
`MAX_CELLS_PER_CLIENT` (9) cells — one row each. A bare `connection_id` key
would silently collapse a zoomed-out client back to one cell.

So: PK `connection_id`, SK `region_cell`. The GSI on `region_cell` serves its
stated purpose unchanged — `list_connections_by_cell` runs on every poll and
must never be a table scan.

## TTL

`expires_at`, unix seconds. Positions expire after 1h, connections after 2h
(matching API Gateway's maximum WebSocket connection duration).

TTL is a garbage collector, never a correctness mechanism — AWS only commits to
deleting expired items "within a few days". Every read path is already correct
with expired items present: a stale position reads as "changed" to the delta
filter, which is the desired outcome anyway. Connections are reaped explicitly
on disconnect; TTL only catches ones that died without a clean close.

## No Secrets Manager

airplanes.live needs no credentials. Secrets Manager was an OpenSky-only
requirement and applies nowhere in this architecture.

## Known gaps, deliberate

- **`publish()` is one `PutRecord` per aircraft.** A 700-aircraft cell is 700
  API calls. `PutRecords` batches 500 and is the obvious next step; it needs
  per-record partial-failure handling, which is real code and not needed to
  validate the shape here.
- **`consume()` does not checkpoint.** Iterators live in memory, so a restart
  resumes at `LATEST` and drops what arrived while down. Phase 5's Lambda event
  source mapping provides checkpointing and resharding for free.
- **`list_active_cells()` scans the GSI** and dedupes client-side. DynamoDB has
  no `DISTINCT`; the alternative is an aggregate item updated on every
  connect/disconnect — a write amplifier to save a scan over a table sized by
  "currently connected clients".
- **`Processor._last_broadcast` is still an in-memory dict.** Fine in a
  long-lived process, breaks in Lambda where containers cycle. It moves into
  the position item as `last_broadcast_at` in phase 5.

## Verified against the live resources

- Connectivity: throwaway Lambda in `us-east-1` → `api.airplanes.live` — HTTP
  200, 258 aircraft, 143KB, 0.63s, no VPC. Function and role deleted after.
- One connection across 9 cells: `count_connections()` 10, per-cell 2,
  `list_active_cells()` 9 distinct, idempotent delete.
- Floats survive exactly (`10972.800000000001`), bools stay `BOOL` not 0/1,
  ints stay ints.
- Kinesis: 6 records for one `icao24` read back in order off a single shard;
  different keys landed on different shards.
- End to end: 329 aircraft over London, `airplanes.live -> poller -> Kinesis ->
  processor -> DynamoDB -> WebSocket`, first delivery +9.6s.
