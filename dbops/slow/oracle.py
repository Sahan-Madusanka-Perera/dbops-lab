"""Oracle: V$SQL for the ranking, V$SQL_PLAN for the plan each cursor actually ran.

Reading the plan Oracle used (rather than asking EXPLAIN PLAN what it would do)
needs only SELECT_CATALOG_ROLE, so this runs as the read-only monitor account.
"""

from __future__ import annotations

import subprocess

import oracledb

from ..config import targets
from . import Scan, Statement, explain_scan, finish, parse_order_by, parse_predicates

TOP_SQL = """
SELECT sql_id, plan_hash_value, SUM(executions), SUM(elapsed_time) / 1000,
       SUM(buffer_gets), SUM(rows_processed), MAX(sql_text)
FROM v$sql
WHERE parsing_schema_name = 'SHOP' AND command_type = 3 AND executions > 0
GROUP BY sql_id, plan_hash_value
ORDER BY 4 DESC
FETCH FIRST :top ROWS ONLY
"""

PLAN_SQL = """
SELECT operation, options, object_owner, object_name, filter_predicates
FROM v$sql_plan
WHERE sql_id = :sql_id AND plan_hash_value = :plan
  AND child_number = (SELECT MIN(child_number) FROM v$sql_plan WHERE sql_id = :sql_id AND plan_hash_value = :plan)
ORDER BY id
"""

TABLES_SQL = "SELECT table_name, num_rows, last_analyzed, stale_stats FROM dba_tab_statistics WHERE owner = 'SHOP' AND object_type = 'TABLE'"


def collect(env: dict, top: int) -> tuple[list[Statement], str]:
    t = next(x for x in targets(env) if x.engine == "oracle")
    with oracledb.connect(user=t.user, password=t.password, dsn=f"{t.host}:{t.port}/{t.database}") as conn:
        cur = conn.cursor()
        tables = {name.lower(): (rows, analyzed, stale) for name, rows, analyzed, stale in cur.execute(TABLES_SQL)}
        statements = []
        for sql_id, plan_hash, execs, ms, gets, rows, text in cur.execute(TOP_SQL, top=top).fetchall():
            st = Statement("oracle", " ".join(text.split()), int(execs), float(ms),
                           work=f"{gets / execs:,.0f} buffer gets for {rows / execs:,.0f} rows")
            plan = cur.execute(PLAN_SQL, sql_id=sql_id, plan=plan_hash).fetchall()
            st.plan = " > ".join(f"{op} {opt or ''} {name or ''}".replace("  ", " ").strip()
                                 for op, opt, _, name, _ in plan[1:8] if op != "STATISTICS COLLECTOR")
            sorts = any(op.startswith("SORT") and (opt or "").startswith("ORDER BY") for op, opt, *_ in plan)
            for op, opt, owner, name, filt in plan:
                if op == "TABLE ACCESS" and "FULL" in (opt or "") and owner == "SHOP":
                    table = name.lower()
                    eq, rng = parse_predicates(filt or "")
                    sort = parse_order_by(text) if sorts else []
                    n_rows, analyzed, stale = tables.get(table, (None, None, None))
                    st.scans.append(Scan(table, "full scan", eq, sort, rng, table_rows=n_rows))
                    st.notes.append(explain_scan(st.scans[-1]))
                    if analyzed is None or stale == "YES":
                        st.notes.append(f"{table} has {'no' if analyzed is None else 'stale'} optimizer statistics")
                        st.fixes.append(f"EXEC DBMS_STATS.GATHER_TABLE_STATS('SHOP', '{table.upper()}');")
            statements.append(finish(st))
        # Index DDL for SHOP's tables needs the schema prefix when run as a DBA.
        for st in statements:
            st.fixes = [f.replace(" ON ", " ON shop.", 1).replace("INDEX ix_", "INDEX shop.ix_", 1)
                        if f.startswith("CREATE INDEX") else f for f in st.fixes]
    return statements, "V$SQL for SHOP since the cursors were loaded"


def reset(env: dict) -> str:
    sql = "ALTER SESSION SET CONTAINER = FREEPDB1;\nALTER SYSTEM FLUSH SHARED_POOL;\n"
    subprocess.run(["docker", "exec", "-i", "dbops-oracle", "sqlplus", "-s", "/", "as", "sysdba"],
                   input=sql, capture_output=True, text=True, check=True, timeout=60)
    return "shared pool flushed in FREEPDB1"
