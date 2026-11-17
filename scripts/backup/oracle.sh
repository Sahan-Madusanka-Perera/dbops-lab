#!/usr/bin/env bash
#
# oracle.sh: RMAN online backup, then a full restore onto an empty host.
#
#   backup   BACKUP DATABASE PLUS ARCHIVELOG (needs ARCHIVELOG mode:
#            scripts/oracle/enable-archivelog.sh). The control file and spfile
#            autobackup lands in backups/oracle/autobackup/.
#   validate RESTORE DATABASE ... VALIDATE: RMAN reads every block of every
#            piece and checks it, without writing anything.
#   restore  the real test, on a throwaway container with no database in it:
#              1. spfile from the autobackup, as a pfile with a smaller SGA
#              2. control file from the autobackup, mount
#              3. catalog the backup pieces, RESTORE, RECOVER to the backup's last SCN
#              4. OPEN RESETLOGS, open FREEPDB1
#            then every SHOP table's row count and hash is compared with the
#            source AS OF that same SCN (flashback query), so later writes on the
#            source cannot cause a false mismatch.
#
#   --validate-only   skip the restore (about 20 s instead of a few minutes)
#
# Retention is RMAN's: REDUNDANCY 2, then DELETE OBSOLETE, then backup folders
# left with no pieces are removed.
#
# shellcheck source=scripts/backup/lib.sh
source "$(dirname "$0")/lib.sh"

SOURCE=dbops-oracle
SCRATCH=dbops-verify-oracle
VALIDATE_ONLY=0
[[ "${1:-}" == "--validate-only" ]] && VALIDATE_ONLY=1

TAG="DBOPS_$BACKUP_ID"
rman_on() { docker exec -i "$1" rman target / ; }
sysdba_on() { docker exec -i "$1" sqlplus -s / as sysdba; }
sql_value() {   # sql_value <container> <query>: one value, no formatting
  sysdba_on "$1" <<SQL | tr -d '[:space:]'
SET HEADING OFF FEEDBACK OFF PAGES 0
$2
SQL
}

MODE=$(sql_value "$SOURCE" "SELECT log_mode FROM v\$database;")
[[ "$MODE" == ARCHIVELOG ]] || { echo "error: $MODE; run scripts/oracle/enable-archivelog.sh first" >&2; exit 2; }

# Backup pieces are about 0.5 GB; a full restore writes about 3 GB of datafiles
# into the scratch container on top of that.
if [[ $VALIDATE_ONLY == 1 ]]; then require_free_gb 3 "an RMAN backup"
else require_free_gb 8 "an RMAN backup and a full restore"; fi
start_backup oracle
mkdir -p backups/oracle/autobackup   # RMAN will not create it
rman_on "$SOURCE" > "$BACKUP_DIR/backup.log" 2>&1 <<RMAN || { tail -20 "$BACKUP_DIR/backup.log"; exit 1; }
BACKUP DATABASE FORMAT '/backups/$BACKUP_ID/db_%U' TAG '$TAG'
  PLUS ARCHIVELOG FORMAT '/backups/$BACKUP_ID/arc_%U' TAG '$TAG';
DELETE NOPROMPT ARCHIVELOG ALL BACKED UP 1 TIMES TO DEVICE TYPE DISK COMPLETED BEFORE 'SYSDATE - 1';
RMAN

# The newest redo in any backup: the SCN this backup can be recovered to.
UNTIL_SCN=$(sql_value "$SOURCE" "SELECT MAX(next_change#) FROM v\$backup_redolog;")
# Autobackup names are c-<DBID>-<YYYYMMDD>-<seq>, so the newest sorts last.
AUTOBACKUP=$(find backups/oracle/autobackup -name 'c-*' -exec basename {} \; | sort | tail -1)
jq --arg f "$AUTOBACKUP" --arg scn "$UNTIL_SCN" '.autobackup = $f | .until_scn = $scn' <<<'{}' > "$BACKUP_DIR/restore-point.json"
finish_backup "$SOURCE" "RMAN BACKUP DATABASE PLUS ARCHIVELOG, compressed" "recoverable to SCN $UNTIL_SCN"

log "RESTORE ... VALIDATE"
if ! rman_on "$SOURCE" > "$BACKUP_DIR/validate.log" 2>&1 <<RMAN
RESTORE DATABASE FROM TAG '$TAG' VALIDATE;
RESTORE SPFILE FROM '/backups/autobackup/$AUTOBACKUP' VALIDATE;
RESTORE CONTROLFILE FROM '/backups/autobackup/$AUTOBACKUP' VALIDATE;
RMAN
then
  tail -15 "$BACKUP_DIR/validate.log"; VERIFY_OK=0; finish_verify "RMAN RESTORE VALIDATE" "RESTORE VALIDATE failed"
fi

oracle_prune() {
  rman_on "$SOURCE" > "$BACKUP_DIR/prune.log" 2>&1 <<'RMAN' || true
CROSSCHECK BACKUP;
DELETE NOPROMPT OBSOLETE;
DELETE NOPROMPT EXPIRED BACKUP;
RMAN
  local dir
  for dir in backups/oracle/2*/; do
    compgen -G "$dir/db_*" >/dev/null || { log "prune: $dir has no pieces left (RMAN obsolete)"; rm -rf "$dir"; }
  done
}

