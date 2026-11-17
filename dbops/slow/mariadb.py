"""MariaDB: the slow query log for the ranking, EXPLAIN FORMAT=JSON for the plan.

The log is read from the primary's data directory, as a DBA on the server would.
Statements are grouped by their text with literals replaced (a small
pt-query-digest), and the slowest real instance of each is explained.
EXPLAIN needs read access to the tables, so this connects as root.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field

import pymysql

from . import Scan, Statement, alias_map, explain_scan, finish, normalise_sql, parse_predicates, parse_sort_keys

CONTAINER = "dbops-mariadb-primary"
SLOW_LOG = "/var/lib/mysql/slow.log"
IGNORED_USERS = {"root", "dbops_monitor", "repl"}


@dataclass
class Entry:
    user: str = ""
    schema: str = ""
    query_time: float = 0.0
    rows_sent: int = 0
    rows_examined: int = 0
    sql: list[str] = field(default_factory=list)


_HEADER = {
    "user": re.compile(r"^# User@Host: (\w+)\["),
    "schema": re.compile(r"Schema: (\w*)"),
    "times": re.compile(r"Query_time: ([\d.]+)\s+Lock_time: [\d.]+\s+Rows_sent: (\d+)\s+Rows_examined: (\d+)"),
}


_BANNER = re.compile(r"^\S+, Version: .* started with:$")


def parse_slow_log(text: str) -> list[Entry]:
    entries, cur = [], None
    for line in text.splitlines():
        # A server (re)start writes a three-line banner into the log: it ends any entry.
        if _BANNER.match(line):
            if cur and cur.sql:
                entries.append(cur)
            cur = None
            continue
        if line.startswith(("# Time:", "# User@Host:")):
            if cur and cur.sql:
                entries.append(cur)
                cur = None
            if line.startswith("# User@Host:"):
                cur = Entry()
                if m := _HEADER["user"].search(line):
                    cur.user = m.group(1)
            continue
        if cur is None:
            continue
        if line.startswith("#"):
            if m := _HEADER["schema"].search(line):
                cur.schema = m.group(1)
            if m := _HEADER["times"].search(line):
                cur.query_time, cur.rows_sent, cur.rows_examined = float(m.group(1)), int(m.group(2)), int(m.group(3))
        elif not line.lower().startswith(("set timestamp", "use ")) and line.strip():
            cur.sql.append(line.strip())
    if cur and cur.sql:
        entries.append(cur)
    return entries


def _connect(env: dict, schema: str = "shop") -> pymysql.Connection:
    return pymysql.connect(host=env.get("DBOPS_HOST", "127.0.0.1"), port=int(env.get("MARIADB_PRIMARY_PORT", 3316)),
                           user="root", password=env["MARIADB_ROOT_PASSWORD"], database=schema, connect_timeout=5)


def collect(env: dict, top: int) -> tuple[list[Statement], str]:
    log = subprocess.run(["docker", "exec", CONTAINER, "tail", "-c", "50000000", SLOW_LOG],
                         capture_output=True, text=True, check=True, timeout=30).stdout
    groups: dict[str, dict] = {}
    for e in parse_slow_log(log):
        if e.user in IGNORED_USERS or not e.schema:
            continue
        sql = " ".join(e.sql)
        g = groups.setdefault(normalise_sql(sql), {"calls": 0, "time": 0.0, "examined": 0, "sent": 0,
                                                    "sample": sql, "sample_time": -1.0, "schema": e.schema})
        g["calls"] += 1
        g["time"] += e.query_time
        g["examined"] += e.rows_examined
        g["sent"] += e.rows_sent
        if e.query_time > g["sample_time"]:
            g["sample"], g["sample_time"] = sql, e.query_time

    ranked = sorted(groups.items(), key=lambda kv: kv[1]["time"], reverse=True)[:top]
    statements = []
    for query, g in ranked:
        st = Statement("mariadb", query, g["calls"], g["time"] * 1000,
                       work=f"{g['examined'] / g['calls']:,.0f} rows read for {g['sent'] / g['calls']:,.0f} returned")
        try:
            with _connect(env, g["schema"]) as conn, conn.cursor() as cur:
                cur.execute("EXPLAIN FORMAT=JSON " + g["sample"])
                plan = json.loads(cur.fetchone()[0])
            st.plan, st.scans, estimates = read_plan(plan, g["sample"])
            for scan in st.scans:
                st.notes.append(explain_scan(scan))
            examined = g["examined"] / g["calls"]
            for table, est in estimates.items():
                # The optimizer chose this plan believing the table is tiny: statistics are stale.
                if est * 100 < examined:
                    st.notes.append(f"optimizer estimates {est:,} rows in {table} but the statement reads {examined:,.0f}: "
                                    f"statistics are stale")
                    st.fixes.append(f"ANALYZE TABLE {table};")
        except pymysql.Error as e:
            st.notes.append(f"could not EXPLAIN: {e.args[-1] if e.args else e}")
        statements.append(finish(st))
    return statements, f"slow log on {CONTAINER} (full scans of 1,000+ rows, or over 0.5 s)"


def read_plan(plan: dict, sql: str) -> tuple[str, list[Scan], dict[str, int]]:
    """EXPLAIN FORMAT=JSON -> (summary, scans, {table: estimated rows} for full scans)."""
    aliases = alias_map(sql)
    summary, scans, estimates = [], [], {}

    def walk(node, sort_key: str) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, sort_key)
            return
        if not isinstance(node, dict):
            return
        if "filesort" in node:
            summary.append("filesort")
            sort_key = node["filesort"].get("sort_key", sort_key)
        if "table_name" in node and "access_type" in node:
            alias = node["table_name"]
            table = aliases.get(alias.lower(), alias.lower())
            access = node["access_type"]
            summary.append(f"{access} on {table}")
            eq, rng = parse_predicates(node.get("attached_condition", ""))
            own_sort = [k for k in parse_sort_keys(sort_key or "")
                        if _sort_alias(sort_key, k[0]) in ("", alias.lower())]
            if access == "ALL":
                scans.append(Scan(table, "full scan", eq, own_sort, rng))
                estimates[table] = int(node.get("rows", 0))
            elif access == "ref" and own_sort:
                keyed = [c.lower() for c in node.get("used_key_parts", [])]
                scans.append(Scan(table, "sort after index lookup", keyed + [c for c in eq if c not in keyed],
                                  own_sort, rng))
        for value in node.values():
            walk(value, sort_key)

    walk(plan, "")
    return " > ".join(dict.fromkeys(summary)), scans, estimates


def _sort_alias(sort_key: str, column: str) -> str:
    m = re.search(r"(\w+)\.`?" + re.escape(column) + r"\b", sort_key, re.IGNORECASE)
    return m.group(1).lower() if m else ""


def reset(env: dict) -> str:
    subprocess.run(["docker", "exec", CONTAINER, "sh", "-c", f": > {SLOW_LOG}"], check=True, timeout=30)
    return f"{SLOW_LOG} truncated"
