"""PostgreSQL: connections, long-running and blocked queries.

The monitor role is a member of pg_monitor, which lets it see every session's
query text in pg_stat_activity.
"""

from __future__ import annotations

import psycopg

from .. import thresholds as t
from ..config import Target
from ..model import Check, Status, grade
from . import CONNECT_TIMEOUT_S, guarded, unreachable

LONG_RUNNING_SQL = """
SELECT pid, usename, EXTRACT(EPOCH FROM now() - query_start)::int AS secs, LEFT(query, 60)
FROM pg_stat_activity
WHERE state = 'active' AND backend_type = 'client backend'
  AND pid <> pg_backend_pid()
  AND now() - query_start >= make_interval(secs => %s)
ORDER BY query_start
"""

BLOCKED_SQL = """
SELECT pid, pg_blocking_pids(pid), EXTRACT(EPOCH FROM now() - query_start)::int, LEFT(query, 60)
FROM pg_stat_activity
WHERE cardinality(pg_blocking_pids(pid)) > 0
ORDER BY query_start
"""


def run(targets: list[Target]) -> list[Check]:
    return [c for target in targets for c in _instance(target)]


def _instance(target: Target) -> list[Check]:
    try:
        conn = psycopg.connect(host=target.host, port=target.port, dbname=target.database,
                               user=target.user, password=target.password,
                               connect_timeout=CONNECT_TIMEOUT_S, autocommit=True)
    except psycopg.Error as e:
        return [unreachable(target, e)]

    with conn, conn.cursor() as cur:
        def one(sql, *args):
            return cur.execute(sql, args or None).fetchone()

        def reachable():
            return Check(target.name, "reachable", Status.OK,
                         detail=f"PostgreSQL {one('SHOW server_version')[0]}, {target.database}")

        def connections():
            used, limit = one("SELECT count(*) FILTER (WHERE backend_type = 'client backend'),"
                              " current_setting('max_connections')::int FROM pg_stat_activity")
            pct = round(100 * used / limit, 1)
            return Check(target.name, "connections", grade(pct, *t.CONNECTIONS_PCT), pct, "%", f"{used} of {limit}")

        def long_running():
            rows = cur.execute(LONG_RUNNING_SQL, (t.LONG_RUNNING_S,)).fetchall()
            detail = "; ".join(f"pid {pid} {user} {secs}s: {q}" for pid, user, secs, q in rows[:3])
            return Check(target.name, f"long-running (>{t.LONG_RUNNING_S}s)",
                         grade(len(rows), *t.LONG_RUNNING_COUNT), len(rows), "", detail)

        def blocked():
            rows = cur.execute(BLOCKED_SQL).fetchall()
            detail = "; ".join(f"pid {pid} blocked by {blockers} for {secs}s: {q}" for pid, blockers, secs, q in rows[:3])
            return Check(target.name, "blocked sessions", grade(len(rows), *t.BLOCKED_SESSIONS),
                         len(rows), "", detail)

        checks = []
        for name, fn in [("reachable", reachable), ("connections", connections),
                         ("long-running", long_running), ("blocked sessions", blocked)]:
            checks += guarded(target, name, fn)
        return checks
