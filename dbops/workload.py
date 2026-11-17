"""A small, repeatable application workload, so slow queries have something to find.

The same four queries run against every engine, picked at random with a fixed
seed, so two runs are comparable (before and after an index, say):

  orders_for_customer   60%  a customer's latest orders     (no index on customer_id)
  customer_by_email     25%  login lookup                   (unique index: the control)
  pending_totals        10%  totals for one order status     (no index on status)
  recent_orders          5%  orders from the last day        (no index on created_at)

Connects as the application would: SHOP in Oracle and MariaDB, postgres in
PostgreSQL, no auth in MongoDB (until M5).
"""

from __future__ import annotations

import random
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .config import ENGINES, load_env

MIX = [("orders_for_customer", 60), ("customer_by_email", 25), ("pending_totals", 10), ("recent_orders", 5)]
CUSTOMERS = 5000

SQL = {
    "oracle": {
        "orders_for_customer": "SELECT id, status, total_cents, created_at FROM orders WHERE customer_id = :1 "
                               "ORDER BY created_at DESC FETCH FIRST 20 ROWS ONLY",
        "customer_by_email": "SELECT id, country FROM customers WHERE email = :1",
        "pending_totals": "SELECT c.country, COUNT(*), SUM(o.total_cents) FROM orders o "
                          "JOIN customers c ON c.id = o.customer_id WHERE o.status = :1 GROUP BY c.country",
        "recent_orders": "SELECT id, customer_id, total_cents FROM orders WHERE created_at >= SYSTIMESTAMP - INTERVAL '1' DAY "
                         "ORDER BY created_at DESC FETCH FIRST 50 ROWS ONLY",
    },
    "mariadb": {
        "orders_for_customer": "SELECT id, status, total_cents, created_at FROM orders WHERE customer_id = %s "
                               "ORDER BY created_at DESC LIMIT 20",
        "customer_by_email": "SELECT id, country FROM customers WHERE email = %s",
        "pending_totals": "SELECT c.country, COUNT(*), SUM(o.total_cents) FROM orders o "
                          "JOIN customers c ON c.id = o.customer_id WHERE o.status = %s GROUP BY c.country",
        "recent_orders": "SELECT id, customer_id, total_cents FROM orders WHERE created_at >= NOW() - INTERVAL 1 DAY "
                         "ORDER BY created_at DESC LIMIT 50",
    },
    "postgres": {
        "orders_for_customer": "SELECT id, status, total_cents, created_at FROM orders WHERE customer_id = %s "
                               "ORDER BY created_at DESC LIMIT 20",
        "customer_by_email": "SELECT id, country FROM customers WHERE email = %s",
        "pending_totals": "SELECT c.country, count(*), sum(o.total_cents) FROM orders o "
                          "JOIN customers c ON c.id = o.customer_id WHERE o.status = %s GROUP BY c.country",
        "recent_orders": "SELECT id, customer_id, total_cents FROM orders WHERE created_at >= now() - interval '1 day' "
                         "ORDER BY created_at DESC LIMIT 50",
    },
}


def _params(name: str, rng: random.Random):
    if name == "orders_for_customer":
        return (rng.randint(1, CUSTOMERS),)
    if name == "customer_by_email":
        return (f"customer{rng.randint(1, CUSTOMERS)}@example.com",)
    if name == "pending_totals":
        return (rng.choice(["PENDING", "REFUNDED"]),)
    return ()


@dataclass
class Result:
    engine: str
    latencies_ms: dict[str, list[float]] = field(default_factory=dict)
    error: str = ""

    def add(self, name: str, ms: float) -> None:
        self.latencies_ms.setdefault(name, []).append(ms)


