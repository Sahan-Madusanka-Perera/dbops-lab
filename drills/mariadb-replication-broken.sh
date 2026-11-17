#!/usr/bin/env bash
#
# Drill: MariaDB replication broken by a write on the replica (INC-003).
#
# Someone fixing a customer record connects to the replica instead of the
# primary, as root. read_only does not stop root (it holds READ_ONLY ADMIN), so
# the insert takes customers.id 5001 on the replica only. The next signup on the
# primary also gets id 5001, and the replica's SQL thread stops on a duplicate key.
#
#   inject    the stray insert on the replica, then normal traffic on the primary
#   fix       put the replica back in line with the primary and restart replication;
#             see INC-003 for why each step is there
#   cleanup   fix (if still needed), then remove the drill's rows from the primary
#             and check both servers hold the same data
#
# Usage:  drills/mariadb-replication-broken.sh inject|fix|cleanup

# shellcheck source=drills/lib.sh
source "$(dirname "$0")/lib.sh"

PRIMARY=dbops-mariadb-primary
REPLICA=dbops-mariadb-replica
STRAY=drill-stray@example.com     # written on the replica by mistake
SIGNUP=drill-signup@example.com   # a real signup on the primary

# The application, on the primary: shop_app, DML only.
app() {
  docker exec -i -e MYSQL_PWD="$APP_PASSWORD" "$PRIMARY" mariadb -ushop_app --table shop
}

replica_status() {
  echo "SHOW REPLICA STATUS\G" | maria_root "$REPLICA" |
    grep -E ' (Slave_IO_Running|Slave_SQL_Running|Last_SQL_Errno|Last_SQL_Error|Gtid_IO_Pos|Seconds_Behind_Master):' |
    sed 's/^ *//'
  echo "SELECT @@gtid_slave_pos AS gtid_slave_pos, @@gtid_binlog_pos AS gtid_binlog_pos" | maria_root "$REPLICA"
}

inject() {
  say "drill start"
  replica_status
  say "on the REPLICA, as root: insert a customer (read_only does not stop root)"
  maria_root "$REPLICA" <<SQL
INSERT INTO customers (email, country) VALUES ('${STRAY}', 'SG');
SELECT id, email FROM customers WHERE email = '${STRAY}';
SQL
  say "on the PRIMARY, as shop_app: a new signup and its first order"
  app <<SQL
INSERT INTO customers (email, country) VALUES ('${SIGNUP}', 'LK');
INSERT INTO orders (customer_id, status, total_cents, created_at)
  SELECT id, 'PENDING', 4200, NOW() FROM customers WHERE email = '${SIGNUP}';
SELECT id, email FROM customers WHERE email = '${SIGNUP}';
SQL
  sleep 2
  say "replica now:"
  replica_status
}

fix() {
  say "before:"
  replica_status
  # 1. Remove the stray row on the replica. sql_log_bin = 0: this repair is local
  #    and must not become another transaction in the replica's binlog.
  say "remove the stray row on the replica (not binlogged)"
  maria_root "$REPLICA" <<SQL
SET SESSION sql_log_bin = 0;
DELETE FROM customers WHERE email = '${STRAY}';
SQL
  # 2. The stray insert was binlogged on the replica under its own server_id,
  #    an errant GTID. With gtid_strict_mode the replica refuses the primary's
  #    next GTID with the same sequence number, and a failover to this replica
  #    would hand the transaction to the other servers. Rebuild the replica's
  #    binlog GTID state from what it applied from the primary (gtid_slave_pos).
  say "drop the errant GTID: reset the replica's own binlog, keep gtid_slave_pos"
  maria_root "$REPLICA" <<SQL
STOP REPLICA;
RESET MASTER;
START REPLICA;
SQL
  sleep 2
  say "after:"
  replica_status
}

# Same data on both servers: CHECKSUM TABLE reads every row.
compare() {
  local p r
  p=$(echo "CHECKSUM TABLE customers, orders" | maria_root "$PRIMARY" | awk '/shop\./ {print $2, $4}')
  r=$(echo "CHECKSUM TABLE customers, orders" | maria_root "$REPLICA" | awk '/shop\./ {print $2, $4}')
  echo "primary: $p" | paste -sd' ' -
  echo "replica: $r" | paste -sd' ' -
  if [[ "$p" != "$r" ]]; then
    say "checksums DIFFER"
    return 1
  fi
  say "checksums match"
}

cleanup() {
  if echo "SHOW REPLICA STATUS\G" | maria_root "$REPLICA" | grep -q 'Slave_SQL_Running: No'; then
    say "replication still stopped: running fix first"
    fix
  fi
  say "remove the drill's rows on the primary (replicated)"
  maria_root "$PRIMARY" <<SQL
DELETE o FROM orders o JOIN customers c ON c.id = o.customer_id WHERE c.email = '${SIGNUP}';
DELETE FROM customers WHERE email IN ('${SIGNUP}', '${STRAY}');
SQL
  sleep 2
  compare
}

case "${1:-}" in
  compare) compare ;;
  *) dispatch "$@" ;;
esac
