#!/usr/bin/env bash
#
# Drill: Oracle tablespace full (INC-002).
#
# APP_DATA holds the SHOP schema and is 20 MB with AUTOEXTEND OFF, like a
# tablespace someone sized once and forgot. A batch job loads orders until the
# next extent cannot be allocated and every insert fails with ORA-01653 (table)
# or ORA-01654 (index).
#
#   inject    load orders as SHOP, in 2,000-row commits, until Oracle refuses
#   fix       give the datafile room to grow: AUTOEXTEND ON, capped by MAXSIZE
#   cleanup   delete the drill rows, shrink the segments, put the file back to
#             20 MB with AUTOEXTEND OFF
#
# The drill's rows are marked status = 'DRILL' so cleanup removes only them.
#
# Usage:  drills/oracle-tablespace-full.sh inject|fix|cleanup

# shellcheck source=drills/lib.sh
source "$(dirname "$0")/lib.sh"

DATAFILE=/opt/oracle/oradata/FREE/FREEPDB1/app_data01.dbf

usage_now() {
  ora_sys <<'SQL'
SET HEADING OFF FEEDBACK OFF
SELECT 'APP_DATA ' || ROUND(m.used_percent, 1) || '% used, ' ||
       ROUND(m.used_space * 8192 / 1048576) || ' MB of ' ||
       ROUND(m.tablespace_size * 8192 / 1048576) || ' MB'
FROM dba_tablespace_usage_metrics m WHERE m.tablespace_name = 'APP_DATA';
SQL
}

inject() {
  say "drill start: $(usage_now | xargs)"
  say "batch load: 2,000 orders per commit, as SHOP, until the tablespace is full"
  ora_shop <<'SQL' || true
SET SERVEROUTPUT ON FEEDBACK OFF
DECLARE
  batches PLS_INTEGER := 0;
BEGIN
  LOOP
    INSERT INTO orders (customer_id, status, total_cents, created_at)
    SELECT MOD(LEVEL, 5000) + 1, 'DRILL', 999, SYSTIMESTAMP
    FROM dual CONNECT BY LEVEL <= 2000;
    COMMIT;
    batches := batches + 1;
  END LOOP;
EXCEPTION WHEN OTHERS THEN
  ROLLBACK;
  DBMS_OUTPUT.PUT_LINE(batches || ' batches committed (' || batches * 2000 || ' rows), then:');
  DBMS_OUTPUT.PUT_LINE(SQLERRM);
END;
/
SQL
  say "after the load: $(usage_now | xargs)"
  say "an application insert now:"
  ora_shop <<'SQL' | sed '/^$/d' || true
SET FEEDBACK ON
INSERT INTO orders (customer_id, status, total_cents, created_at) VALUES (42, 'PENDING', 1500, SYSTIMESTAMP);
ROLLBACK;
SQL
}

fix() {
  say "before: $(usage_now | xargs)"
  # Growth is allowed, but only to a ceiling: an unbounded autoextend moves the
  # outage from this tablespace to the filesystem, where it takes everything down.
  ora_sys <<SQL
WHENEVER SQLERROR EXIT SQL.SQLCODE
SET FEEDBACK OFF
ALTER DATABASE DATAFILE '${DATAFILE}' RESIZE 64M;
ALTER DATABASE DATAFILE '${DATAFILE}' AUTOEXTEND ON NEXT 16M MAXSIZE 1G;
PROMPT datafile resized to 64M, autoextend on next 16M up to 1G
SQL
  say "after: $(usage_now | xargs)"
}

cleanup() {
  say "removing the drill's rows and returning APP_DATA to 20 MB, AUTOEXTEND OFF"
  # SHRINK SPACE gives the freed blocks back below the high-water mark, which the
  # datafile RESIZE needs; it runs on the table and, with CASCADE, its indexes.
  ora_sys <<SQL
WHENEVER SQLERROR EXIT SQL.SQLCODE
SET FEEDBACK OFF
DELETE FROM shop.orders WHERE status = 'DRILL';
COMMIT;
ALTER TABLE shop.orders ENABLE ROW MOVEMENT;
ALTER TABLE shop.orders SHRINK SPACE CASCADE;
ALTER TABLE shop.orders DISABLE ROW MOVEMENT;
ALTER DATABASE DATAFILE '${DATAFILE}' AUTOEXTEND OFF;
ALTER DATABASE DATAFILE '${DATAFILE}' RESIZE 20M;
PROMPT cleaned up
SQL
  say "now: $(usage_now | xargs)"
}

dispatch "$@"
