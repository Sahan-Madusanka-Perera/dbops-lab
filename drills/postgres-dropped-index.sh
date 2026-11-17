#!/usr/bin/env bash
#
# Drill: an index dropped on PostgreSQL (INC-004).
#
# ix_orders_customer_id_created_at serves the busiest query, "a customer's last 20
# orders" (M4 added it). A cleanup that drops indexes looking unused, or a
# migration's down step run against the wrong database, removes it. Nothing
# errors: the query still runs, as a full scan of orders, and gets slower.
#
#   inject    drop the index (DROP INDEX CONCURRENTLY, as a cleanup job would)
#   fix       rebuild it without blocking writes, then refresh statistics
#   cleanup   make sure the index exists and is valid (safe to run any time)
#
# Measure around it with:  dbops workload --engine postgres   and   dbops slow --engine postgres
#
# Usage:  drills/postgres-dropped-index.sh inject|fix|cleanup

# shellcheck source=drills/lib.sh
source "$(dirname "$0")/lib.sh"

INDEX=ix_orders_customer_id_created_at

index_state() {
  psql_shop -At -c "
    SELECT COALESCE((SELECT CASE WHEN i.indisvalid THEN 'present, valid' ELSE 'present, INVALID' END
                     FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
                     WHERE c.relname = '${INDEX}'), 'missing')"
}

plan() {
  psql_shop -c "EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
    SELECT id, status, total_cents, created_at FROM orders WHERE customer_id = 42
    ORDER BY created_at DESC LIMIT 20"
}

inject() {
  say "drill start: ${INDEX} $(index_state)"
  plan
  say "DROP INDEX CONCURRENTLY ${INDEX}"
  psql_shop -c "DROP INDEX CONCURRENTLY IF EXISTS ${INDEX}"
  say "now: ${INDEX} $(index_state)"
  plan
}

fix() {
  say "before: ${INDEX} $(index_state)"
  # A failed CONCURRENTLY build leaves an INVALID index that is still maintained on
  # every write but never used for reads: drop it before building again.
  if [[ "$(index_state)" == "present, INVALID" ]]; then
    say "an invalid index is left from an earlier build: dropping it"
    psql_shop -c "DROP INDEX CONCURRENTLY ${INDEX}"
  fi
  # CONCURRENTLY: reads and writes carry on during the build. Slower than a plain
  # CREATE INDEX, which would block every write to orders until it finished.
  local start=$SECONDS
  psql_shop -c "CREATE INDEX CONCURRENTLY IF NOT EXISTS ${INDEX} ON orders (customer_id, created_at)"
  psql_shop -c "ANALYZE orders"
  say "built in $((SECONDS - start)) s: ${INDEX} $(index_state)"
  plan
}

cleanup() {
  if [[ "$(index_state)" == "present, valid" ]]; then
    say "${INDEX} present, valid: nothing to do"
  else
    fix
  fi
}

dispatch "$@"
