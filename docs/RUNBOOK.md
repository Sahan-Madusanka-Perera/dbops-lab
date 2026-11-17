# Runbook: DBOps Lab

## 1. What runs where

| Container | Engine | Host port | Connect as |
|---|---|---|---|
| `dbops-oracle` | Oracle Database 23ai Free, PDB `FREEPDB1` | 1521 | `shop_app` (app), `shop` (schema owner), `system` / `sys` (`ORACLE_PASSWORD`) |
| `dbops-mariadb-primary` | MariaDB 11.4, binlog + GTID, `server_audit` | 3316 | `shop_app` (app), `shop` (schema owner), `root` |
| `dbops-mariadb-replica` | MariaDB 11.4, `read_only`, `server_audit` | 3317 | `shop_app`, `root` |
| `dbops-mongo1..3` | MongoDB 8.0, replica set `rs0`, access control (keyfile) | 27017-27019 | `shop_app` (app), `dbops_admin`; `authSource=admin` |
| `dbops-postgres` | PostgreSQL 17 + `pg_stat_statements`, connection log | 5442 | `shop_app` (app), `postgres` |

`dbops_monitor` (read-only) exists in every engine. Every account that can log in is
listed, with its owner, in `policy/access-policy.toml`.

Every engine holds the same `shop` data: 5,000 customers and 50,000 orders. `orders`
has no index on `customer_id` or `status` on purpose.

Ports are set in `.env`. Inside the Docker network the hosts are the service
names: `oracle`, `mariadb-primary`, `mariadb-replica`, `mongo1`, `postgres`.

## 2. Standard operating procedures

### 2.1 First-time setup

Docker Desktop needs at least 6 GB of memory (Settings → Resources). Oracle alone
takes about 2 GB.

```bash
cp .env.example .env      # then change every password
make up                   # first run pulls about 3 GB of images; Oracle takes a few minutes to create its database
make replication          # seed the MariaDB replica and start replication
make ps                   # every container should say (healthy)
```

### 2.2 Daily start / stop

```bash
make up       # data is kept in named volumes between runs
make down
```

### 2.3 Connect

```bash
make oracle            # SQL*Plus as SHOP
make mariadb           # primary
make mariadb-replica   # replica
make mongo             # mongosh on whichever node is primary, as dbops_admin
make psql
```

### 2.4 Check replication

MariaDB, on the replica:

```sql
SHOW REPLICA STATUS\G
-- Slave_IO_Running: Yes, Slave_SQL_Running: Yes, Seconds_Behind_Master: 0
```

MongoDB:

```js
rs.status().members.map(m => `${m.name} ${m.stateStr} ${m.optimeDate}`)
```

### 2.5 Health checks

```bash
make install     # once
make health      # or: .venv/bin/dbops health --watch 30
```

`dbops health` connects as `dbops_monitor`, which `make up` creates (or
`make accounts` after a password change). What to do when a check fails:

| Check | Status | First move |
|---|---|---|
| `reachable` | CRIT | `make ps`, then `make logs s=<service>` |
| `container` | CRIT | The container stopped: `docker start dbops-<name>`, then find out why in its logs |
| `replication` (MariaDB) | CRIT | Read the error in the detail column. A stopped thread with no error: `START REPLICA;` on the replica. Data drift (duplicate key, missing row): rebuild with `make replication` |
| `replication lag` | WARN/CRIT | Long transaction on the primary, or a slow statement on the replica: check `long-running` on both |
| `read_only` | CRIT | `SET GLOBAL read_only = ON;` on the replica, then look for writes that landed there |
| `primary` / `member` (rs0) | CRIT | `make mongo`, `rs.status()`. A stopped member rejoins on its own once started |
| `tablespace` | WARN/CRIT | Add a datafile or resize: `ALTER TABLESPACE app_data ADD DATAFILE ... SIZE 50M;` |
| `blocked sessions` | WARN/CRIT | The detail names the blocker. Ask its owner, or end it: Oracle `ALTER SYSTEM KILL SESSION 'sid,serial#'`, Postgres `SELECT pg_terminate_backend(pid)` |
| `long-running` | WARN/CRIT | Find out what the statement is before killing it; M4 covers why it is slow |

### 2.6 Backups and restore tests

```bash
make backup              # all four engines; make backup e=oracle for one
make backups             # what is on disk, and whether each passed its restore test
```

Every run takes a backup, restores it into a throwaway `dbops-verify-<engine>`
container, compares the data with the source and removes the container. A
script exits 1 when the restore test fails, so cron or CI sees it.

