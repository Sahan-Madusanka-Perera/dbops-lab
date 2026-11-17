# INC-002: Oracle APP_DATA tablespace full; batch inserts failed with ORA-01653

| | |
|---|---|
| Date | 2026-11-17 |
| Engine | Oracle 23ai Free (FREEPDB1, schema SHOP) |
| Drill | `drills/oracle-tablespace-full.sh` |
| Severity | SEV-2 (writes failing; reads unaffected; no data lost) |
| Time to detect | 1 s in the drill, because health ran right after the load. With the 5-minute health timer, up to 5 minutes |
| Time to recover | 47 s from the first CRIT to the fix being verified, the cause already known. In a real incident, add the time to page someone |

Evidence: `results/m7-drill-oracle-tablespace.txt`.

## What users would have seen

Order batches failing with `ORA-01653: unable to increase tablespace APP_DATA`.
Everything else looked normal:

- Single-row inserts still succeeded, because they fit in the last free blocks.
- Every read query ran at normal speed. The workload during the incident ran
  17,003 `orders_for_customer` calls at a p50 of 0.44 ms.

A partial failure like this is easy to misread as an application bug.

## Timeline

| Time (UTC) | Event |
|---|---|
| 16:19:42 | Drill started. APP_DATA 79.1% used, 16 MB of a fixed 20 MB |
| 16:19:43 | The batch load commits 12 batches (24,000 rows); the 13th fails with ORA-01653 on SHOP.ORDERS |
| 16:19:43 | First signal: `dbops health` reports `CRIT tablespace APP_DATA 99.7%` and exits 2 |
| 16:19:43 | Root cause identified: fixed-size datafile, AUTOEXTEND OFF, and nothing more to allocate |
| about 16:20 | Checked by hand, not logged: a 500-row insert fails with ORA-01653, a 1-row insert succeeds |
| 16:20:08 | Workload: reads unaffected |
| 16:20:30 | Fix: datafile resized to 64 MB, AUTOEXTEND ON NEXT 16M MAXSIZE 1G |
| 16:20:30 | Recovered: the same 500-row insert succeeds; health `OK tablespace APP_DATA 2%`, 14 OK |

## Root cause

APP_DATA has one 20 MB datafile created with `AUTOEXTEND OFF`
(`engines/oracle/init/01-shop-schema.sql`). Every table and index in SHOP lives in
it. The batch job needed new extents, and once the file had no free extent left,
Oracle could not grow the segment. ORA-01653 is the table form of the error; when
an index needs the extent, it is ORA-01654.

Contributing: the tablespace was already at 79% before the load, just under the
85% WARN threshold. The load took it from there to full in about 1 second, so a
check that runs every 5 minutes would never have shown a WARN first. A percentage
threshold catches slow growth. It does not catch a single job that needs more
space than is left.

## How it was detected

```
16:19:43  batch load: 2,000 orders per commit, as SHOP, until the tablespace is full
12 batches committed (24000 rows), then:
ORA-01653: unable to increase tablespace APP_DATA by 1MB during insert or update
on table SHOP.ORDERS

$ dbops health --engine oracle
  CRIT    tablespace APP_DATA        99.7%          20 MB of 20 MB
CRIT    13 OK, 0 WARN, 1 CRIT, 0 UNKNOWN          (exit 2)
```

## Fix

```sql
-- as SYSDBA, in FREEPDB1 (drills/oracle-tablespace-full.sh fix)
ALTER DATABASE DATAFILE '/opt/oracle/oradata/FREE/FREEPDB1/app_data01.dbf' RESIZE 64M;
ALTER DATABASE DATAFILE '/opt/oracle/oradata/FREE/FREEPDB1/app_data01.dbf'
  AUTOEXTEND ON NEXT 16M MAXSIZE 1G;
```

The fix sets a MAXSIZE on purpose. An unbounded autoextend only moves the outage
to the filesystem, where it takes every tablespace and the redo logs down with it
(see INC-001 for what a full disk does to the lab). With a cap, `dbops health`
measures usage against the cap, so the 85% and 95% thresholds still mean
something.

The other option was to add a second datafile (`ALTER TABLESPACE app_data ADD
DATAFILE ... SIZE 64M AUTOEXTEND ON MAXSIZE 1G`). Resizing is simpler while the
file is this small.

To put the lab back for the next run: `drills/oracle-tablespace-full.sh cleanup`.
It deletes the drill's rows, runs `SHRINK SPACE CASCADE` on SHOP.ORDERS so the
datafile can shrink, and sets the file back to 20 MB with AUTOEXTEND OFF.

## What would stop it happening again

- [x] Every application tablespace autoextends, with a MAXSIZE that the
      filesystem can hold (the fix above).
- [ ] A free-space check in absolute terms next to the percentage: WARN when the
      free MB left is less than the largest batch writes. 4 MB free was enough
      for single rows but not for a 2,000-row batch.
- [ ] A growth-rate check: compare usage across health runs and WARN when, at
      the current rate, the tablespace will be full in less than 7 days.
- [ ] Batch jobs check free space first and run in smaller commits, so a failure
      loses one small batch rather than a large one.
- [x] Runbook entry: "ORA-01653 / ORA-01654" (RUNBOOK 2.11).
