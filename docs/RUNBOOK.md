# Runbook: DBOps Lab

## 1. What runs where

| Container | Engine | Host port | Connect as |
|---|---|---|---|
| `dbops-oracle` | Oracle Database 23ai Free, PDB `FREEPDB1` | 1521 | `shop` (app), `system` / `sys` (`ORACLE_PASSWORD`) |
| `dbops-mariadb-primary` | MariaDB 11.4, binlog + GTID | 3316 | `shop` (app), `root` |
| `dbops-mariadb-replica` | MariaDB 11.4, `read_only` | 3317 | `root` |
| `dbops-mongo1..3` | MongoDB 8.0, replica set `rs0` | 27017-27019 | no auth yet (added in M5) |
| `dbops-postgres` | PostgreSQL 17 + `pg_stat_statements` | 5442 | `postgres` |

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
make mongo             # mongosh on whichever node is primary
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

### 2.5 Start again from empty

```bash
make reset    # deletes every volume; asks first
make up && make replication
```

## 3. Troubleshooting

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
