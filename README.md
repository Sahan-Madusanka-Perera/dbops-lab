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

Operating procedures and troubleshooting are in [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Milestones

| | Milestone | Branch | What it proves |
|---|---|---|---|
| M1 | Lab stack | `feat/m1-lab-stack` | Oracle 23ai Free, MariaDB primary + replica (GTID), a 3-node MongoDB replica set and PostgreSQL with `pg_stat_statements`, all in one Compose file |
| M2 | Health checks | `feat/m2-health-checks` | `dbops health`: CPU, memory, storage, connections, replication lag, Oracle tablespaces and long sessions, as text or `--json` |
| M3 | Backup and restore validation | `feat/m3-backup-restore` | RMAN backup + `RESTORE VALIDATE`, mariabackup, mongodump and pg_dump, each restored into a scratch instance and compared |
| M4 | Slow queries | `feat/m4-slow-queries` | The worst queries from the MariaDB slow log, the Mongo profiler, `pg_stat_statements` and Oracle `V$SQL`, ranked, with index suggestions |
| M5 | Access review | `feat/m5-access-review` | Every user, role and privilege across the four engines, plus unused accounts and password age, as an evidence report |
| M6 | Ansible provisioning | `feat/m6-ansible-provisioning` | MariaDB and the M2/M3 jobs as systemd timers on a Linux VM |
| M7 | Incident drills | `feat/m7-incident-drills` | Full tablespace, broken replication, a dropped index, a lost Mongo primary, each with a postmortem |

## Layout

```
engines/     one folder per database engine: config and init scripts the containers mount
scripts/     shell scripts for operations (replication setup, backups), grouped by engine
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
