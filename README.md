# DBOps Lab

A database operations lab: Oracle, MariaDB, MongoDB and PostgreSQL running side by
side in Docker, with the scripts a DBA runs every day: health checks, backup and
restore validation, slow query hunting and access reviews. It also has drills
that break the databases on purpose so the fixes can be practised and written up.

The application databases stay small on purpose. This project is about running
the databases, not the apps that use them.

## Quick start

Needs Docker Desktop with at least 6 GB of memory.

```bash
cp .env.example .env    # change the passwords
make up                 # Oracle, MariaDB x2, MongoDB x3, PostgreSQL
make replication        # seed the MariaDB replica, start GTID replication
make install            # the dbops CLI, in .venv
make health             # one verdict for all seven instances
make backup             # back up and restore-test all four engines
make                    # list every command
```

## dbops health (M2)

Connects to every instance as a read-only `dbops_monitor` account and checks:

| | Oracle | MariaDB | MongoDB | PostgreSQL |
|---|---|---|---|---|
| CPU, memory, storage of the data directory | ✓ | ✓ | ✓ | ✓ |
| Connections against the limit | sessions | ✓ | ✓ | ✓ |
| Statements running over 60 s | ✓ | ✓ | ✓ | ✓ |
| Blocked sessions | ✓ | | | ✓ |
| Tablespace usage | ✓ | | | |
| Replication | | threads, lag, `read_only`, replica connected | primary, member state, lag | |

```bash
dbops health                     # text, grouped by instance
dbops health --engine oracle     # one engine; repeat the flag for more
dbops health --json              # for a monitoring system
dbops health --watch 30          # refresh every 30 s
```

Exit codes follow the Nagios plugin convention (0 OK, 1 WARN, 2 CRIT, 3 UNKNOWN),
so it drops straight into cron, a systemd timer or any Nagios-compatible monitor.
Thresholds are all in [dbops/thresholds.py](dbops/thresholds.py).

Measured output is in [results/](results/): a healthy run, and a run with four
faults injected (stopped replica SQL thread, stopped Mongo member, an Oracle row
lock, long queries), each caught with the blocker and the wait event named.

## Backups and restore tests (M3)

A backup counts once it has been restored and checked against its source.
`make backup` takes one per engine, restores each into a throwaway container and
compares the data, then records the result in `backups/<engine>/<id>/manifest.json`:

| Engine | Backup, taken from | Restore test |
|---|---|---|
| Oracle | RMAN `BACKUP DATABASE PLUS ARCHIVELOG`, compressed, with control file + spfile autobackup | `RESTORE ... VALIDATE`, then a full restore onto an **empty** container: spfile and control file from the autobackup, `RESTORE`, `RECOVER` to the backup's SCN, `OPEN RESETLOGS`. Each SHOP table's row count and `ORA_HASH` is compared with the source `AS OF` that SCN |
| MariaDB | `mariadb-backup --slave-info`, prepared, from the **replica** | a fresh MariaDB started on a copy of the data dir; row count + `CHECKSUM TABLE` per table |
| MongoDB | `mongodump --oplog`, from a **secondary** | `mongorestore --oplogReplay` into a fresh mongod; count + `dbHash` per collection |
| PostgreSQL | `pg_dump -Fc` + `pg_dumpall --globals-only` | `pg_restore` into a fresh postgres; count + md5 of the rows per table |

```bash
make backup              # all four; one engine: make backup e=oracle
make backups             # every backup on disk and its restore test result
```

`dbops health` reports the newest backup that **passed** its restore test per
engine: WARN after 26 h, CRIT after 50 h, CRIT at once if the newest one failed.
Measured: Oracle backs up in about 45 s (465 MB) and restores in about 2.5 min;
the others take seconds. A backup piece with 4 KB overwritten is caught by
`RESTORE VALIDATE` ([results/m3-oracle-corrupt-piece.txt](results/m3-oracle-corrupt-piece.txt)).

## Slow queries (M4)

`dbops slow` ranks each engine's statements by total time, reads the plan each
one used, and says why it is slow and what would fix it. `dbops workload` runs a
fixed application query mix against all four engines so there is something to find.