Before it writes anything, each script checks free space on the host disk:
2-3 GB for MariaDB, MongoDB and PostgreSQL, 8 GB for Oracle (the full restore
writes about 3 GB of datafiles). `scripts/backup/oracle.sh --validate-only`
skips the full restore and needs 3 GB.

Retention: the newest 3 backups per engine (`BACKUP_KEEP` to change it). Oracle
uses RMAN's own: `REDUNDANCY 2`, `DELETE OBSOLETE`, and archived logs already
backed up and older than a day are deleted from the recovery area.

Oracle needs ARCHIVELOG mode for online backups. `make up` turns it on;
`make archivelog` does it on its own (one 30-second restart the first time).

### 2.7 Find and fix slow queries

```bash
make slow a=--reset      # measure from now
make workload            # or wait for real traffic
make slow                # top 3 per engine; make slow a="--top 10" for more
make slow a=--ddl        # the suggested fixes as one script
```

1. Read each finding's `why`. A full scan of a small table, or a query that has
   to read a large result (an aggregate over 5,000 rows), needs no index.
2. A `statistics are stale` / `no optimizer statistics` note comes first: fix the
   statistics (`ANALYZE TABLE`, `DBMS_STATS.GATHER_TABLE_STATS`) and measure again
   before adding an index. The plan may already change.
3. Create the index in the engine's own client (`make oracle`, `make mariadb`,
   `make mongo`, `make psql`). MariaDB: on the primary only; it replicates.
   MongoDB: connect to the primary.
4. `make slow a=--reset`, run the workload again and compare latency per query,
   **all** queries, not just the one you fixed. A new index changes other plans.

Where the data comes from, and how to clear it, per engine:

| Engine | Source | `--reset` does |
|---|---|---|
| Oracle | `V$SQL`, `V$SQL_PLAN` | `ALTER SYSTEM FLUSH SHARED_POOL` in FREEPDB1 (every cursor is hard-parsed again: not on a busy production system) |
| MariaDB | `/var/lib/mysql/slow.log` on the primary | truncates the file |
| MongoDB | `system.profile` in `shop`, each member | records the time in `.dbops/mongo-profile-since`; older entries are ignored (a secondary cannot drop the collection) |
| PostgreSQL | `pg_stat_statements` | `pg_stat_statements_reset()` |

### 2.8 Access review

Quarterly, and after any access change:

```bash
make access                                   # findings, accounts, where login records come from
make access a=--markdown > results/access-$(date +%F).md    # the evidence report, for sign-off
```

1. CRIT first: an engine without access control, an account with no or a default
   password, an admin account nobody approved, an app account with admin rights.
2. For each `unapproved-account`: find the owner. Approve it (add it to
   `policy/access-policy.toml` with an owner and a purpose, through a pull request)
   or lock it. Lock first if nobody claims it within a day:
   `ALTER USER x ACCOUNT LOCK` (Oracle, MariaDB), `ALTER ROLE x NOLOGIN` (PostgreSQL),
   `db.revokeRolesFromUser` / `db.dropUser` (MongoDB).
3. A risk that stays (`admin-any-host` in this lab) gets an `[[exception]]` with the
   reason, who accepted it and an expiry date no more than six months out.
4. Sign the report (reviewer and data owner) and keep it.

Rotate a password: change it in `.env`, then `make accounts`, then restart whatever
uses it. `make accounts` resets every approved account to `.env`.

### 2.9 The database server db-vm1 (M6)

Needs Terraform. `make install` also installs Ansible into `.venv`.

```bash
make vm-up            # create the host (terraform apply)
make vm-provision     # configure it; run it again any time, it changes only what drifted
make vm-check         # what a run would change, with diffs, changing nothing
make vm-verify        # run the backup and health jobs now
make vm-ssh           # log in as ansible
make vm-down          # delete the host and its disks (asks first)
```

On the server:

```bash
sudo dbops health                                   # what the timer runs
sudo dbops backups
systemctl list-timers 'dbops-*'
journalctl -u dbops-health -u dbops-mariadb-backup --since today
sudo tail /var/log/dbops-alerts.log                 # every failed job
sudo systemctl start dbops-mariadb-backup           # back up now
```

Roll out a dbops change: bump `version` in `pyproject.toml`, then `make vm-provision`.
The wheel is built once per version, so without a bump the servers keep the old one.

Rotate a password: change it in `.env`, then `make vm-provision` (the accounts and
`/etc/dbops/dbops.env` are rewritten from it).