def _run_sql(engine: str, env: dict, seconds: float, seed: int) -> Result:
    result, rng = Result(engine), random.Random(seed)
    host = env.get("DBOPS_HOST", "127.0.0.1")
    try:
        if engine == "oracle":
            import oracledb
            conn = oracledb.connect(user="shop", password=env["ORACLE_APP_PASSWORD"],
                                    dsn=f"{host}:{env.get('ORACLE_PORT', 1521)}/FREEPDB1")
        elif engine == "mariadb":
            import pymysql
            conn = pymysql.connect(host=host, port=int(env.get("MARIADB_PRIMARY_PORT", 3316)), user="shop",
                                   password=env["MARIADB_APP_PASSWORD"], database="shop", autocommit=True)
        else:
            import psycopg
            conn = psycopg.connect(host=host, port=int(env.get("PG_PORT", 5442)), dbname="shop",
                                   user="postgres", password=env["POSTGRES_PASSWORD"], autocommit=True)
    except Exception as e:  # noqa: BLE001 - reported, the other engines carry on
        result.error = str(e).splitlines()[0][:160]
        return result

    names, weights = zip(*MIX)
    deadline = time.monotonic() + seconds
    with conn:
        cur = conn.cursor()
        while time.monotonic() < deadline:
            name = rng.choices(names, weights)[0]
            start = time.perf_counter()
            cur.execute(SQL[engine][name], _params(name, rng))
            cur.fetchall()
            result.add(name, (time.perf_counter() - start) * 1000)
        cur.close()
    return result


def _run_mongo(env: dict, seconds: float, seed: int) -> Result:
    from pymongo import MongoClient

    result, rng = Result("mongo"), random.Random(seed)
    client = MongoClient(env.get("DBOPS_HOST", "127.0.0.1"), int(env.get("MONGO1_PORT", 27017)),
                         directConnection=True, serverSelectionTimeoutMS=5000)
    db = client.shop
    queries = {
        "orders_for_customer": lambda: list(db.orders.find({"customerId": rng.randint(1, CUSTOMERS)})
                                            .sort("createdAt", -1).limit(20)),
        "customer_by_email": lambda: db.customers.find_one({"email": f"customer{rng.randint(1, CUSTOMERS)}@example.com"}),
        "pending_totals": lambda: list(db.orders.aggregate([
            {"$match": {"status": rng.choice(["PENDING", "REFUNDED"])}},
            {"$group": {"_id": None, "orders": {"$sum": 1}, "total": {"$sum": "$totalCents"}}}])),
        "recent_orders": lambda: list(db.orders.find(
            {"createdAt": {"$gte": datetime.now(timezone.utc) - timedelta(days=1)}}).sort("createdAt", -1).limit(50)),
    }
    names, weights = zip(*MIX)
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            name = rng.choices(names, weights)[0]
            start = time.perf_counter()
            queries[name]()
            result.add(name, (time.perf_counter() - start) * 1000)
    except Exception as e:  # noqa: BLE001
        result.error = str(e).splitlines()[0][:160]
    finally:
        client.close()
    return result


def run(engines: list[str] | None, seconds: float, seed: int = 42) -> list[Result]:
    env = load_env()
    chosen = engines or list(ENGINES)
    with ThreadPoolExecutor(max_workers=len(chosen)) as pool:
        futures = [pool.submit(_run_mongo, env, seconds, seed) if e == "mongo"
                   else pool.submit(_run_sql, e, env, seconds, seed) for e in chosen]
        return [f.result() for f in futures]


def as_text(results: list[Result], seconds: float) -> str:
    lines = [f"Workload: {seconds:g} s per engine, all engines at once, seed fixed", ""]
    lines.append(f"  {'engine':<9} {'query':<20} {'calls':>7} {'p50 ms':>8} {'p95 ms':>8} {'calls/s':>8}")
    for r in results:
        if r.error:
            lines.append(f"  {r.engine:<9} error: {r.error}")
            continue
        for name, _ in MIX:
            ms = sorted(r.latencies_ms.get(name, []))
            if not ms:
                continue
            p95 = ms[min(len(ms) - 1, int(len(ms) * 0.95))]
            lines.append(f"  {r.engine:<9} {name:<20} {len(ms):>7} {statistics.median(ms):>8.2f} {p95:>8.2f} "
                         f"{len(ms) / seconds:>8.1f}")
    return "\n".join(lines)
