# shellcheck shell=bash
# Shared by the drill scripts. Sourced, not run.
#
# Every drill takes one subcommand:
#   inject    break something on purpose, the way it breaks in production
#   fix       the remediation from the postmortem, as commands (what the on-call DBA runs)
#   cleanup   put the lab back exactly as it was, so the drill can run again
#
# Each step prints UTC timestamps, so a run's output is the postmortem's timeline.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ -f .env ]] || { echo "error: .env not found; run: cp .env.example .env" >&2; exit 2; }
set -a
# shellcheck source=/dev/null  # .env is local and git-ignored
source .env
set +a

# A timestamped line: the timeline in the postmortem comes from these.
say() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }

usage() {
  echo "usage: $0 inject|fix|cleanup" >&2
  exit 2
}

# Run the subcommand named on the command line; each drill defines inject, fix and cleanup.
dispatch() {
  case "${1:-}" in
    inject | fix | cleanup) "$1" ;;
    *) usage ;;
  esac
}

# SQL*Plus as SYSDBA inside the container, switched to the shop PDB. Reads SQL on stdin.
ora_sys() {
  { echo "SET FEEDBACK OFF"; echo "ALTER SESSION SET CONTAINER = FREEPDB1;"; cat; } |
    docker exec -i dbops-oracle sqlplus -s -L / as sysdba
}

# SQL*Plus as SHOP (the schema owner, standing in for the application's batch job).
# CONNECT is sent on stdin so the password is not in the process list.
ora_shop() {
  { echo "SET FEEDBACK OFF"; echo "CONNECT shop/\"${ORACLE_APP_PASSWORD}\"@FREEPDB1"; cat; } |
    docker exec -i dbops-oracle sqlplus -s -L /nolog
}

# The MariaDB client as root on one container (primary or replica). Reads SQL on stdin.
maria_root() {
  docker exec -i -e MYSQL_PWD="$MARIADB_ROOT_PASSWORD" "$1" mariadb -uroot --table shop
}

psql_shop() {
  docker exec -i dbops-postgres psql -U postgres -d shop -v ON_ERROR_STOP=1 -X -q "$@"
}

# mongosh as dbops_admin on one member, running a script. The password comes from the
# environment and the login happens inside the script, so it never shows in `ps`.
MONGO_LOGIN='db.getSiblingDB("admin").auth("dbops_admin", process.env.MONGO_ADMIN_PASSWORD);'
mongo_eval() {
  docker exec -e MONGO_ADMIN_PASSWORD "dbops-$1" mongosh --quiet --eval "$MONGO_LOGIN $2"
}
