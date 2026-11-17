#!/usr/bin/env bash
#
# postgres.sh: logical backup of the shop database, then a restore test.
#
#   backup   pg_dump -Fc (custom format: compressed, restorable table by table)
#            + pg_dumpall --globals-only (roles, which pg_dump leaves out)
#   verify   restore both into a throwaway postgres:17 container, then compare
#            every table's row count and an md5 of its rows with the source
#
# shellcheck source=scripts/backup/lib.sh
source "$(dirname "$0")/lib.sh"

SOURCE=dbops-postgres
DB=shop
SCRATCH=dbops-verify-postgres

require_free_gb 2 "a pg_dump and a scratch restore"
start_backup postgres
src_sql() { docker exec "$SOURCE" psql -U postgres -d "$DB" -tAc "$1"; }

LSN=$(src_sql "SELECT pg_current_wal_lsn()")
docker exec "$SOURCE" pg_dump -U postgres -d "$DB" -Fc -f "/backups/$BACKUP_ID/$DB.dump"
docker exec "$SOURCE" pg_dumpall -U postgres --globals-only -f "/backups/$BACKUP_ID/globals.sql"
finish_backup "$SOURCE" "pg_dump -Fc + pg_dumpall --globals-only" "WAL LSN $LSN at start"

log "restore into $SCRATCH"
docker run -d --name "$SCRATCH" -e POSTGRES_HOST_AUTH_METHOD=trust \
  -v "$REPO_ROOT/$BACKUP_DIR:/backup:ro" postgres:17 >/dev/null
# The image's first-boot server listens on a socket only; TCP answers once init is done.
wait_for 60 docker exec "$SCRATCH" pg_isready -h 127.0.0.1 -U postgres || { VERIFY_OK=0; finish_verify restore "scratch server never came up"; }

scratch_sql() { docker exec "$SCRATCH" psql -U postgres -d "$DB" -tAc "$1"; }
# Globals first (roles the dump's GRANTs refer to); "postgres already exists" is expected.
docker exec "$SCRATCH" psql -q -U postgres -f /backup/globals.sql >/dev/null 2>&1 || true
docker exec "$SCRATCH" createdb -U postgres "$DB"
if ! docker exec "$SCRATCH" pg_restore -U postgres -d "$DB" --exit-on-error /backup/$DB.dump; then
  VERIFY_OK=0; finish_verify "pg_restore" "pg_restore failed"
fi

TABLES=$(src_sql "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")
for t in $TABLES; do
  q="SELECT count(*) || ' rows, md5 ' || left(md5(coalesce(string_agg(t::text, ',' ORDER BY t::text), '')), 12) FROM $t t"
  compare "$t" "$(src_sql "$q")" "$(scratch_sql "$q")"
done

finish_verify "pg_restore into postgres:17, row count + md5 per table" \
  "$(wc -w <<<"$TABLES" | tr -d ' ') tables restored and compared"
prune postgres
