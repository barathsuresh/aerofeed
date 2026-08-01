# Poll scheduling and the per-client cell cap

The poller issues one upstream request per *distinct* region cell with an
active subscriber, on a 1-minute EventBridge rule. airplanes.live allows 1
request/second, so a 60-second cycle has a hard ceiling of **~60 distinct
cells for the entire service** — not per client, for everyone connected at
once.

`MAX_CELLS_PER_CLIENT` (`core/geo.py`, mirrored in
`terraform/variables.tf`) caps how many cells a single zoomed-out viewport can
claim, currently 4. That bounds one client's share of the global budget — at
4, roughly 15 clients could each be watching an entirely different part of
the world before the per-cycle budget is gone. It says nothing about how many
*distinct* clients or regions the service as a whole is polling.

## What's missing: a per-cycle cap across clients

There is no cap on the total number of distinct cells the poller will
attempt across all connected clients in a single cycle. Each client is
individually bounded to 4 cells, but 14+ clients watching 14+ different
regions will collectively exceed the ~60-cell, 1 req/s upstream budget, and
the poller has no rotation or backpressure to fall back on — it just issues
every distinct active cell's request as fast as pacing allows, taking longer
than 60s to finish the cycle once the budget is exceeded, and drifting the
schedule.

Raising `MAX_CELLS_PER_CLIENT` above 4 makes this worse per client and pulls
the collective ceiling in sooner. Fixing it properly means rotating which
cells get polled each cycle (or otherwise rationing across clients) instead
of only rationing within one client's viewport — not yet implemented.
