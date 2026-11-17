#!/usr/bin/env bash
#
# Drill: MongoDB loses its primary (INC-005).
#
# mongo1 is killed (SIGKILL, like the host losing power). The two secondaries
# stop hearing its heartbeats, wait out electionTimeoutMillis (10 s) and elect a
# new primary. A writer connected the way the application connects (replica set
# string, shop_app, w: majority) measures how long writes could not be
# acknowledged.
#
#   inject    start the writer, kill mongo1, report the election and the write gap
#   fix       start mongo1 again; it rejoins as a secondary and catches up. It has
#             priority 2, so it then takes the primary back: a second election,
#             measured by the writer too
#   cleanup   make sure all three members are up with mongo1 primary, and drop the
#             writer's collection
#
# Usage:  drills/mongo-primary-lost.sh inject|fix|cleanup

# shellcheck source=drills/lib.sh
source "$(dirname "$0")/lib.sh"

WRITER_FROM=dbops-mongo3      # any container on the lab network that stays up
WRITER_SECONDS=40

# Who is primary, as members see it. Asks mongo2 (it never goes down in this drill).
members() {
  mongo_eval mongo2 'rs.status().members.forEach(m => print("  " + m.name + "  " + m.stateStr
    + (m.health ? "" : "  (unreachable)")))'
}

# Wait until the primary's name matches $1 (a grep pattern). Right after the kill,
# mongo2 still lists mongo1 as PRIMARY until its heartbeats time out, so the
# inject step waits for "mongo[23]", not for "any primary".
wait_for_primary() {
  local tries
  for ((tries = 0; tries < 60; tries++)); do
    if mongo_eval mongo2 'const p = rs.status().members.find(m => m.stateStr === "PRIMARY"); print(p ? p.name : "")' |
      grep -q "$1"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

# Start the writer in the background; its report is printed by writer_report.
WRITER_LOG=""
writer_start() {
  WRITER_LOG=$(mktemp)
  docker exec -e APP_PASSWORD -e WRITER_SECONDS="$WRITER_SECONDS" -e WRITER_TAG="$1" "$WRITER_FROM" \
    mongosh --quiet --nodb --eval "$(cat drills/mongo-writer.js)" >"$WRITER_LOG" 2>&1 &
  WRITER_PID=$!
}

writer_report() {
  wait "$WRITER_PID" || true
  sed 's/^/  writer: /' "$WRITER_LOG"
  rm -f "$WRITER_LOG"
}

inject() {
  say "drill start"
  members
  say "writer started from ${WRITER_FROM}: shop_app, replica set connection, w: majority, every 100 ms"
  writer_start inject
  sleep 5
  say "docker kill dbops-mongo1"
  docker kill dbops-mongo1 >/dev/null
  wait_for_primary "mongo[23]" || say "no new primary after 60 s"
  say "new primary elected:"
  members
  say "waiting for the writer to finish (${WRITER_SECONDS} s in all)"
  writer_report
}

fix() {
  say "writer started for the rejoin"
  writer_start fix
  sleep 3
  say "docker start dbops-mongo1"
  docker start dbops-mongo1 >/dev/null
  if wait_for_primary mongo1; then
    say "mongo1 is primary again (priority 2 took it back)"
  else
    say "mongo1 did not become primary within 60 s"
  fi
  members
  writer_report
}

cleanup() {
  if [[ "$(docker inspect -f '{{.State.Running}}' dbops-mongo1)" != true ]]; then
    say "mongo1 is down: running fix first"
    fix
  fi
  wait_for_primary mongo1 || { say "error: mongo1 is not primary"; exit 1; }
  mongo_eval mongo1 'print("dropped shop.drill_writes: " + db.getSiblingDB("shop").drill_writes.drop())'
  members
}

dispatch "$@"
