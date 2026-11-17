# INC-003: A write on the MariaDB replica stopped replication (errant GTID)

| | |
|---|---|
| Date | 2026-11-17 |
| Engine | MariaDB 11.4, GTID replication (primary server_id 1, replica server_id 2) |
| Drill | `drills/mariadb-replication-broken.sh` |
| Severity | SEV-2 (the replica stopped applying changes and its data diverged; the primary was unaffected) |
| Time to detect | 2 s in the drill, because health ran right after. With the 5-minute health timer, up to 5 minutes. With the errant-GTID check added after this drill, detection comes at the stray write itself |
| Time to recover | 15 s from the first CRIT to replication running again with matching checksums, the cause already known |

Evidence: `results/m7-drill-mariadb-replication.txt`.

## What users would have seen

Nothing at first: the application writes to the primary. The damage is on the
replica:

- Reads served from the replica (reports, the nightly backup) would show a
  customer that does not exist on the primary.
- The replica stops receiving new changes at all, and falls further behind with
  every write on the primary.
- A failover to the replica during the outage would lose every transaction
  after GTID 0-1-39. It would also publish the stray row as if it were real data.

## Timeline

| Time (UTC) | Event |
|---|---|
| 16:22:14 | Drill started. Both servers at GTID 0-1-39, checksums equal |
| 16:22:14 | On the replica, as root: `INSERT INTO customers` takes id 5001. `read_only = ON` does not stop it, and the replica logs it as GTID 0-2-40 |
| 16:22:14 | On the primary, as shop_app: a signup also gets id 5001 (GTID 0-1-40), then its first order (0-1-41) |
| 16:22:16 | The replica's SQL thread stops with error 1950: GTID 0-1-40 would be out of order with 0-2-40 in strict mode |
| 16:22:16 | First signal: `dbops health` reports `CRIT replication IO=Yes SQL=No` with the error text, and exits 2 |
| 16:22:28 | Root cause identified from `gtid_binlog_pos = 0-2-40`: a transaction the replica wrote itself |
| 16:22:29 | Fix: stray row deleted with `sql_log_bin = 0`; `RESET MASTER` on the replica; `START REPLICA` |
| 16:22:31 | Recovered: IO=Yes SQL=Yes at 0-1-41, lag 0 s, `CHECKSUM TABLE` equal on both servers |

## Root cause

A root session on the replica wrote a row. Two settings let that happen and then
made it fatal:

1. `read_only = ON` blocks ordinary accounts only. Root has every privilege,
   including READ_ONLY ADMIN, which bypasses `read_only`. Health showed
   `OK read_only ON` the whole time.
2. The replica runs `log_slave_updates` and `gtid_strict_mode`. So the stray
   write was logged in the replica's own binlog as GTID 0-2-40, sequence 40 in
   domain 0. When the primary's 0-1-40 arrived, strict mode refused to log a
   second sequence 40 and stopped the SQL thread.

I expected a different failure. I predicted error 1062, a duplicate key on
customers.id 5001. Strict GTID mode stopped the replica first, before the row
was applied. Without strict mode, it would have stopped on 1062 instead. And if
the stray row had not collided with anything, replication would have carried on
with diverged data and no error at all. In that case only the errant GTID shows
that something is wrong.

The primary's check (`OK replicas connected 1`) stayed green throughout. The IO
thread kept reading the binlog, and only the replica's SQL thread had stopped.

## How it was detected

```
$ dbops health --engine mariadb
mariadb-replica
  CRIT    replication                IO=Yes SQL=No  An attempt was made to binlog GTID 0-1-40 which would
          create an out-of-order sequence number with existing GTID 0-2-40, and gtid strict mode is enabled
  OK      read_only                  ON
CRIT    15 OK, 0 WARN, 1 CRIT, 0 UNKNOWN          (exit 2)

$ SELECT @@gtid_slave_pos, @@gtid_binlog_pos;      -- on the replica
0-1-39    0-2-40          <- 0-2-*: written by server_id 2, the replica itself
```

Re-run with the errant-GTID check added after this drill: the stray write alone,
before any primary traffic. Replication still looks healthy, and the new check
catches it:

```
  OK      replication                IO=Yes SQL=Yes GTID 0-1-43
  CRIT    errant GTIDs               1              written on the replica, not replicated: 0-2-44
```

## Fix

On the replica, in order (`drills/mariadb-replication-broken.sh fix`):

```sql
-- 1. Remove the stray row. sql_log_bin = 0 keeps the repair out of the binlog,
--    so it does not create a second errant GTID.
SET SESSION sql_log_bin = 0;
DELETE FROM customers WHERE email = 'drill-stray@example.com';

-- 2. Drop the errant GTID. RESET MASTER clears the replica's own binlog and
--    gtid_binlog_pos. gtid_slave_pos (0-1-39, what it applied from the primary)
--    is kept, so replication resumes from exactly the right place.
STOP REPLICA;
RESET MASTER;
START REPLICA;
```

Then check that both servers hold the same data:
`drills/mariadb-replication-broken.sh compare` runs `CHECKSUM TABLE customers,
orders` on both and compares the results.

What was rejected, and why:

- **Skipping the failing event** (`SET GLOBAL sql_slave_skip_counter = 1`, or
  moving `gtid_slave_pos` past it) gets replication running in seconds. But it
  throws away the primary's real signup and keeps the fake one: the replica
  would hold the wrong customer 5001 for good.
- **Rebuilding the replica from a backup** (`make replication`) always works,
  but takes minutes rather than seconds. It is the right call when the
  divergence is larger than one known row, or when the stray writes cannot be
  found.

The cost of `RESET MASTER`: it deletes the replica's binlogs. Here nothing reads
them (no replica hangs off this replica, and backups use mariadb-backup), so the
cost was nil. Check that before running it elsewhere.

## What would stop it happening again

- [x] Health check `errant GTIDs` on every replica: CRIT when `gtid_binlog_pos`
      holds a GTID with the replica's own server_id. It catches the stray write
      at once, even when it would not break replication (`dbops/engines/mariadb.py`,
      tests in `tests/test_replication.py`).
- [ ] Day-to-day admin accounts without READ_ONLY ADMIN, so `read_only` applies
      to people as well. Keep root for break-glass use, and add an access-review
      rule (`dbops access`) for accounts that can write to a replica. MariaDB
      has no `super_read_only` like MySQL's, so this privilege split is what
      stands in for it.
- [ ] A scheduled `CHECKSUM TABLE`, or pt-table-checksum, comparison between
      primary and replica. It catches divergence that leaves no errant GTID,
      for example a write made with `sql_log_bin = 0`.
- [x] Runbook entry: "Replica SQL thread stopped (1950 / 1062)" (RUNBOOK 2.11).
