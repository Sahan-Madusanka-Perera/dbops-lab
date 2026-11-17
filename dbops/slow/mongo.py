"""MongoDB: the database profiler on every member, grouped by query shape.

The profiler (system.profile) is on for the shop database: see
engines/mongo/init-replica-set.js. Each entry already carries its plan summary
(COLLSCAN, IXSCAN ...), documents examined and returned, and whether it sorted
in memory, so no separate explain is needed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pymongo import MongoClient

from ..config import REPO_ROOT, targets
from . import Scan, Statement, explain_scan, finish

DB = "shop"
# Keep in step with engines/mongo/init-replica-set.js
PROFILE_FILTER = {"$or": [{"millis": {"$gte": 500}}, {"docsExamined": {"$gte": 1000}}]}
FIELDS = {"op": 1, "ns": 1, "command": 1, "planSummary": 1, "docsExamined": 1, "nreturned": 1,
          "millis": 1, "hasSortStage": 1, "queryShapeHash": 1}
# system.profile cannot be dropped on a secondary (a drop is a write), so a reset
# records when it happened and collect reads only the entries after it.
SINCE_FILE = REPO_ROOT / ".dbops" / "mongo-profile-since"


def _since() -> datetime:
    try:
        return datetime.fromisoformat(SINCE_FILE.read_text().strip())
    except (OSError, ValueError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _members(env: dict):
    for t in targets(env):
        if t.engine == "mongo":
            yield t.name, MongoClient(t.host, t.port, directConnection=True, serverSelectionTimeoutMS=5000)


def filter_of(command: dict) -> dict:
    if "filter" in command:
        return command["filter"]
    if "pipeline" in command and command["pipeline"] and "$match" in command["pipeline"][0]:
        return command["pipeline"][0]["$match"]
    return command.get("query", command.get("q", {})) or {}


def split_filter(flt: dict) -> tuple[list[str], list[str]]:
    eq, rng = [], []
    for key, value in flt.items():
        if key.startswith("$"):
            continue
        if isinstance(value, dict) and any(op in value for op in ("$gt", "$gte", "$lt", "$lte")):
            rng.append(key)
        else:
            eq.append(key)
    return eq, rng


def shape(op: str, command: dict) -> str:
    """Readable query shape with values hidden: find orders {customerId: ?} sort {createdAt: -1} limit 20"""
    coll = command.get("find") or command.get("aggregate") or command.get("count") or ""
    eq, rng = split_filter(filter_of(command))
    parts = [("aggregate" if "pipeline" in command else op), coll,
             "{" + ", ".join([f"{k}: ?" for k in eq] + [f"{k}: <range>" for k in rng]) + "}"]
    if command.get("sort"):
        parts.append("sort {" + ", ".join(f"{k}: {v}" for k, v in command["sort"].items()) + "}")
    if "pipeline" in command:
        parts.append("> " + " > ".join(next(iter(stage)) for stage in command["pipeline"][1:]))
    if command.get("limit"):
        parts.append(f"limit {command['limit']}")
    return " ".join(p for p in parts if p)


def collect(env: dict, top: int) -> tuple[list[Statement], str]:
    groups: dict[str, dict] = {}
    for _, client in _members(env):
        with client:
            query = {"op": {"$in": ["query", "command", "getmore"]}, "ts": {"$gte": _since()}}
            for doc in client[DB].system.profile.find(query, FIELDS):
                cmd = doc.get("command", {})
                if doc["op"] == "getmore" or not filter_of(cmd) and "pipeline" not in cmd:
                    continue
                key = doc.get("queryShapeHash") or shape(doc["op"], cmd)
                g = groups.setdefault(key, {"calls": 0, "ms": 0, "examined": 0, "returned": 0, "plans": set(),
                                            "sorted": False, "command": cmd, "op": doc["op"]})
                g["calls"] += 1
                g["ms"] += doc.get("millis", 0)
                g["examined"] += doc.get("docsExamined", 0)
                g["returned"] += doc.get("nreturned", 0)
                g["plans"].add(doc.get("planSummary", ""))
                g["sorted"] |= bool(doc.get("hasSortStage"))

    ranked = sorted(groups.values(), key=lambda g: g["ms"], reverse=True)[:top]
    statements = []
    for g in ranked:
        cmd = g["command"]
        coll = cmd.get("find") or cmd.get("aggregate") or cmd.get("count") or "?"
        st = Statement("mongo", shape(g["op"], cmd), g["calls"], float(g["ms"]),
                       plan=", ".join(sorted(p for p in g["plans"] if p)),
                       work=f"{g['examined'] / g['calls']:,.0f} docs read for {g['returned'] / g['calls']:,.0f} returned")
        if any("COLLSCAN" in p for p in g["plans"]) or g["sorted"]:
            eq, rng = split_filter(filter_of(cmd))
            sort = [(k, int(v)) for k, v in (cmd.get("sort") or {}).items()]
            reason = "full scan" if any("COLLSCAN" in p for p in g["plans"]) else "in-memory sort"
            st.scans.append(Scan(coll, reason, eq, sort, rng))
            st.notes.append(explain_scan(st.scans[-1]))
        statements.append(finish(st))
    return statements, "system.profile on mongo1-3 since the last reset (1,000+ documents read, or over 500 ms)"


def reset(env: dict) -> str:
    SINCE_FILE.parent.mkdir(exist_ok=True)
    SINCE_FILE.write_text(datetime.now(timezone.utc).isoformat())
    return "system.profile entries before now will be ignored"
