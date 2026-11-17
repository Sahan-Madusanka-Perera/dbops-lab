#!/usr/bin/env bash
#
# enable-archivelog.sh: put Oracle in ARCHIVELOG mode with a fast recovery area,
# and set the RMAN defaults the backups rely on. Safe to re-run.
#
# Why: in NOARCHIVELOG mode RMAN can only back up a database that is shut down,
# and nothing can be recovered past the last backup. ARCHIVELOG keeps every redo
# log, so a backup can be taken while the database is open and recovered to any
# point after it.
#
# Switching modes needs a restart (SHUTDOWN IMMEDIATE, STARTUP MOUNT): about 30 s
# of downtime, once.
#
set -euo pipefail

CONTAINER=dbops-oracle
FRA=/opt/oracle/oradata/FRA

sysdba() { docker exec -i "$CONTAINER" sqlplus -s / as sysdba; }

mode=$(sysdba <<'SQL' | tr -d '[:space:]'
SET HEADING OFF FEEDBACK OFF
SELECT log_mode FROM v$database;
SQL
)
echo "log mode: $mode"

echo "==> fast recovery area at $FRA (archived redo logs live here)"
docker exec "$CONTAINER" mkdir -p "$FRA"
sysdba <<SQL
WHENEVER SQLERROR EXIT SQL.SQLCODE
SET FEEDBACK OFF
ALTER SYSTEM SET db_recovery_file_dest_size = 10G SCOPE = BOTH;
ALTER SYSTEM SET db_recovery_file_dest = '$FRA' SCOPE = BOTH;
SQL

if [[ "$mode" != "ARCHIVELOG" ]]; then
  echo "==> restart into ARCHIVELOG mode"
  sysdba <<'SQL'
WHENEVER SQLERROR EXIT SQL.SQLCODE
SHUTDOWN IMMEDIATE
STARTUP MOUNT
ALTER DATABASE ARCHIVELOG;
ALTER DATABASE OPEN;
ALTER PLUGGABLE DATABASE ALL OPEN;
ALTER PLUGGABLE DATABASE ALL SAVE STATE;
SQL
fi

echo "==> RMAN defaults"
# Control file autobackup: every backup also saves the control file and spfile,
# which a restore onto an empty host needs first.
docker exec -i "$CONTAINER" rman target / <<'RMAN'
CONFIGURE CONTROLFILE AUTOBACKUP ON;
CONFIGURE CONTROLFILE AUTOBACKUP FORMAT FOR DEVICE TYPE DISK TO '/backups/autobackup/%F';
CONFIGURE RETENTION POLICY TO REDUNDANCY 2;
CONFIGURE BACKUP OPTIMIZATION ON;
CONFIGURE DEVICE TYPE DISK BACKUP TYPE TO COMPRESSED BACKUPSET;
RMAN

sysdba <<'SQL'
SET HEADING OFF FEEDBACK OFF
SELECT 'log mode: ' || log_mode FROM v$database;
SELECT 'FREEPDB1: ' || open_mode FROM v$pdbs WHERE name = 'FREEPDB1';
SQL
