#!/usr/bin/env bash
#
# setup-replication.sh: seed the MariaDB replica from a consistent dump of the
# primary and start GTID replication. Safe to re-run: it rebuilds the replica
# from scratch each time, which is also how a broken replica is repaired (M7).
#
# Steps:
#   1. create the replication user on the primary
#   2. stop and reset the replica, and clear its own binlog
#   3. dump the shop database from the primary with its GTID position
#   4. load the dump into the replica without writing it to the replica's binlog
#   5. point the replica at the primary (MASTER_USE_GTID=slave_pos) and start it
#   6. check both replication threads are running
#
# Usage:  scripts/mariadb/setup-replication.sh
# Needs:  the stack up (make up) and MARIADB_ROOT_PASSWORD, MARIADB_REPL_PASSWORD in .env
#
set -euo pipefail

cd "$(dirname "$0")/../.."
[[ -f .env ]] || { echo "error: .env not found; run: cp .env.example .env" >&2; exit 2; }
set -a; source .env; set +a

PRIMARY=dbops-mariadb-primary
REPLICA=dbops-mariadb-replica
DATABASES=(shop)

# Run SQL as root inside a container. MYSQL_PWD keeps the password out of `ps`.
sql() {
  local container=$1; shift
  docker exec -i -e MYSQL_PWD="$MARIADB_ROOT_PASSWORD" "$container" mariadb -uroot "$@"
}

step() { printf '\n==> %s\n' "$*"; }

step "1/6 replication user on the primary"
sql "$PRIMARY" <<SQL
CREATE USER IF NOT EXISTS 'repl'@'%' IDENTIFIED BY '${MARIADB_REPL_PASSWORD}';
ALTER USER 'repl'@'%' IDENTIFIED BY '${MARIADB_REPL_PASSWORD}';
GRANT REPLICATION SLAVE ON *.* TO 'repl'@'%';
SQL

step "2/6 reset the replica"
# RESET MASTER comes last: the DROPs are binlogged, and any GTID of the replica's
# own would conflict with the primary's under gtid_strict_mode.
sql "$REPLICA" <<SQL
STOP REPLICA;
RESET REPLICA ALL;
$(for db in "${DATABASES[@]}"; do echo "DROP DATABASE IF EXISTS \`$db\`;"; done)
RESET MASTER;
SQL

step "3-4/6 dump ${DATABASES[*]} from the primary into the replica"
# --single-transaction: a consistent InnoDB snapshot without locking writers.
# --master-data=1 --gtid: the dump sets gtid_slave_pos to the snapshot's position,
#   so the replica starts exactly where the dump ends.
{
  echo "SET SESSION sql_log_bin = 0;"
  docker exec -e MYSQL_PWD="$MARIADB_ROOT_PASSWORD" "$PRIMARY" \
    mariadb-dump -uroot --single-transaction --master-data=1 --gtid \
      --routines --triggers --events --databases "${DATABASES[@]}"
} | sql "$REPLICA"

GTID_POS=$(sql "$REPLICA" -N -e "SELECT @@GLOBAL.gtid_slave_pos")
echo "replica gtid_slave_pos: ${GTID_POS:-<empty>}"
[[ -n "$GTID_POS" ]] || { echo "error: the dump did not set gtid_slave_pos" >&2; exit 1; }

step "5/6 point the replica at the primary"
sql "$REPLICA" <<SQL
CHANGE MASTER TO
  MASTER_HOST = 'mariadb-primary',
  MASTER_PORT = 3306,
  MASTER_USER = 'repl',
  MASTER_PASSWORD = '${MARIADB_REPL_PASSWORD}',
  MASTER_USE_GTID = slave_pos,
  MASTER_CONNECT_RETRY = 5;
START REPLICA;
SQL

step "6/6 check replication"
for _ in $(seq 1 15); do
  status=$(sql "$REPLICA" -e "SHOW REPLICA STATUS\G")
  io=$(awk -F': ' '/Slave_IO_Running:/ {print $2}' <<<"$status")
  sqlt=$(awk -F': ' '/Slave_SQL_Running:/ {print $2}' <<<"$status")
  [[ "$io" == "Yes" && "$sqlt" == "Yes" ]] && break
  sleep 1
done

grep -E 'Slave_(IO|SQL)_Running:|Seconds_Behind_Master:|Gtid_IO_Pos:|Last_(IO|SQL)_Error:' <<<"$status"

if [[ "$io" == "Yes" && "$sqlt" == "Yes" ]]; then
  echo; echo "OK: replica is replicating from mariadb-primary"
else
  echo; echo "FAIL: replication threads are not both running (see Last_*_Error above)" >&2
  exit 1
fi
