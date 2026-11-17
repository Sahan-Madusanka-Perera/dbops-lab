"""dbops slow: parsing SQL and plan text, and the index each finding suggests. No databases needed."""

from dbops.slow import (Scan, Statement, alias_map, finish, index_columns, normalise_sql, parse_order_by,
                        parse_predicates, suggest_index)
from dbops.slow.mariadb import parse_slow_log
from dbops.slow.mariadb import read_plan as mariadb_plan
from dbops.slow.mongo import shape, split_filter
from dbops.slow.postgres import explainable
from dbops.slow.postgres import read_plan as postgres_plan

# ---------------------------------------------------------------- SQL and plan text


def test_predicates_from_each_engines_plan_text():
    # PostgreSQL Filter
    assert parse_predicates("((customer_id = $1) AND (created_at >= (now() - $2::interval)))") == (
        ["customer_id"], ["created_at"])
    assert parse_predicates("((status)::text = 'PENDING'::text)") == (["status"], [])
    # MariaDB attached_condition
    assert parse_predicates("o.`status` = 'PENDING' and o.created_at >= '2026-01-01'") == (["status"], ["created_at"])
    # Oracle filter_predicates
    assert parse_predicates('"CUSTOMER_ID"=:1') == (["customer_id"], [])


def test_join_right_hand_side_is_not_a_filter_column():
    eq, _ = parse_predicates("o.customer_id = c.id AND c.country = 'LK'")
    assert eq == ["customer_id", "country"]


def test_order_by_directions_and_end_of_clause():
    assert parse_order_by("SELECT * FROM orders ORDER BY o.created_at DESC, id LIMIT 20") == [
        ("created_at", -1), ("id", 1)]
    assert parse_order_by("SELECT * FROM orders WHERE id = 1 ORDER BY created_at FETCH FIRST 5 ROWS ONLY") == [
        ("created_at", 1)]
    assert parse_order_by("SELECT * FROM orders") == []


def test_alias_map_ignores_keywords_after_the_table():
    assert alias_map("SELECT * FROM shop.orders o JOIN customers AS c ON c.id = o.customer_id") == {
        "orders": "orders", "o": "orders", "customers": "customers", "c": "customers"}
    assert alias_map("SELECT * FROM orders WHERE id = 1") == {"orders": "orders"}


def test_normalise_sql_groups_the_same_statement_with_other_values():
    a = normalise_sql("SELECT * FROM orders WHERE customer_id = 42 AND status IN ('A', 'B');")
    b = normalise_sql("SELECT *  FROM orders WHERE customer_id = 7 AND status IN ('C','D','E')")
    assert a == b == "SELECT * FROM orders WHERE customer_id = ? AND status IN (?+)"


# ---------------------------------------------------------------- suggestions


def test_esr_puts_equality_then_sort_then_range():
    scan = Scan("orders", "full scan", eq=["customer_id"], sort=[("created_at", -1)], range=["total", "created_at"])
    assert index_columns(scan) == [("customer_id", 1), ("created_at", -1), ("total", 1)]
    assert suggest_index("postgres", scan) == (
        "CREATE INDEX ix_orders_customer_id_created_at_total ON orders (customer_id, created_at, total);")
    assert suggest_index("mongo", scan) == "db.orders.createIndex({ customer_id: 1, created_at: -1, total: 1 })"


def test_no_index_for_small_tables_or_scans_without_columns():
    assert suggest_index("postgres", Scan("countries", "full scan", eq=["code"], table_rows=200)) is None
    assert suggest_index("postgres", Scan("orders", "full scan")) is None


def test_finish_keeps_existing_fixes_and_deduplicates():
    st = Statement("mariadb", "q", 1, 1.0, fixes=["ANALYZE TABLE orders;"],
                   scans=[Scan("orders", "full scan", eq=["status"])] * 2)
    assert finish(st).fixes == ["ANALYZE TABLE orders;", "CREATE INDEX ix_orders_status ON orders (status);"]


# ---------------------------------------------------------------- MariaDB slow log