### 2.10 Run an incident drill (M7)

Each drill breaks one thing the way it breaks in production, and puts it back:

```bash
make drill d=oracle-tablespace-full        # inject
make drill d=oracle-tablespace-full a=fix  # the remediation from the postmortem
make drill-cleanup d=oracle-tablespace-full
```

| Drill | Breaks | Postmortem |
|---|---|---|
| `oracle-tablespace-full` | APP_DATA (20 MB, AUTOEXTEND OFF) filled by a batch load: ORA-01653 | [INC-002](incidents/INC-002-oracle-tablespace-full.md) |
| `mariadb-replication-broken` | a root write on the replica: errant GTID, SQL thread stops | [INC-003](incidents/INC-003-mariadb-replication-broken.md) |
| `postgres-dropped-index` | `ix_orders_customer_id_created_at` dropped: 7.7x slower, no errors | [INC-004](incidents/INC-004-postgres-dropped-index.md) |
| `mongo-primary-lost` | `docker kill dbops-mongo1`: election, write pause | [INC-005](incidents/INC-005-mongo-primary-lost.md) |

Every step prints UTC timestamps. Keep the output: it is the postmortem's
timeline. Write the postmortem from [incidents/TEMPLATE.md](incidents/TEMPLATE.md).
Run `make health` before and after: a drill is finished when health matches what
it was at the start.

### 2.11 Respond to an incident

First, `make health` (exit 2 = CRIT) and read the CRIT lines. Then find the
symptom below.

**Oracle: `ORA-01653` / `ORA-01654` (unable to extend), or `CRIT tablespace X`.**
Expect small inserts to still work while batches fail. Find the datafile and
whether it can grow:

```sql
SELECT file_name, bytes/1048576 mb, autoextensible, maxbytes/1048576 max_mb
FROM dba_data_files WHERE tablespace_name = 'APP_DATA';
```

Give it room, with a ceiling the filesystem can hold (check `df` first):
`ALTER DATABASE DATAFILE '...' AUTOEXTEND ON NEXT 16M MAXSIZE 1G;`, or `RESIZE`,
or `ALTER TABLESPACE ... ADD DATAFILE`. Never use `MAXSIZE UNLIMITED`.
[INC-002](incidents/INC-002-oracle-tablespace-full.md).

**MariaDB: `CRIT replication IO=Yes SQL=No`, or `CRIT errant GTIDs`.** On the
replica: `SHOW REPLICA STATUS\G` (Last_SQL_Errno, Last_SQL_Error) and
`SELECT @@gtid_slave_pos, @@gtid_binlog_pos`. A GTID in `gtid_binlog_pos` with
the replica's own server_id is a write made on the replica.

- Find the stray rows.
- Undo them with `SET SESSION sql_log_bin = 0` in the same session.
- If replication stopped on error 1950: `STOP REPLICA; RESET MASTER; START
  REPLICA` on the replica. First check that nothing reads the replica's binlogs.
- Then `drills/mariadb-replication-broken.sh compare`.

Do not skip events with `sql_slave_skip_counter`: that keeps the wrong data. If
the stray writes cannot be found, rebuild the replica with `make replication`.
[INC-003](incidents/INC-003-mariadb-replication-broken.md).

**A query suddenly slower, no errors.** Health stays OK here.

1. `make slow a="--engine postgres --reset"`, then let traffic run for a minute.
2. `make slow`: the top statement, its plan, and the missing index.
3. Check for a dropped or INVALID index:
   `SELECT indexrelid::regclass, indisvalid FROM pg_index WHERE NOT indisvalid;`
4. Rebuild with `CREATE INDEX CONCURRENTLY`, never a plain `CREATE INDEX` on a
   live table. Then `ANALYZE` the table.

[INC-004](incidents/INC-004-postgres-dropped-index.md).

**MongoDB: `CRIT reachable` on a member, `CRIT rs0 member ...`.** Check
`rs.status()` from a member that is up.

- If a primary exists, writes work, and the urgent job is the lost redundancy:
  one more loss stops all writes.
- Bring the member back (`docker start`, or start `mongod`). It catches up from
  the oplog by itself.
- If it was gone longer than the oplog window, it needs an initial sync: empty
  its data directory and start it.
- If it is gone for good: `rs.reconfig()` it out of the set, then `rs.add()` a
  replacement.

[INC-005](incidents/INC-005-mongo-primary-lost.md).

### 2.12 Start again from empty

```bash
make reset    # deletes every volume; asks first
make up && make replication
```

## 3. Troubleshooting