| Engine | Where the statements come from | Plan |
|---|---|---|
| Oracle | `V$SQL` for the SHOP schema | `V$SQL_PLAN`: the plan the cursor actually ran; optimizer statistics from `DBA_TAB_STATISTICS` |
| MariaDB | the slow query log on the primary (over 0.5 s, or a full scan of 1,000+ rows), grouped with literals replaced | `EXPLAIN FORMAT=JSON` of the slowest instance; estimated rows vs rows read flags stale statistics |
| MongoDB | the profiler (`system.profile`) on all three members (over 500 ms, or 1,000+ documents read), grouped by query shape | the profile entry's own plan summary (`COLLSCAN`, in-memory sort) |
| PostgreSQL | `pg_stat_statements` | `EXPLAIN (GENERIC_PLAN)`: the stored text is explained without running it or filling in values |

Index suggestions follow ESR: **E**quality columns, then **S**ort, then **R**ange,
so one index serves the filter, returns rows already in order and narrows the range.
Tables under 1,000 rows get no suggestion. Suggestions are for review: nothing is applied.

```bash
make slow a=--reset      # clear each engine's statistics, to measure from now
make workload            # 30 s of the application mix, latency per query
make slow                # top 3 per engine: plan, why, fix
make slow a=--ddl        # just the fixes, as a script to review
```

Measured on this lab ([results/m4-before-after.txt](results/m4-before-after.txt)), applying the
suggested fixes ([results/m4-fixes.sql](results/m4-fixes.sql)):

| p50 latency, ms | Oracle | MariaDB | MongoDB | PostgreSQL |
|---|---|---|---|---|
| orders for a customer, newest first | 1.14 → 0.70 | 0.51 → 0.46 | 13.76 → 0.33 | 1.50 → 0.36 |
| orders in the last day | 5.69 → 0.75 | 3.71 → 0.53 | 14.47 → 0.41 | 4.22 → 0.37 |
| queries per second, whole mix | 1.6× | 1.4× | 16× | 2.6× |

The first "after" run made MariaDB **slower**: `pending_totals` went from 6.9 ms to
24 ms. With the new `status` index the optimizer drove the join from `customers`,
whose statistics said 77 rows (it has 5,000), and probed `orders` 5,000 times.
`dbops slow` reported the stale statistics; `ANALYZE TABLE customers` brought it to
5.2 ms. An index can make a plan worse: measure after every change.

The indexes live only in the running lab. `make reset` starts again without them.

Operating procedures and troubleshooting are in [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Milestones

| | Milestone | Branch | What it proves |
|---|---|---|---|
| M1 | Lab stack | `feat/m1-lab-stack` | Oracle 23ai Free, MariaDB primary + replica (GTID), a 3-node MongoDB replica set and PostgreSQL with `pg_stat_statements`, all in one Compose file |
| M2 | Health checks | `feat/m2-health-checks` | `dbops health`: CPU, memory, storage, connections, replication lag, Oracle tablespaces and long sessions, as text or `--json` |
| M3 | Backup and restore validation | `feat/m3-backup-restore` | RMAN, mariadb-backup, mongodump and pg_dump, each restored into an empty container and compared with the source |
| M4 | Slow queries | `feat/m4-slow-queries` | The worst queries from the MariaDB slow log, the Mongo profiler, `pg_stat_statements` and Oracle `V$SQL`, ranked, with index suggestions |
| M5 | Access review | `feat/m5-access-review` | Every user, role and privilege across the four engines, plus unused accounts and password age, as an evidence report |
| M6 | Ansible provisioning | `feat/m6-ansible-provisioning` | MariaDB and the M2/M3 jobs as systemd timers on a Linux VM |
| M7 | Incident drills | `feat/m7-incident-drills` | Full tablespace, broken replication, a dropped index, a lost Mongo primary, each with a postmortem |

## Layout

```
engines/     one folder per database engine: config and init scripts the containers mount
scripts/     shell scripts for operations, grouped by engine; backup/ has one script per engine
backups/     where backups land (git-ignored), one folder per engine
dbops/       the Python CLI: health (M2); backup verification, slow queries, access review to come
tests/       unit tests (no databases needed) and live tests against the lab
drills/      scripts that cause an incident on purpose (M7)
ansible/     inventory, playbooks and roles for a Linux VM (M6)
docs/        RUNBOOK.md, plus incidents/ for postmortems
results/     measured output: health snapshots, restore timings, query plans
```

## Conventions

- **Branches:** `<type>/<kebab-case>`, with the milestone in the name for milestone
  work: `feat/m2-health-checks`, `fix/replica-gtid-drift`, `docs/runbook-backups`,
  `drill/full-tablespace`. Branch from `main` once the previous milestone is merged.
- **Commits:** one change per commit, imperative mood, milestone in brackets where it
  applies: `Add RMAN level-0 backup and restore validation (M3)`.
- **Merging:** a pull request per milestone, squash-free so the history shows the steps.
