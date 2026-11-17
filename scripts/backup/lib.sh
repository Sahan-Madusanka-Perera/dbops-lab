# shellcheck shell=bash
#
# lib.sh: shared by every scripts/backup/<engine>.sh. Sourced, not run.
#
# Each backup lives in backups/<engine>/<id>/, where <id> is the UTC start time,
# and carries a manifest.json:
#
#   { engine, id, source, method, started_at, finished_at, backup_seconds,
#     size_bytes, position, verify: { status, method, seconds, detail, tables } }
#
# verify.status is PASSED or FAILED. `dbops health` reads the newest manifest
# per engine, so a failed restore test shows up as CRIT there too.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
[[ -f .env ]] || { echo "error: .env not found; run: cp .env.example .env" >&2; exit 2; }
set -a
# shellcheck source=/dev/null  # .env is local and git-ignored
source .env
set +a

command -v jq >/dev/null || { echo "error: jq is needed (brew install jq)" >&2; exit 2; }

BACKUP_KEEP="${BACKUP_KEEP:-3}"           # newest backups kept per engine
BACKUP_ID="$(date -u +%Y%m%dT%H%M%SZ)"
VERIFY_STARTED=0

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# require_free_gb <gb> <why>: refuse to start without headroom on the host disk.
# Docker Desktop keeps its whole VM disk in one file on that same disk, so a
# restore inside a container fills the Mac's disk too. Running it dry once took
# the whole Docker VM down with I/O errors; this check exists because of that.
require_free_gb() {
  local need=$1 why=$2 free_kb free_gb
  free_kb=$(df -k "$REPO_ROOT/backups" | awk 'NR == 2 {print $4}')
  free_gb=$((free_kb / 1024 / 1024))
  if (( free_gb < need )); then
    echo "error: ${free_gb} GB free on the host disk; ${why} needs at least ${need} GB. Nothing was written." >&2
    exit 2
  fi
  log "disk: ${free_gb} GB free, ${need} GB needed for ${why}"
}

# start_backup <engine>: sets BACKUP_DIR (host) and the manifest skeleton
start_backup() {
  ENGINE=$1
  BACKUP_DIR="backups/$ENGINE/$BACKUP_ID"
  mkdir -p "$BACKUP_DIR"
  STARTED_AT=$(now_iso)
  STARTED_S=$SECONDS
  log "$ENGINE backup $BACKUP_ID -> $BACKUP_DIR"
}

# finish_backup <source> <method> <position>: write the manifest without a verdict yet
finish_backup() {
  local size
  size=$(du -sk "$BACKUP_DIR" | awk '{print $1 * 1024}')
  jq -n --arg engine "$ENGINE" --arg id "$BACKUP_ID" --arg source "$1" --arg method "$2" \
        --arg position "$3" --arg started "$STARTED_AT" --arg finished "$(now_iso)" \
        --argjson seconds $((SECONDS - STARTED_S)) --argjson size "$size" \
    '{engine: $engine, id: $id, source: $source, method: $method, started_at: $started,
      finished_at: $finished, backup_seconds: $seconds, size_bytes: $size, position: $position}' \
    > "$BACKUP_DIR/manifest.json"
  log "backup done in $((SECONDS - STARTED_S)) s, $(du -sh "$BACKUP_DIR" | awk '{print $1}')"
  VERIFY_STARTED=$SECONDS
}

# Comparison rows are collected as tab-separated "table  source  restored" lines.
# An empty value never matches: two failed queries must not look like agreement.
COMPARE_ROWS=""
compare() { COMPARE_ROWS+="$1"$'\t'"$2"$'\t'"$3"$'\n'; }

# finish_verify <method> <detail>
#   With compared tables: PASSED when every pair matches and nothing set VERIFY_OK=0.
#   Without (Oracle's RESTORE ... VALIDATE): PASSED only when the caller set VERIFY_OK=1.
# Exits 1 on FAILED, so cron and CI see the failure.
finish_verify() {
  local method=$1 detail=$2 tables status=FAILED
  tables=$(printf '%s' "$COMPARE_ROWS" | jq -R -s '
    split("\n") | map(select(length > 0) | split("\t")
      | {table: .[0], source: (.[1] // ""), restored: (.[2] // ""),
         match: ((.[1] // "") != "" and .[1] == .[2])})')

  if [[ "$(jq 'length' <<<"$tables")" -gt 0 ]]; then
    [[ "$(jq 'all(.match)' <<<"$tables")" == true && "${VERIFY_OK:-1}" == 1 ]] && status=PASSED
    echo
    { echo $'match\ttable\tsource\trestored'
      jq -r '.[] | [(if .match then "ok" else "DIFF" end), .table, .source, .restored] | @tsv' <<<"$tables"
    } | column -t -s $'\t' | sed 's/^/  /'
    echo
  else
    [[ "${VERIFY_OK:-0}" == 1 ]] && status=PASSED
  fi

  local manifest="$BACKUP_DIR/manifest.json"
  jq --arg status "$status" --arg method "$method" --arg detail "$detail" \
     --argjson seconds $((SECONDS - VERIFY_STARTED)) --argjson tables "$tables" \
     --arg at "$(now_iso)" \
     '.verify = {status: $status, method: $method, verified_at: $at, seconds: $seconds,
                 detail: $detail, tables: $tables}' "$manifest" > "$manifest.tmp"
  mv "$manifest.tmp" "$manifest"

  log "verify $status in $((SECONDS - VERIFY_STARTED)) s: $detail"
  [[ "$status" == PASSED ]] || exit 1
}

# prune <engine>: keep the newest BACKUP_KEEP backups of this engine
prune() {
  local dir
  while read -r dir; do
    [[ -n "$dir" ]] || continue
    log "prune: removing $dir (keeping newest $BACKUP_KEEP)"
    rm -rf "$dir"
  done < <(find "backups/$1" -mindepth 1 -maxdepth 1 -type d -name '2*' | sort -r | tail -n +$((BACKUP_KEEP + 1)))
}

# Throwaway containers for restore tests are always removed, even on failure.
SCRATCH=""
cleanup_scratch() { [[ -z "$SCRATCH" ]] || docker rm -f "$SCRATCH" >/dev/null 2>&1 || true; }
trap cleanup_scratch EXIT

# wait_for <seconds> <command...>: retry until the command succeeds
wait_for() {
  local limit=$1; shift
  for ((i = 0; i < limit; i++)); do
    "$@" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}
