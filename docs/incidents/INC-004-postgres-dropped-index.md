# INC-004: Index dropped on PostgreSQL; the busiest query became 7.7x slower

| | |
|---|---|
| Date | 2026-11-17 |
| Engine | PostgreSQL 17 (database shop) |
| Drill | `drills/postgres-dropped-index.sh` |
| Severity | SEV-3 (slower, no errors; throughput fell to 30%, which a busier system would feel as SEV-2) |
| Time to detect | `dbops health` never flagged it. Found by hand from workload latency 23 s after the drop. `dbops slow` ranked the query first but did not name the fix until a rule was added during the drill |
| Time to recover | 79 s from the drop to the index being rebuilt and valid; the index build itself took under 1 s |

Evidence: `results/m7-drill-postgres-index.txt`.

## What users would have seen

No errors, only slower pages. "My last 20 orders" took 1.16 ms at p50 instead
of 0.15 ms. The database finished 660 of those per second instead of 2,186:
the workload runs closed-loop, so slower queries mean fewer of them. On a busier
system, that lost throughput turns into queueing, connection pool exhaustion
and timeouts.

| `orders_for_customer` | calls in 20 s | p50 ms | p95 ms | calls/s | plan |
|---|---|---|---|---|---|
| Before (index present) | 43,729 | 0.15 | 0.20 | 2,186 | Bitmap Index Scan on ix_orders_customer_id_created_at, 15 buffers |
| Index dropped | 13,190 | 1.16 | 1.32 | 660 | Seq Scan (EXPLAIN ANALYZE) or a backward Index Scan on ix_orders_created_at (generic plan), 368-371 buffers |
| Index rebuilt | 44,335 | 0.15 | 0.20 | 2,217 | Bitmap Index Scan, 15 buffers |

The other queries kept their latency, but all four ran less often: the workload
runs all four queries in one loop.

## Timeline

| Time (UTC) | Event |
|---|---|
| 16:24:29 | Baseline workload: `orders_for_customer` p50 0.15 ms, 2,186 calls/s |
| 16:24:50 | Drill started: `DROP INDEX CONCURRENTLY ix_orders_customer_id_created_at` |
| 16:24:50 | `dbops health --engine postgres`: `OK 8 OK, 0 WARN, 0 CRIT`. No signal |
| 16:24:53 | Workload: p50 1.16 ms, 660 calls/s |
| 16:25:13 | First signal: `dbops slow` ranks the statement first, 13,114 ms total and 368 blocks per call, with no fix suggested |
| 16:25:58 | Root cause identified. With a rule added to `dbops slow` for an index scan followed by a filter, it names the missing index |
| 16:26:09 | Fix: `CREATE INDEX CONCURRENTLY`, then `ANALYZE orders`, under load |
| 16:26:17 | Recovered: p50 0.15 ms, p95 0.20 ms, 2,217 calls/s |

## Root cause

The composite index `(customer_id, created_at)` served the busiest query in the
application:
`WHERE customer_id = $1 ORDER BY created_at DESC LIMIT 20`. With it gone, the
planner had two options, and both read close to the whole table:

- **with the customer id known** (EXPLAIN ANALYZE): a sequential scan of 50,000
  rows, keeping 10;
- **as a generic plan** (what a prepared statement may use): walk
  `ix_orders_created_at` backwards, newest first, and throw away every other
  customer's rows until 20 match.

Nothing errors, so nothing that only checks whether queries succeed notices.

Contributing:

1. `dbops health` has no latency or query-cost check. It reported OK the whole
   time.
2. `dbops slow` from M4 only suggested indexes for sequential scans with a
   filter. The generic plan here is an Index Scan, so the report ranked the
   query first but gave no fix. A dropped index often looks like this, because
   another index is left over for the planner to use instead.

## How it was detected

```
$ dbops health --engine postgres
OK      8 OK, 0 WARN, 0 CRIT, 0 UNKNOWN

$ dbops workload --engine postgres --seconds 20
  postgres  orders_for_customer    13190     1.16     1.32    659.5        (was 0.15 / 0.20 / 2186.4)

$ dbops slow --engine postgres          # with the rule added in M7
  #1  13,190 calls   total 13,114 ms   mean 0.99 ms   368 blocks/call
      SELECT id, status, total_cents, created_at FROM orders WHERE customer_id = $1 ORDER BY created_at DESC LIMI...
      plan  Limit > Index Scan on orders
      why   index scan on ix_orders_created_at then filter of orders (50,000 rows), filtering on customer_id, sorting by created_at
      fix   CREATE INDEX ix_orders_customer_id_created_at ON orders (customer_id, created_at);
```

## Fix

```sql
-- drills/postgres-dropped-index.sh fix
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_orders_customer_id_created_at
  ON orders (customer_id, created_at);
ANALYZE orders;
```

`CONCURRENTLY` lets reads and writes continue during the build. A plain
`CREATE INDEX` holds a lock that blocks every INSERT, UPDATE and DELETE on orders
until it finishes. A 10-second workload ran alongside the build: 18,527 calls,
with no errors.

If a concurrent build fails part way, it leaves an INVALID index behind. Writes
still have to maintain that index, but no query uses it. The fix script checks
for one (`pg_index.indisvalid`) and drops it before building again.

## What would stop it happening again

- [x] `dbops slow` flags an Index Scan whose filter holds an equality the
      index does not cover, and suggests the index in ESR order
      (`dbops/slow/postgres.py`, tests in `tests/test_slow.py`).
- [ ] An "expected indexes" check in `dbops health`: compare each engine's
      index list (`pg_indexes` here) against a list kept in the repo, and go
      CRIT when one is missing or INVALID. Missing indexes are then caught in
      the first 5-minute health run instead of by people noticing slowness.
- [ ] A latency check in `dbops health` from `pg_stat_statements`: WARN when a
      statement's mean time is several times its usual value.
- [ ] Index drops go through a migration that is reviewed and runs on staging
      first. A "drop unused indexes" job must not trust `idx_scan = 0` when the
      statistics were reset recently (`pg_stat_user_indexes`, together with
      `pg_stat_database.stats_reset`).
- [x] Runbook entry: "Query suddenly slower, no errors" (RUNBOOK 2.11).
