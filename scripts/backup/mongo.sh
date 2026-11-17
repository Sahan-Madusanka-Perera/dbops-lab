#!/usr/bin/env bash
#
# mongo.sh: logical backup from a secondary, then a restore test.
#
#   backup   mongodump --oplog on mongo2 (a secondary, so the primary does no
#            extra work). --oplog also captures writes made during the dump, so
#            the restore is consistent as of the moment the dump finished.
#   verify   mongorestore --oplogReplay into a throwaway standalone mongo:8.0,
#            then compare every shop collection's document count and dbHash
#            (an md5 of its documents, the same hash replica set members use to
#            check they agree) with the source.
#
# mongorestore refuses --oplogReplay with --nsExclude, so the whole dump is restored.
#
# shellcheck source=scripts/backup/lib.sh
source "$(dirname "$0")/lib.sh"

SOURCE=dbops-mongo2
SCRATCH=dbops-verify-mongo
DB=shop

require_free_gb 2 "a mongodump and a scratch restore"
start_backup mongo
docker exec "$SOURCE" mongodump --quiet --oplog --gzip --archive="/backups/$BACKUP_ID/dump.archive.gz"
OPTIME=$(docker exec "$SOURCE" mongosh --quiet --eval \
  'const t = rs.status().members.find(m => m.self).optime.ts; `${t.getHighBits()}:${t.getLowBits()}`')
finish_backup "$SOURCE" "mongodump --oplog --gzip (secondary)" "oplog ts $OPTIME on mongo2 after the dump"

log "restore into $SCRATCH"
docker run -d --name "$SCRATCH" -v "$REPO_ROOT/$BACKUP_DIR:/backup:ro" mongo:8.0 >/dev/null
wait_for 60 docker exec "$SCRATCH" mongosh --quiet --eval 'db.adminCommand("ping").ok' \
  || { VERIFY_OK=0; finish_verify restore "scratch server never came up"; }
if ! docker exec "$SCRATCH" mongorestore --quiet --gzip --archive=/backup/dump.archive.gz --oplogReplay; then
  VERIFY_OK=0; finish_verify mongorestore "mongorestore failed"
fi

# count + dbHash per collection, as "name<TAB>count docs, dbHash xxxx" lines
summary() {
  docker exec "$1" mongosh --quiet "$DB" --eval '
    const hashes = db.runCommand({ dbHash: 1 }).collections;
    Object.keys(hashes).sort().forEach(c =>
      print(`${c}\t${db.getCollection(c).countDocuments()} docs, dbHash ${hashes[c].slice(0, 12)}`));'
}
SRC=$(summary "$SOURCE")
DST=$(summary "$SCRATCH")
while IFS=$'\t' read -r coll value; do
  compare "$coll" "$value" "$(awk -F'\t' -v c="$coll" '$1 == c {print $2}' <<<"$DST")"
done <<<"$SRC"

finish_verify "mongorestore --oplogReplay into mongo:8.0, count + dbHash per collection" \
  "$(wc -l <<<"$SRC" | tr -d ' ') collections restored and compared"
prune mongo
