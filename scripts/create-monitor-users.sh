#!/usr/bin/env bash
#
# create-monitor-users.sh: create (or update) the read-only dbops_monitor
# account that `dbops health` connects as. Safe to re-run: it creates what is
# missing and resets the password to MONITOR_PASSWORD from .env.
#
#   Oracle      FREEPDB1 user with CREATE SESSION + SELECT_CATALOG_ROLE (DBA_* and V$ views)
#   MariaDB     PROCESS (see every session) + SLAVE MONITOR (SHOW REPLICA STATUS)
#   PostgreSQL  member of the built-in pg_monitor role
#   MongoDB     none yet: the replica set runs without auth until M5
#
# Usage:  scripts/create-monitor-users.sh
#
set -euo pipefail

cd "$(dirname "$0")/.."
[[ -f .env ]] || { echo "error: .env not found; run: cp .env.example .env" >&2; exit 2; }
set -a
# shellcheck source=/dev/null  # .env is local and git-ignored
source .env
set +a
: "${MONITOR_PASSWORD:?add MONITOR_PASSWORD to .env (see .env.example)}"

echo "==> oracle"
# OS authentication as SYSDBA inside the container: no password on the command line.
docker exec -i dbops-oracle sqlplus -s / as sysdba <<SQL
WHENEVER SQLERROR EXIT SQL.SQLCODE
SET FEEDBACK OFF
ALTER SESSION SET CONTAINER = FREEPDB1;
CREATE USER IF NOT EXISTS dbops_monitor IDENTIFIED BY "${MONITOR_PASSWORD}";
ALTER USER dbops_monitor IDENTIFIED BY "${MONITOR_PASSWORD}";
GRANT CREATE SESSION, SELECT_CATALOG_ROLE TO dbops_monitor;
PROMPT dbops_monitor ready in FREEPDB1
SQL

maria() {
  docker exec -i -e MYSQL_PWD="$MARIADB_ROOT_PASSWORD" "$1" mariadb -uroot
}
MARIA_SQL="
CREATE USER IF NOT EXISTS 'dbops_monitor'@'%' IDENTIFIED BY '${MONITOR_PASSWORD}';
ALTER USER 'dbops_monitor'@'%' IDENTIFIED BY '${MONITOR_PASSWORD}';
GRANT PROCESS, SLAVE MONITOR ON *.* TO 'dbops_monitor'@'%';
"

echo "==> mariadb-replica"
# Directly on the replica first, kept out of its binlog so it adds no GTIDs of
# its own. A replica rebuilt by setup-replication.sh starts after the point
# where the primary created this user, so it would not get it by replication.
{ echo "SET SESSION sql_log_bin = 0;"; echo "$MARIA_SQL"; } | maria dbops-mariadb-replica
echo "dbops_monitor ready on the replica"

echo "==> mariadb-primary"
# These replicate too; IF NOT EXISTS keeps that from breaking the replica.
echo "$MARIA_SQL" | maria dbops-mariadb-primary
echo "dbops_monitor ready on the primary"

echo "==> postgres"
docker exec -i dbops-postgres psql -q -v ON_ERROR_STOP=1 -U postgres -d shop \
  -v pw="$MONITOR_PASSWORD" <<'SQL'
SET client_min_messages = warning;
SELECT 'CREATE ROLE dbops_monitor LOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dbops_monitor') \gexec
ALTER ROLE dbops_monitor WITH LOGIN PASSWORD :'pw';
GRANT pg_monitor TO dbops_monitor;
GRANT CONNECT ON DATABASE shop TO dbops_monitor;
\echo dbops_monitor ready
SQL

echo "==> mongo: skipped, no auth until M5"