### `make vm-provision` changed sshd_config but sshd still accepts passwords

A run failed after the config was written, and the `Restart ssh` handler never ran.
`ansible.cfg` has `force_handlers = True` so that a failed run still runs the handlers
it queued; for a host provisioned before that, restart by hand:
`cd ansible && ../.venv/bin/ansible dbservers -b -m systemd_service -a "name=ssh state=restarted"`.
Check: `ssh -o PubkeyAuthentication=no -p 2222 ansible@127.0.0.1` must say `Permission denied (publickey)`.

### db-vm1 says memory and disk are those of the whole Docker VM

It is a container: `/proc/meminfo` and `df` show Docker Desktop's VM, not the 1 GB
limit. That is why the buffer pool is set in `group_vars/dbservers.yml` instead of
being computed from `ansible_memtotal_mb`. On a real VM both are right.

### `systemctl is-system-running` on db-vm1 says `degraded`

`systemctl --failed` names the unit. In a container, units that need a real kernel
(udev, kernel modules) are masked in the image; anything else is a real failure.

### MongoDB says `requires authentication` or `Authentication failed`

Access control is on (M5). Connect as `dbops_admin`, `shop_app` or `dbops_monitor`
with `authSource=admin`; passwords are in `.env`. If no user works on a lab that
predates M5, the users were never created: `make accounts` creates `dbops_admin`
through the localhost exception (allowed only while no user exists), then the rest.

### A MongoDB member keeps restarting: `permissions on ... are too open` or `keyfile`

The replica set keyfile is `secrets/mongo-keyfile`, made by `make up`. Each member
copies it to `/tmp` owned by `mongodb` with mode 400 before it starts mongod. If it
is missing: `make secrets/mongo-keyfile`. All three members need the same file:
after replacing it, restart all three.

### A new index made a query slower

Seen in M4 on MariaDB: after `ix_orders_status`, the `pending_totals` join went
from 6.9 ms to 24 ms. `make slow` showed `optimizer estimates 77 rows in customers`;
the table has 5,000. InnoDB's persistent statistics were taken while the seed was
still loading. Fix: `ANALYZE TABLE customers;` (5.2 ms afterwards). Check
`information_schema.tables.table_rows` against `COUNT(*)` when a plan looks wrong.

### `dbops slow` shows `could not EXPLAIN`

PostgreSQL: the stored statement has a placeholder where SQL does not allow one.
`interval $1` is rewritten to `$1::interval` already; anything else, explain it by
hand in `make psql` with real values. MariaDB: the slow log holds a statement that
cannot be explained (a `CALL`, or a schema that no longer exists).

### Oracle stays `(health: starting)` for minutes on the first run

Expected: it is creating the database. `make logs s=oracle` and wait for
`DATABASE IS READY TO USE!`. Later starts take about 20 seconds.

### Oracle exits, or the logs mention memory

Docker Desktop has too little memory. Give it at least 6 GB and `make up` again.

### `make replication` fails with `Last_IO_Error: ... Access denied`

`MARIADB_REPL_PASSWORD` changed after the replica was last set up. Re-run
`make replication`: it resets the replication user's password and rebuilds the replica.

### `make replication` fails with `the dump did not set gtid_slave_pos`

The primary has no binary log. Check `engines/mariadb/primary/conf.d/primary.cnf` is
mounted: `make mariadb`, then `SELECT @@log_bin, @@gtid_binlog_pos;`.

### `port is already allocated`

Something on your machine already uses that port (the defaults already avoid 3306 and 5432, which a local MySQL or Postgres usually holds).
Change the port in `.env` and `make up` again.

### Init scripts did not run

They only run when a volume is empty. Changing a file in `engines/*/init` does
nothing to an existing database; `make reset` to start again.

### A backup script stops with `needs at least N GB`

Working as intended: there is not enough free space on the Mac for the backup
and its restore test, and nothing was written. Free space and run it again.
See [INC-001](incidents/INC-001-docker-disk-full.md) for what happens without this check.

### Docker reports `input/output error`, or containers exit with code 128

The Mac's disk filled up under Docker's VM. Free space first, then
`docker desktop restart`. Containers that then fail with `failed to start shim
... file exists` need recreating (their volumes keep the data):
`docker compose up -d --force-recreate <service>`.

### `RESTORE ... VALIDATE` fails with `ORA-19599 ... is corrupt in backup piece`

That backup piece is damaged. RMAN tries an older backup automatically
("failover to previous backup"). Take a new backup straight away and check the
disk the backups are on.
