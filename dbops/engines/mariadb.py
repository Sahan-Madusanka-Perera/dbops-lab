"""MariaDB: connections, long-running statements and replication.

The primary is checked for a connected replica; the replica for both replication
threads, lag and read_only.
"""

from __future__ import annotations

import pymysql
import pymysql.cursors

from .. import thresholds as t
from ..config import Target
from ..model import Check, Status, grade
from . import CONNECT_TIMEOUT_S, guarded, unreachable

# Server threads that are always "running" and are not user statements.
LONG_RUNNING_SQL = """
SELECT ID, USER, DB, TIME, LEFT(INFO, 60) AS INFO
FROM information_schema.PROCESSLIST
WHERE COMMAND NOT IN ('Sleep', 'Daemon', 'Slave_IO', 'Slave_SQL', 'Slave_worker')
  AND COMMAND NOT LIKE 'Binlog Dump%%'
  AND USER NOT IN ('system user', 'event_scheduler')
  AND ID <> CONNECTION_ID()
  AND TIME >= %s
ORDER BY TIME DESC
"""


def run(targets: list[Target]) -> list[Check]:
    return [c for target in targets for c in _instance(target)]


def _instance(target: Target) -> list[Check]:
    try:
        conn = pymysql.connect(
            host=target.host, port=target.port, user=target.user, password=target.password,
            connect_timeout=CONNECT_TIMEOUT_S, read_timeout=10,
            cursorclass=pymysql.cursors.DictCursor,
        )
    except pymysql.Error as e:
        return [unreachable(target, e)]

    with conn, conn.cursor() as cur:
        def one(sql, *args):
            cur.execute(sql, args or None)
            return cur.fetchone()

        def reachable():
            return Check(target.name, "reachable", Status.OK,
                         detail=f"MariaDB {one('SELECT VERSION() AS v')['v']}" + (f", {target.role}" if target.role else ""))

        def connections():
            used = int(one("SHOW GLOBAL STATUS LIKE 'Threads_connected'")["Value"])
            limit = int(one("SELECT @@max_connections AS m")["m"])
            pct = round(100 * used / limit, 1)
            return Check(target.name, "connections", grade(pct, *t.CONNECTIONS_PCT), pct, "%", f"{used} of {limit}")

        def long_running():
            cur.execute(LONG_RUNNING_SQL, (t.LONG_RUNNING_S,))
            rows = cur.fetchall()
            detail = "; ".join(f"id {r['ID']} {r['USER']} {r['TIME']}s: {r['INFO'] or ''}".strip() for r in rows[:3])
            return Check(target.name, f"long-running (>{t.LONG_RUNNING_S}s)",
                         grade(len(rows), *t.LONG_RUNNING_COUNT), len(rows), "", detail)

        def replicas_connected():
            n = int(one("SELECT COUNT(*) AS n FROM information_schema.PROCESSLIST"
                        " WHERE COMMAND LIKE 'Binlog Dump%'")["n"])
            return Check(target.name, "replicas connected", Status.OK if n else Status.WARN, n, "",
                         "" if n else "no replica is reading the binlog")

        def replication():
            return replication_checks(target, one("SHOW REPLICA STATUS"))

        def errant():
            row = one("SELECT @@gtid_binlog_pos AS pos, @@server_id AS id")
            return errant_gtid_check(target, row["pos"], int(row["id"]))

        def read_only():
            ro = int(one("SELECT @@read_only AS ro")["ro"])
            return Check(target.name, "read_only", Status.OK if ro else Status.CRIT, "ON" if ro else "OFF", "",
                         "" if ro else "a writable replica can diverge from the primary")

        plan = [("reachable", reachable), ("connections", connections), ("long-running", long_running)]
        if target.role == "primary":
            plan.append(("replicas connected", replicas_connected))
        if target.role == "replica":
            plan += [("replication", replication), ("errant GTIDs", errant), ("read_only", read_only)]

        checks = []
        for name, fn in plan:
            checks += guarded(target, name, fn)
        return checks


def replication_checks(target: Target, status: dict | None) -> list[Check]:
    """SHOW REPLICA STATUS row -> thread and lag checks. Separate so it can be tested without a server."""
    if not status:
        return [Check(target.name, "replication", Status.CRIT, detail="not configured as a replica")]

    io, sql = status["Slave_IO_Running"], status["Slave_SQL_Running"]
    if io != "Yes" or sql != "Yes":
        error = status.get("Last_SQL_Error") or status.get("Last_IO_Error") or "stopped"
        return [Check(target.name, "replication", Status.CRIT, f"IO={io} SQL={sql}", "", error)]

    lag = status["Seconds_Behind_Master"]
    if lag is None:
        return [Check(target.name, "replication lag", Status.CRIT, detail="lag unknown")]
    return [
        Check(target.name, "replication", Status.OK, "IO=Yes SQL=Yes", "", f"GTID {status.get('Gtid_IO_Pos', '')}"),
        Check(target.name, "replication lag", grade(lag, *t.REPLICATION_LAG_S), lag, "s"),
    ]


def errant_gtids(binlog_pos: str, server_id: int) -> list[str]:
    """GTIDs a replica wrote itself: domain-server-seq entries carrying its own server_id.

    A replica's binlog should only hold what it replicated. An entry under its own
    server_id is a write made on the replica: its data has diverged, and in strict
    GTID mode replication stops at the primary's next write in that domain (INC-003).
    """
    return [g for g in (binlog_pos or "").split(",")
            if g.strip() and g.strip().split("-")[1] == str(server_id)]


def errant_gtid_check(target: Target, binlog_pos: str, server_id: int) -> Check:
    found = [g.strip() for g in errant_gtids(binlog_pos, server_id)]
    if not found:
        return Check(target.name, "errant GTIDs", Status.OK, 0)
    return Check(target.name, "errant GTIDs", Status.CRIT, len(found), "",
                 f"written on the replica, not replicated: {', '.join(found)}")
