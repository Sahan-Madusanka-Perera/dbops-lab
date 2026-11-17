# INC-005: MongoDB primary lost; writes paused 10.5 s during the election

| | |
|---|---|
| Date | 2026-11-17 |
| Engine | MongoDB 8.0, replica set rs0 (mongo1 priority 2, mongo2 and mongo3 priority 1) |
| Drill | `drills/mongo-primary-lost.sh` |
| Severity | SEV-2 (writes paused for 10.5 s and the set lost its spare member until mongo1 returned; no data lost) |
| Time to detect | 11 s for the replica set itself to detect and elect a new primary. `dbops health` showed CRIT on its first run after the kill; with the 5-minute health timer, up to 5 minutes |
| Time to recover | Writes: 10.5 s, automatic. Full redundancy: 63 s from the kill to mongo1 being primary again. 11 s of that came after `docker start`; the rest was the drill waiting before starting it |

Evidence: `results/m7-drill-mongo-primary.txt`.

## What users would have seen

On an application connected with the replica set string, the way the drill's
writer connects:

- **No errors.** For 10.5 s every write simply took longer: the driver held each
  one until it could find a new primary, then retried it there. The writer
  wrote one document every 100 ms and got 281 acknowledged, 0 failed.
- **Errors where timeouts are short.** An application with a request timeout
  under about 11 s would have returned errors for that window instead.

One client would have failed for the whole outage: the lab's own
`dbops workload`. It pins itself to mongo1 with `directConnection=True`, because
the replica set advertises Docker-internal hostnames the Mac cannot resolve.
That is acceptable for a benchmark, but an application connecting that way has
no failover at all.

## Timeline

| Time (UTC) | Event |
|---|---|
| 16:28:11 | Drill started. mongo1 PRIMARY, mongo2 and mongo3 SECONDARY. Writer running from mongo3's container |
| 16:28:16.6 | `docker kill dbops-mongo1` (SIGKILL: no step-down, no goodbye to the other members). Last acknowledged write |
| 16:28:27.2 | mongo2 elected PRIMARY; writes acknowledged again after 10.5 s |
| 16:28:51 | First signal from monitoring: `dbops health` CRIT on `mongo1 reachable`, `mongo1 container` and `rs0 member mongo1:27017`; exit 2 |
| 16:28:51 | Root cause identified: the container is not running (`dbops-mongo1 is not running`) |
| 16:29:08 | Fix: `docker start dbops-mongo1` |
| 16:29:19 | mongo1 caught up and took the primary back (priority 2). The writer saw no gap |
| 16:29:45 | Recovered: health 22 OK; lag 1 s on both secondaries |

## Root cause

A drill: the primary's process was killed. The replica set handled it as it is
designed to:

1. The secondaries send heartbeats every 2 s. After `electionTimeoutMillis`
   (10 s) without hearing from the primary, mongo2 called an election.
2. mongo2 and mongo3 are 2 of 3 votes, a majority, so mongo2 won. The 10.5 s
   write gap is almost all that timeout. Electing the new primary took the
   rest.
3. The driver noticed the new primary from its own monitoring and retried the
   pending writes there (`retryWrites=true`, the default).

What the drill was really testing is what happens next. Until mongo1 returned,
the set had no spare member: losing either remaining member would have left 1 of
3 votes. No primary could then be elected, and every write would stop.
`dbops health` showed this as CRIT on the missing member. It did not say how
little was left; see the follow-ups.

My prediction for the rejoin was wrong. I expected mongo1's priority of 2 to
cause a second outage when it took the primary back. It did not:

- A priority takeover is a planned election. mongo1 first caught up from the
  oplog, then mongo2 stepped down and handed over.
- The writer saw no gap longer than its own 100 ms interval: 381 acknowledged,
  0 failed.

A crash costs about 10 s of writes. A planned handover costs well under one.

## How it was detected

```
writer: 16:28:27.174  writes acknowledged again after 10.5 s without one
writer: writer (inject): 281 writes acknowledged, 0 failed, over 40 s
writer: longest time without an acknowledged write: 10.5 s (16:28:16.628 to 16:28:27.174 UTC)

$ dbops health --engine mongo
mongo1
  CRIT    reachable        127.0.0.1:27017: [Errno 61] Connection refused ...
  CRIT    container        dbops-mongo1 is not running
rs0
  OK      primary          mongo2:27017
  CRIT    member mongo1:27017   (not reachable/healthy) ... Host not found
  OK      lag mongo3:27017 0s
CRIT    15 OK, 0 WARN, 3 CRIT, 0 UNKNOWN          (exit 2)
```

## Fix

```bash
docker start dbops-mongo1          # on a real server: start mongod, or replace the host
drills/mongo-primary-lost.sh fix   # does the same, with the writer measuring the rejoin
```

mongo1 rejoined as a secondary, copied the writes it had missed from the oplog
(about 40 s of them), and then took the primary back. Nothing had to be done by
hand: there was no rollback, because SIGKILL came between writes and every
acknowledged write was already on a majority.

Had mongo1 been gone for good (a lost disk), the fix would be one of:

- add a new member with `rs.add()` and let it do an initial sync;
- `rs.reconfig()` it out of the set, then add a replacement.

Both need the remaining two members to agree.

## What would stop it happening again

A primary will fail some day. The goal is that losing one costs about 10 s,
not an outage.

- [x] Applications connect with the replica set string and `retryWrites=true`.
      The drill's writer is the reference: `drills/mongo-writer.js`.
- [ ] Request timeouts above the election window (about 12 s), or retry on
      `NotWritablePrimary` / `InterruptedDueToReplStateChange`. The window can be
      shortened by lowering `electionTimeoutMillis`, at the cost of elections on
      brief network hiccups.
- [ ] A health check for redundancy left: WARN when fewer than all members are
      healthy, CRIT when one more loss would leave the set without a majority.
      Today the missing member is CRIT, but the result does not say "one more
      failure stops all writes".
- [ ] Make the replica set's hostnames resolvable from the Mac (hosts entries,
      or advertise the host ports), so `dbops workload` can use the replica set
      string and fail over like an application.
- [x] Runbook entry: "MongoDB primary lost / member down" (RUNBOOK 2.11).
