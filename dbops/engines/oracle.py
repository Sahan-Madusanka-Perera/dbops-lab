"""Oracle: sessions, tablespaces, long-running and blocked sessions.

Connects in python-oracledb thin mode, so no Oracle client needs installing.
The monitor user reads V$ and DBA_ views through SELECT_CATALOG_ROLE.
"""

from __future__ import annotations

import oracledb

from .. import thresholds as t
from ..config import Target
from ..model import Check, Status, grade
from . import CONNECT_TIMEOUT_S, guarded, unreachable

TABLESPACES_SQL = """
SELECT m.tablespace_name,
       ROUND(m.used_percent, 1),
       ROUND(m.used_space * ts.block_size / 1048576),
       ROUND(m.tablespace_size * ts.block_size / 1048576)
FROM dba_tablespace_usage_metrics m
JOIN dba_tablespaces ts ON ts.tablespace_name = m.tablespace_name
ORDER BY m.used_percent DESC
"""

LONG_RUNNING_SQL = """
SELECT sid, serial#, username, sql_id, last_call_et
FROM v$session
WHERE type = 'USER' AND status = 'ACTIVE'
  AND last_call_et >= :seconds
  AND sid <> SYS_CONTEXT('USERENV', 'SID')
ORDER BY last_call_et DESC
"""

BLOCKED_SQL = """
SELECT sid, blocking_session, seconds_in_wait, event
FROM v$session
WHERE blocking_session IS NOT NULL
ORDER BY seconds_in_wait DESC
"""


def run(targets: list[Target]) -> list[Check]:
    return [c for target in targets for c in _instance(target)]


def _instance(target: Target) -> list[Check]:
    try:
        conn = oracledb.connect(
            user=target.user, password=target.password,
            dsn=f"{target.host}:{target.port}/{target.database}",
            tcp_connect_timeout=CONNECT_TIMEOUT_S,
        )
    except oracledb.Error as e:
        return [unreachable(target, e)]

    with conn, conn.cursor() as cur:
        def one(sql, **binds):
            cur.execute(sql, binds)
            return cur.fetchone()

        def all_rows(sql, **binds):
            cur.execute(sql, binds)
            return cur.fetchall()

        def reachable():
            version, = one("SELECT version_full FROM v$instance")
            return Check(target.name, "reachable", Status.OK, detail=f"Oracle {version}, {target.database}")

        def sessions():
            used, limit = one("SELECT (SELECT COUNT(*) FROM v$session),"
                              " (SELECT TO_NUMBER(value) FROM v$parameter WHERE name = 'sessions') FROM dual")
            pct = round(100 * used / limit, 1)
            return Check(target.name, "sessions", grade(pct, *t.CONNECTIONS_PCT), pct, "%", f"{used} of {limit}")

        def tablespaces():
            return [
                Check(target.name, f"tablespace {name}", grade(pct, *t.TABLESPACE_PCT), pct, "%",
                      f"{used_mb} MB of {size_mb} MB")
                for name, pct, used_mb, size_mb in all_rows(TABLESPACES_SQL)
            ]

        def long_running():
            rows = all_rows(LONG_RUNNING_SQL, seconds=t.LONG_RUNNING_S)
            detail = "; ".join(f"sid {sid},{serial} {user} sql_id {sql_id} {secs}s"
                               for sid, serial, user, sql_id, secs in rows[:3])
            return Check(target.name, f"long-running (>{t.LONG_RUNNING_S}s)",
                         grade(len(rows), *t.LONG_RUNNING_COUNT), len(rows), "", detail)

        def blocked():
            rows = all_rows(BLOCKED_SQL)
            detail = "; ".join(f"sid {sid} blocked by {blocker} for {secs}s on {event}"
                               for sid, blocker, secs, event in rows[:3])
            return Check(target.name, "blocked sessions", grade(len(rows), *t.BLOCKED_SESSIONS),
                         len(rows), "", detail)

        checks = []
        for name, fn in [("reachable", reachable), ("sessions", sessions), ("tablespaces", tablespaces),
                         ("long-running", long_running), ("blocked sessions", blocked)]:
            checks += guarded(target, name, fn)
        return checks