if [[ $VALIDATE_ONLY == 1 ]]; then
  VERIFY_OK=1
  finish_verify "RMAN RESTORE VALIDATE (database, spfile, control file)" "every backup block read and checked; no restore"
  oracle_prune; exit 0
fi

log "restore onto an empty container ($SCRATCH)"
docker run -d --name "$SCRATCH" --entrypoint sleep -v "$REPO_ROOT/backups/oracle:/backups:ro" \
  gvenzl/oracle-free:23 infinity >/dev/null
docker exec "$SCRATCH" bash -c 'rm -rf /opt/oracle/oradata/* && mkdir -p /opt/oracle/oradata/FREE/FREEPDB1 /opt/oracle/oradata/FREE/pdbseed /opt/oracle/oradata/FRA'

# 1. spfile -> pfile, scaled down to fit next to the running lab
rman_on "$SCRATCH" > "$BACKUP_DIR/restore.log" 2>&1 <<RMAN || true
STARTUP NOMOUNT;
RESTORE SPFILE TO PFILE '/tmp/initFREE.ora' FROM '/backups/autobackup/$AUTOBACKUP';
SHUTDOWN ABORT;
RMAN
docker exec "$SCRATCH" bash -c "test -s /tmp/initFREE.ora && sed -i -E \
  -e 's/^\*\.sga_target=.*/*.sga_target=900M/' -e 's/^\*\.pga_aggregate_target=.*/*.pga_aggregate_target=200M/' /tmp/initFREE.ora" \
  || { tail -20 "$BACKUP_DIR/restore.log"; VERIFY_OK=0; finish_verify restore "could not restore the spfile from $AUTOBACKUP"; }

# 2-4. control file, mount, restore, recover, open
sysdba_on "$SCRATCH" >> "$BACKUP_DIR/restore.log" 2>&1 <<'SQL'
STARTUP NOMOUNT PFILE='/tmp/initFREE.ora'
SQL
if ! rman_on "$SCRATCH" >> "$BACKUP_DIR/restore.log" 2>&1 <<RMAN
RESTORE CONTROLFILE FROM '/backups/autobackup/$AUTOBACKUP';
ALTER DATABASE MOUNT;
CATALOG START WITH '/backups/$BACKUP_ID/' NOPROMPT;
RUN {
  SET UNTIL SCN $UNTIL_SCN;
  RESTORE DATABASE;
  RECOVER DATABASE;
}
ALTER DATABASE OPEN RESETLOGS;
RMAN
then
  grep -E 'RMAN-|ORA-' "$BACKUP_DIR/restore.log" | tail -8
  VERIFY_OK=0; finish_verify "RMAN restore onto an empty host" "restore or recovery failed, see $BACKUP_DIR/restore.log"
fi
sysdba_on "$SCRATCH" >> "$BACKUP_DIR/restore.log" 2>&1 <<'SQL'
ALTER PLUGGABLE DATABASE FREEPDB1 OPEN;
SQL

# Same expression on both sides: COUNT(*) and the sum of ORA_HASH over every column.
TABLES=$(sysdba_on "$SOURCE" <<'SQL'
SET HEADING OFF FEEDBACK OFF PAGES 0
ALTER SESSION SET CONTAINER = FREEPDB1;
SELECT table_name FROM dba_tables WHERE owner = 'SHOP' ORDER BY 1;
SQL
)
fingerprint() {   # fingerprint <container> <table> <as-of clause>
  sysdba_on "$1" <<SQL | sed '/^$/d' | tr -s ' '
SET HEADING OFF FEEDBACK OFF PAGES 0 LINES 400
ALTER SESSION SET CONTAINER = FREEPDB1;
-- After the container switch: switching drops the DBMS_OUTPUT buffer.
SET SERVEROUTPUT ON
DECLARE
  cols VARCHAR2(4000);
  n NUMBER; h NUMBER;
BEGIN
  SELECT LISTAGG(column_name, ' || ''|'' || ') WITHIN GROUP (ORDER BY column_id)
    INTO cols FROM dba_tab_columns WHERE owner = 'SHOP' AND table_name = '$2';
  EXECUTE IMMEDIATE 'SELECT COUNT(*), SUM(ORA_HASH(' || cols || ')) FROM shop.$2 $3' INTO n, h;
  DBMS_OUTPUT.PUT_LINE(n || ' rows, hash ' || h);
END;
/
SQL
}
for t in $TABLES; do
  compare "$t" "$(fingerprint "$SOURCE" "$t" "AS OF SCN $UNTIL_SCN" | grep rows)" \
               "$(fingerprint "$SCRATCH" "$t" "" | grep rows)"
done

finish_verify "RMAN restore + recover onto an empty container, OPEN RESETLOGS, row count + ORA_HASH per table vs source AS OF SCN" \
  "restored to SCN $UNTIL_SCN; $(wc -w <<<"$TABLES" | tr -d ' ') SHOP tables compared"
oracle_prune
