#!/usr/bin/env bash
#
# mariadb.sh: physical backup from the replica, then a restore test.
#
#   backup   mariadb-backup on the replica, so the primary does no extra work.
#            --slave-info records the primary's GTID the backup is consistent
#            with: the position a new replica built from it would start at.
#            --safe-slave-backup pauses the SQL thread while the backup finishes.
#            --prepare applies the redo log so the copy is a consistent data dir.
#   verify   start a throwaway mariadb:11.4 on a copy of the prepared data dir,
#            then compare every shop table's row count and CHECKSUM TABLE
#            with the replica.
#
# shellcheck source=scripts/backup/lib.sh
source "$(dirname "$0")/lib.sh"

SOURCE=dbops-mariadb-replica
SCRATCH=dbops-verify-mariadb
CNF=/tmp/dbops-backup.cnf

require_free_gb 3 "a physical backup plus a copy to restore from"
start_backup mariadb

# Credentials go in an option file inside the container, not on the command line.
docker exec -i "$SOURCE" sh -c "umask 077 && cat > $CNF" <<CNF
[client]
user=root
password=$MARIADB_ROOT_PASSWORD
CNF
backup_tool() { docker exec "$SOURCE" mariadb-backup --defaults-extra-file="$CNF" "$@"; }

backup_tool --backup --target-dir="/backups/$BACKUP_ID" --slave-info --safe-slave-backup 2> "$BACKUP_DIR/backup.log" \
  || { tail -5 "$BACKUP_DIR/backup.log"; exit 1; }
backup_tool --prepare --target-dir="/backups/$BACKUP_ID" 2> "$BACKUP_DIR/prepare.log" \
  || { tail -5 "$BACKUP_DIR/prepare.log"; exit 1; }
docker exec "$SOURCE" rm -f "$CNF"

GTID=$(grep -o "gtid_slave_pos = '[^']*'" "$BACKUP_DIR/mariadb_backup_slave_info" | cut -d"'" -f2)
finish_backup "$SOURCE" "mariadb-backup --slave-info, prepared" "primary GTID $GTID"

log "restore into $SCRATCH"
# Restore onto a copy: the backup itself stays untouched for the next restore.
RESTORE_DIR="backups/.restore-mariadb"
rm -rf "$RESTORE_DIR" && cp -R "$BACKUP_DIR" "$RESTORE_DIR"
trap 'cleanup_scratch; rm -rf "$REPO_ROOT/$RESTORE_DIR"' EXIT
docker run -d --name "$SCRATCH" -v "$REPO_ROOT/$RESTORE_DIR:/var/lib/mysql" mariadb:11.4 \
  --server-id=99 --skip-slave-start --skip-log-bin >/dev/null

restored_sql() { docker exec -i -e MYSQL_PWD="$MARIADB_ROOT_PASSWORD" "$SCRATCH" mariadb -uroot -N -e "$1"; }
source_sql() { docker exec -i -e MYSQL_PWD="$MARIADB_ROOT_PASSWORD" "$SOURCE" mariadb -uroot -N -e "$1"; }
wait_for 60 restored_sql "SELECT 1" || { VERIFY_OK=0; finish_verify restore "restored server never came up: $(docker logs --tail 3 "$SCRATCH" 2>&1)"; }

# Checksums only mean something if the replica has not moved on since the backup.
NOW_GTID=$(source_sql "SELECT @@gtid_slave_pos")
[[ "$NOW_GTID" == "$GTID" ]] || log "note: replica is now at $NOW_GTID, backup is at $GTID; differences can be real changes"

TABLES=$(source_sql "SELECT table_name FROM information_schema.tables WHERE table_schema = 'shop' ORDER BY 1")
for t in $TABLES; do
  rows_src=$(source_sql "SELECT COUNT(*) FROM shop.$t")
  rows_dst=$(restored_sql "SELECT COUNT(*) FROM shop.$t")
  sum_src=$(source_sql "CHECKSUM TABLE shop.$t" | awk '{print $2}')
  sum_dst=$(restored_sql "CHECKSUM TABLE shop.$t" | awk '{print $2}')
  compare "$t" "$rows_src rows, checksum $sum_src" "$rows_dst rows, checksum $sum_dst"
done

finish_verify "start mariadb:11.4 on the prepared data dir, row count + CHECKSUM TABLE" \
  "$(wc -w <<<"$TABLES" | tr -d ' ') tables, consistent with primary GTID $GTID"
prune mariadb
