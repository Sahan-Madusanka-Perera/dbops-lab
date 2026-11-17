#!/usr/bin/env bash
#
# create-accounts.sh: create (or update) the approved service accounts in every
# engine and apply the access baseline. The approved list is
# policy/access-policy.toml; `dbops access` checks the result.
#
# Safe to re-run: it creates what is missing and resets each password to .env,
# so it is also how a password is rotated.
#
#   dbops_monitor   read-only monitoring (dbops health, dbops slow on MongoDB)
#                   Oracle      CREATE SESSION + SELECT_CATALOG_ROLE
#                   MariaDB     PROCESS + SLAVE MONITOR
#                   PostgreSQL  pg_monitor
#                   MongoDB     clusterMonitor + find on shop.system.profile
#   shop_app        the application: read and write the shop data, no DDL
#                   Oracle      SELECT/INSERT/UPDATE/DELETE ANY TABLE ON SCHEMA shop (23ai schema privileges)
#                   MariaDB     SELECT, INSERT, UPDATE, DELETE ON shop.*
#                   PostgreSQL  the same on every table in public, now and later
#                   MongoDB     custom role shopApp: find/insert/update/remove on shop
#   baseline        Oracle: lock PDBADMIN (an admin account nobody uses),
#                   audit failed logins (ORA_LOGON_FAILURES)
#                   MongoDB: dbops_admin, created by engines/mongo/init-replica-set.js
#
# Usage:  scripts/create-accounts.sh
#
set -euo pipefail

cd "$(dirname "$0")/.."
[[ -f .env ]] || { echo "error: .env not found; run: cp .env.example .env" >&2; exit 2; }
set -a
# shellcheck source=/dev/null  # .env is local and git-ignored
source .env
set +a
: "${MONITOR_PASSWORD:?add MONITOR_PASSWORD to .env (see .env.example)}"
: "${APP_PASSWORD:?add APP_PASSWORD to .env (see .env.example)}"
: "${MONGO_ADMIN_PASSWORD:?add MONGO_ADMIN_PASSWORD to .env (see .env.example)}"

echo "==> oracle"
# OS authentication as SYSDBA inside the container: no password on the command line.
docker exec -i dbops-oracle sqlplus -s / as sysdba <<SQL
WHENEVER SQLERROR EXIT SQL.SQLCODE
SET FEEDBACK OFF
ALTER SESSION SET CONTAINER = FREEPDB1;
CREATE USER IF NOT EXISTS dbops_monitor IDENTIFIED BY "${MONITOR_PASSWORD}";
ALTER USER dbops_monitor IDENTIFIED BY "${MONITOR_PASSWORD}";
GRANT CREATE SESSION, SELECT_CATALOG_ROLE TO dbops_monitor;
PROMPT dbops_monitor ready
CREATE USER IF NOT EXISTS shop_app IDENTIFIED BY "${APP_PASSWORD}";
ALTER USER shop_app IDENTIFIED BY "${APP_PASSWORD}";
GRANT CREATE SESSION TO shop_app;
GRANT SELECT ANY TABLE, INSERT ANY TABLE, UPDATE ANY TABLE, DELETE ANY TABLE ON SCHEMA shop TO shop_app;
PROMPT shop_app ready: DML on schema SHOP
ALTER USER pdbadmin ACCOUNT LOCK;
PROMPT PDBADMIN locked
AUDIT POLICY ORA_LOGON_FAILURES;
PROMPT failed logins audited
SQL

maria() {
  docker exec -i -e MYSQL_PWD="$MARIADB_ROOT_PASSWORD" "$1" mariadb -uroot
}
MARIA_SQL="
CREATE USER IF NOT EXISTS 'dbops_monitor'@'%' IDENTIFIED BY '${MONITOR_PASSWORD}';
ALTER USER 'dbops_monitor'@'%' IDENTIFIED BY '${MONITOR_PASSWORD}';
GRANT PROCESS, SLAVE MONITOR ON *.* TO 'dbops_monitor'@'%';
CREATE USER IF NOT EXISTS 'shop_app'@'%' IDENTIFIED BY '${APP_PASSWORD}';
ALTER USER 'shop_app'@'%' IDENTIFIED BY '${APP_PASSWORD}';
GRANT SELECT, INSERT, UPDATE, DELETE ON shop.* TO 'shop_app'@'%';
"

echo "==> mariadb-replica"
# Directly on the replica first, kept out of its binlog so it adds no GTIDs of
# its own. A replica rebuilt by setup-replication.sh starts after the point
# where the primary created these users, so it would not get them by replication.
{ echo "SET SESSION sql_log_bin = 0;"; echo "$MARIA_SQL"; } | maria dbops-mariadb-replica
echo "dbops_monitor, shop_app ready on the replica"

echo "==> mariadb-primary"
# These replicate too; IF NOT EXISTS keeps that from breaking the replica.
echo "$MARIA_SQL" | maria dbops-mariadb-primary
echo "dbops_monitor, shop_app ready on the primary"

echo "==> postgres"
docker exec -i dbops-postgres psql -q -v ON_ERROR_STOP=1 -U postgres -d shop \
  -v monitor_pw="$MONITOR_PASSWORD" -v app_pw="$APP_PASSWORD" <<'SQL'
SET client_min_messages = warning;
SELECT 'CREATE ROLE dbops_monitor LOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dbops_monitor') \gexec
ALTER ROLE dbops_monitor WITH LOGIN PASSWORD :'monitor_pw';
GRANT pg_monitor TO dbops_monitor;
GRANT CONNECT ON DATABASE shop TO dbops_monitor;
\echo dbops_monitor ready
SELECT 'CREATE ROLE shop_app LOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'shop_app') \gexec
ALTER ROLE shop_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'app_pw';
GRANT CONNECT ON DATABASE shop TO shop_app;
GRANT USAGE ON SCHEMA public TO shop_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO shop_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO shop_app;
-- tables postgres creates later get the same grants
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO shop_app;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT USAGE ON SEQUENCES TO shop_app;
\echo shop_app ready: DML on public
SQL

echo "==> mongo"
docker exec -e MONGO_ADMIN_PASSWORD -e APP_PASSWORD -e MONITOR_PASSWORD dbops-mongo1 \
  mongosh --quiet --file /scripts/init-replica-set.js
