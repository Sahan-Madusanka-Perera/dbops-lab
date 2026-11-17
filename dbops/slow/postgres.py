"""PostgreSQL: pg_stat_statements for the ranking, EXPLAIN (GENERIC_PLAN) for the plan.

pg_stat_statements already replaces literals with $1, $2..., and GENERIC_PLAN
(PostgreSQL 16+) plans a statement with its placeholders unfilled, so the stored
text can be explained as-is. EXPLAIN without ANALYZE never runs the statement.
EXPLAIN needs read access to the tables, so this connects as postgres, not as
the read-only monitor account.
"""

from __future__ import annotations

import json
import re

import psycopg

from . import Scan, Statement, explain_scan, finish, parse_order_by, parse_predicates, parse_sort_keys

TOP_SQL = """
SELECT s.query, s.calls, s.total_exec_time, (s.shared_blks_hit + s.shared_blks_read)::float / NULLIF(s.calls, 0)
FROM pg_stat_statements s JOIN pg_database d ON d.oid = s.dbid
WHERE d.datname = current_database()
  AND s.query ~* '^\\s*select' AND s.query !~* '(pg_stat|pg_catalog|pg_class|information_schema)'
ORDER BY s.total_exec_time DESC
LIMIT %s
"""


def _connect(env: dict) -> psycopg.Connection:
    return psycopg.connect(host=env.get("DBOPS_HOST", "127.0.0.1"), port=int(env.get("PG_PORT", 5442)),
                           dbname="shop", user="postgres", password=env["POSTGRES_PASSWORD"],
                           autocommit=True, connect_timeout=5)


def collect(env: dict, top: int) -> tuple[list[Statement], str]:
    with _connect(env) as conn, conn.cursor() as cur:
        rows = cur.execute(TOP_SQL, (top,)).fetchall()
        reltuples = dict(cur.execute("SELECT relname, reltuples::bigint FROM pg_class WHERE relkind = 'r'"
                                     " AND relnamespace = 'public'::regnamespace").fetchall())
        statements = []
        for query, calls, total_ms, blocks in rows:
            st = Statement("postgres", " ".join(query.split()), calls, total_ms,
                           work=f"{blocks:,.0f} blocks/call" if blocks is not None else "")
            try:
                plan = cur.execute(f"EXPLAIN (GENERIC_PLAN, FORMAT JSON) {explainable(query)}").fetchone()[0]
                plan = plan if isinstance(plan, list) else json.loads(plan)
                st.plan, st.scans = read_plan(plan[0]["Plan"], reltuples, query)
            except psycopg.Error as e:
                st.notes.append(f"could not EXPLAIN: {str(e).splitlines()[0][:100]}")
            for scan in st.scans:
                st.notes.append(explain_scan(scan))
            statements.append(finish(st))
    return statements, "pg_stat_statements since its last reset"


def explainable(query: str) -> str:
    """pg_stat_statements turns interval '1 day' into interval $1, which does not parse; $1::interval does."""
    return re.sub(r"\binterval\s+(\$\d+)", r"\1::interval", query, flags=re.IGNORECASE)


def read_plan(root: dict, reltuples: dict[str, int], sql: str = "") -> tuple[str, list[Scan]]:
    """Plan JSON -> ("Limit > Sort > Seq Scan on orders", scans worth an index).

    Two shapes are worth an index:
    - a Seq Scan with a Filter: every row read, most thrown away;
    - an Index Scan whose Filter has an equality the index does not cover: the
      index only gives the order (or a loose range), and rows are thrown away after
      it. INC-004: with (customer_id, created_at) dropped, the planner walked
      ix_orders_created_at backwards and filtered on customer_id.
    """
    summary, found = [], []

    def walk(node: dict, sort_keys: list[str], sorted_above: bool, depth: int) -> None:
        kind = node["Node Type"]
        if depth < 6:
            summary.append(f"{kind} on {node['Relation Name']}" if "Relation Name" in node else kind)
        is_sort = kind in ("Sort", "Incremental Sort")
        keys = node.get("Sort Key", sort_keys) if is_sort else sort_keys
        if kind == "Seq Scan" and node.get("Filter"):
            found.append(("full scan", node, keys, sorted_above or is_sort))
        elif kind in ("Index Scan", "Index Only Scan") and parse_predicates(node.get("Filter", ""))[0]:
            found.append((f"index scan on {node.get('Index Name', '?')} then filter", node, keys,
                          sorted_above or is_sort))
        for child in node.get("Plans", []):
            walk(child, keys, sorted_above or is_sort, depth + 1)

    walk(root, [], False, 0)
    scans = []
    for reason, node, keys, sorted_above in found:
        table, alias = node["Relation Name"], node.get("Alias", node["Relation Name"])
        eq, rng = parse_predicates(" AND ".join(filter(None, [node.get("Index Cond"), node.get("Filter")])))
        sort = [k for k in keys if k.split(".")[0] == alias or ("." not in k.split()[0] and len(found) == 1)]
        sort_keys = parse_sort_keys(", ".join(sort))
        # No Sort node above an index scan: the index delivers the ORDER BY, so take it from the SQL.
        if not sort_keys and not sorted_above and reason != "full scan" and len(found) == 1:
            sort_keys = parse_order_by(sql)
        scans.append(Scan(table, reason, eq, sort_keys, rng, table_rows=reltuples.get(table)))
    return " > ".join(summary), scans


def reset(env: dict) -> str:
    with _connect(env) as conn:
        conn.execute("SELECT pg_stat_statements_reset()")
    return "pg_stat_statements reset"
