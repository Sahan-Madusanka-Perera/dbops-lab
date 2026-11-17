"""MongoDB: per-member connections and long operations, plus replica set health.

Members are reached on their published ports with directConnection, because the
replica set advertises Docker-network names (mongo1:27017) the host cannot resolve.
"""

from __future__ import annotations

from datetime import datetime

from pymongo import MongoClient
from pymongo.errors import PyMongoError

from .. import thresholds as t
from ..config import Target
from ..model import Check, Status, grade
from . import CONNECT_TIMEOUT_S, guarded, unreachable

REPLICA_SET = "rs0"


def run(targets: list[Target]) -> list[Check]:
    checks, rs_status = [], None
    for target in targets:
        member_checks, status = _member(target)
        checks += member_checks
        rs_status = rs_status or status
    if targets:
        checks += replica_set_checks(rs_status)
    return checks


def _client(target: Target) -> MongoClient:
    return MongoClient(target.host, target.port, directConnection=True,
                       serverSelectionTimeoutMS=CONNECT_TIMEOUT_S * 1000,
                       connectTimeoutMS=CONNECT_TIMEOUT_S * 1000)


def _member(target: Target) -> tuple[list[Check], dict | None]:
    client = _client(target)
    try:
        admin = client.admin
        try:
            hello = admin.command("hello")
        except PyMongoError as e:
            return [unreachable(target, e)], None

        def reachable():
            role = "primary" if hello.get("isWritablePrimary") else "secondary" if hello.get("secondary") else "other"
            version = admin.command("buildInfo")["version"]
            return Check(target.name, "reachable", Status.OK, detail=f"MongoDB {version}, {role}")

        def connections():
            conns = admin.command("serverStatus")["connections"]
            used, limit = conns["current"], conns["current"] + conns["available"]
            pct = round(100 * used / limit, 2)
            return Check(target.name, "connections", grade(pct, *t.CONNECTIONS_PCT), pct, "%", f"{used} of {limit}")

        def long_running():
            # The filter goes beside currentOp, not inside it: command("currentOp", {...})
            # would send {currentOp: {...}} and silently return every operation.
            ops = admin.command({
                "currentOp": 1,
                "active": True,
                "secs_running": {"$gte": t.LONG_RUNNING_S},
                "ns": {"$not": {"$regex": r"^(local|admin|config)\."}},   # oplog tailing, internals
            })["inprog"]
            detail = "; ".join(f"opid {o.get('opid')} {o.get('op')} {o.get('ns')} {o.get('secs_running')}s"
                               for o in ops[:3])
            return Check(target.name, f"long-running (>{t.LONG_RUNNING_S}s)",
                         grade(len(ops), *t.LONG_RUNNING_COUNT), len(ops), "", detail)

        checks = []
        for name, fn in [("reachable", reachable), ("connections", connections), ("long-running", long_running)]:
            checks += guarded(target, name, fn)

        try:
            rs_status = admin.command("replSetGetStatus")
        except PyMongoError:
            rs_status = None
        return checks, rs_status
    finally:
        client.close()


def replica_set_checks(status: dict | None) -> list[Check]:
    """replSetGetStatus -> member state, primary count and secondary lag. Testable without a server."""
    if status is None:
        return [Check(REPLICA_SET, "replica set", Status.CRIT, detail="no member could report replica set status")]

    members = status["members"]
    primaries = [m for m in members if m["stateStr"] == "PRIMARY"]
    checks = [Check(REPLICA_SET, "primary", Status.OK if len(primaries) == 1 else Status.CRIT,
                    primaries[0]["name"] if len(primaries) == 1 else len(primaries), "",
                    "" if len(primaries) == 1 else "the set needs exactly one primary to take writes")]

    primary_optime: datetime | None = primaries[0]["optimeDate"] if len(primaries) == 1 else None
    for m in members:
        if m["stateStr"] == "PRIMARY":
            continue
        if m.get("health") != 1 or m["stateStr"] not in ("SECONDARY", "ARBITER"):
            checks.append(Check(REPLICA_SET, f"member {m['name']}", Status.CRIT, m["stateStr"], "",
                                m.get("lastHeartbeatMessage", "")))
            continue
        if primary_optime is None:
            checks.append(Check(REPLICA_SET, f"lag {m['name']}", Status.UNKNOWN, detail="no primary to compare with"))
            continue
        lag = max(0, round((primary_optime - m["optimeDate"]).total_seconds()))
        checks.append(Check(REPLICA_SET, f"lag {m['name']}", grade(lag, *t.REPLICATION_LAG_S), lag, "s"))
    return checks
