"""Slow query analysis: rank statements by total time, read their plans, suggest indexes.

Each engine module returns Statements. What is shared lives here:

- parsing predicates, ORDER BY and table aliases out of SQL and plan text;
- the index rule, ESR: Equality columns first, then the Sort columns, then Range
  columns. An index in that order serves the filter, returns rows already sorted,
  and still narrows the range. The same rule holds for B-trees in all four engines.

Suggestions are candidates for a DBA to review, never applied automatically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MIN_ROWS_FOR_INDEX = 1000   # a full scan of a smaller table is not worth an index


@dataclass
class Scan:
    """A table read the plan says is expensive, and the columns that would fix it."""

    table: str
    reason: str                                   # "full scan", "sort after index lookup", ...
    eq: list[str] = field(default_factory=list)
    sort: list[tuple[str, int]] = field(default_factory=list)   # (column, 1 or -1)
    range: list[str] = field(default_factory=list)
    table_rows: int | None = None


@dataclass
class Statement:
    engine: str
    query: str
    calls: int
    total_ms: float
    plan: str = ""
    work: str = ""                                # rows read per call, or blocks, or buffer gets
    scans: list[Scan] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)

    @property
    def mean_ms(self) -> float:
        return self.total_ms / self.calls if self.calls else 0.0

    def to_dict(self) -> dict:
        return {"engine": self.engine, "query": self.query, "calls": self.calls,
                "total_ms": round(self.total_ms, 1), "mean_ms": round(self.mean_ms, 3),
                "plan": self.plan, "work": self.work, "notes": self.notes, "fixes": self.fixes,
                "scans": [s.__dict__ for s in self.scans]}


# ---------------------------------------------------------------- SQL text parsing

_KEYWORDS = {"and", "or", "not", "null", "is", "in", "like", "between", "select", "where", "from",
             "now", "sysdate", "systimestamp", "current_timestamp", "interval", "rownum", "text", "cache"}
_OP = r"(<=>|>=|<=|<>|!=|=|<|>|\bLIKE\b|\bIN\b|\bBETWEEN\b)"
# A column, maybe qualified, quoted (" or `), wrapped in parentheses or cast (::text),
# followed by a comparison operator.
_PREDICATE = re.compile(r"[`\"]?(\w+)[`\"]?\)?(?:::[\w ]+?)?\)?\s*" + _OP, re.IGNORECASE)
_EQ_OPS = {"=", "<=>", "in"}
_RANGE_OPS = {">", ">=", "<", "<=", "between", "like"}


def parse_predicates(text: str) -> tuple[list[str], list[str]]:
    """Condition text from any engine's plan -> (equality columns, range columns), lowercase."""
    eq, rng = [], []
    for m in _PREDICATE.finditer(text or ""):
        col, op = m.group(1).lower(), m.group(2).lower()
        if col in _KEYWORDS or col.isdigit() or col.startswith("$"):
            continue
        # skip the right-hand side of "a.x = b.y" style joins: preceded by an operator
        before = text[:m.start()].rstrip()
        if before.endswith(("=", "<", ">")):
            continue
        if op in _EQ_OPS and col not in eq:
            eq.append(col)
        elif op in _RANGE_OPS and col not in rng:
            rng.append(col)
    return eq, rng


_ORDER_BY = re.compile(r"\bORDER\s+BY\s+(.+?)(?:\bLIMIT\b|\bFETCH\b|\bOFFSET\b|\)|;|$)", re.IGNORECASE | re.DOTALL)


def parse_order_by(sql: str) -> list[tuple[str, int]]:
    m = _ORDER_BY.search(sql or "")
    return parse_sort_keys(m.group(1)) if m else []


def parse_sort_keys(text: str) -> list[tuple[str, int]]:
    """"o.created_at DESC, id" -> [("created_at", -1), ("id", 1)]"""
    keys = []
    for part in text.split(","):
        words = part.strip().strip("`\"").split()
        if not words:
            continue
        col = re.split(r"[.`\"]+", words[0].strip("`\""))[-1].lower()
        if col and re.fullmatch(r"\w+", col):
            keys.append((col, -1 if len(words) > 1 and words[1].lower() == "desc" else 1))
    return keys


_FROM = re.compile(r"\b(?:FROM|JOIN)\s+([\w.]+)(?:\s+(?:AS\s+)?(\w+))?", re.IGNORECASE)
_NOT_ALIAS = {"where", "join", "on", "group", "order", "left", "right", "inner", "outer", "limit",
              "fetch", "using", "natural", "cross", "full"}


def alias_map(sql: str) -> dict[str, str]:
    """{alias or table: table} from the FROM / JOIN clauses, all lowercase."""
    out = {}
    for table, alias in _FROM.findall(sql or ""):
        table = table.split(".")[-1].lower()
        out[table] = table
        if alias and alias.lower() not in _NOT_ALIAS:
            out[alias.lower()] = table
    return out


_LITERALS = [
    (re.compile(r"'(?:[^'\\]|\\.|'')*'"), "?"),
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "?"),
    (re.compile(r"\(\s*\?(?:\s*,\s*\?)+\s*\)"), "(?+)"),
    (re.compile(r"\s+"), " "),
]


def normalise_sql(sql: str) -> str:
    """Literals become ?, so the same statement with different values groups together."""
    out = sql.strip().rstrip(";")
    for pattern, repl in _LITERALS:
        out = pattern.sub(repl, out)
    return out


# ---------------------------------------------------------------- suggestions

def index_columns(scan: Scan) -> list[tuple[str, int]]:
    """ESR order, each column once."""
    cols: list[tuple[str, int]] = []
    seen = set()
    for col, direction in [(c, 1) for c in scan.eq] + scan.sort + [(c, 1) for c in scan.range]:
        if col not in seen:
            seen.add(col)
            cols.append((col, direction))
    return cols


def suggest_index(engine: str, scan: Scan) -> str | None:
    if scan.table_rows is not None and scan.table_rows < MIN_ROWS_FOR_INDEX:
        return None
    cols = index_columns(scan)
    if not cols:
        return None
    if engine == "mongo":
        spec = ", ".join(f"{c}: {d}" for c, d in cols)
        return f"db.{scan.table}.createIndex({{ {spec} }})"
    name = f"ix_{scan.table}_" + "_".join(c for c, _ in cols)
    # B-tree indexes can be read backwards, so DESC sorts need no DESC column.
    return f"CREATE INDEX {name} ON {scan.table} ({', '.join(c for c, _ in cols)});"


def explain_scan(scan: Scan) -> str:
    rows = f" ({scan.table_rows:,} rows)" if scan.table_rows else ""
    parts = [f"{scan.reason} of {scan.table}{rows}"]
    if scan.eq:
        parts.append("filtering on " + ", ".join(scan.eq))
    if scan.range:
        parts.append("range on " + ", ".join(scan.range))
    if scan.sort:
        parts.append("sorting by " + ", ".join(c for c, _ in scan.sort))
    return ", ".join(parts)


def finish(statement: Statement) -> Statement:
    """Turn scans into fixes (deduplicated, notes' fixes kept)."""
    for scan in statement.scans:
        ddl = suggest_index(statement.engine, scan)
        if ddl and ddl not in statement.fixes:
            statement.fixes.append(ddl)
    return statement