SLOW_LOG = """\
mariadbd, Version: 11.4.13-MariaDB-ubu2404-log (mariadb.org binary distribution). started with:
Tcp port: 0  Unix socket: /run/mysqld/mysqld.sock
Time		    Id Command	Argument
# Time: 261117  9:00:01
# User@Host: shop[shop] @  [172.18.0.1]
# Thread_id: 8  Schema: shop  QC_hit: No
# Query_time: 75.000000  Lock_time: 0.000000  Rows_sent: 1  Rows_examined: 0
SET timestamp=1763370001;
SELECT SLEEP(75);
mariadbd, Version: 11.4.13-MariaDB-ubu2404-log (mariadb.org binary distribution). started with:
Tcp port: 0  Unix socket: /run/mysqld/mysqld.sock
Time		    Id Command	Argument
# User@Host: shop[shop] @  [172.18.0.1]
# Thread_id: 9  Schema: shop  QC_hit: No
# Query_time: 0.006000  Lock_time: 0.000010  Rows_sent: 2  Rows_examined: 55005
use shop;
SET timestamp=1763370100;
SELECT id FROM orders
WHERE status = 'PENDING';
"""


def test_slow_log_restart_banner_ends_an_entry_and_is_not_sql():
    first, second = parse_slow_log(SLOW_LOG)
    assert first.sql == ["SELECT SLEEP(75);"]
    assert (first.user, first.schema, first.query_time) == ("shop", "shop", 75.0)
    assert second.sql == ["SELECT id FROM orders", "WHERE status = 'PENDING';"]
    assert (second.rows_sent, second.rows_examined) == (2, 55005)


def test_mariadb_plan_full_scan_and_sort_owner():
    sql = "SELECT * FROM orders o WHERE o.status = 'PENDING' ORDER BY o.created_at DESC LIMIT 20"
    plan = {"query_block": {"nested_loop": [{"read_sorted_file": {"filesort": {
        "sort_key": "o.created_at desc",
        "table": {"table_name": "o", "access_type": "ALL", "rows": 1,
                  "attached_condition": "o.`status` = 'PENDING'"}}}}]}}
    summary, scans, estimates = mariadb_plan(plan, sql)
    assert summary == "filesort > ALL on orders"
    assert scans == [Scan("orders", "full scan", ["status"], [("created_at", -1)], [])]
    assert estimates == {"orders": 1}


# ---------------------------------------------------------------- PostgreSQL


def test_interval_placeholder_is_made_explainable():
    q = "SELECT count(*) FROM orders WHERE created_at >= now() - interval $1"
    assert explainable(q) == "SELECT count(*) FROM orders WHERE created_at >= now() - $1::interval"
    assert explainable("SELECT 1") == "SELECT 1"


def test_postgres_plan_sort_key_goes_to_its_scan():
    root = {"Node Type": "Limit", "Plans": [{"Node Type": "Sort", "Sort Key": ["created_at DESC"], "Plans": [
        {"Node Type": "Seq Scan", "Relation Name": "orders", "Alias": "orders",
         "Filter": "(customer_id = $1)"}]}]}
    summary, scans = postgres_plan(root, {"orders": 50000})
    assert summary == "Limit > Sort > Seq Scan on orders"
    assert scans == [Scan("orders", "full scan", ["customer_id"], [("created_at", -1)], [], table_rows=50000)]


# ---------------------------------------------------------------- MongoDB


def test_mongo_filter_split_and_shape():
    flt = {"customerId": 42, "createdAt": {"$gte": "2026-01-01"}, "$comment": "x"}
    assert split_filter(flt) == (["customerId"], ["createdAt"])
    assert shape("query", {"find": "orders", "filter": flt, "sort": {"createdAt": -1}, "limit": 20}) == (
        "query orders {customerId: ?, createdAt: <range>} sort {createdAt: -1} limit 20")
    agg = {"aggregate": "orders", "pipeline": [{"$match": {"status": "PENDING"}}, {"$group": {}}, {"$sort": {}}]}
    assert shape("command", agg) == "aggregate orders {status: ?} > $group > $sort"
