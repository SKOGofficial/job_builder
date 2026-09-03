#!/usr/bin/env bash
# Back up the tracker database, safely, while the app is running.
#
#   ./deploy/backup.sh [database] [backup-dir] [keep]
#
# Uses "VACUUM INTO" rather than cp. A plain copy of a live SQLite file can
# catch it mid-write and produce a backup that will not open - and in WAL mode
# it also misses everything still sitting in the -wal file. VACUUM INTO takes a
# consistent snapshot through the database engine and compacts it on the way
# out.
#
# Also checkpoints the live database's WAL after a good backup exists. Left
# alone it only grows - VACUUM INTO reads through it but never truncates it.
#
# Install as a timer:
#   sudo cp deploy/job-builder-backup.{service,timer} /etc/systemd/system/
#   sudo systemctl enable --now job-builder-backup.timer

set -euo pipefail

DB="${1:-/opt/job_builder/job_applications.sqlite3}"
DEST="${2:-/var/backups/job_builder}"
KEEP="${3:-14}"

if [ ! -f "$DB" ]; then
  echo "No database at $DB" >&2
  exit 1
fi

mkdir -p "$DEST"
STAMP="$(date +%Y%m%d-%H%M%S)"
TARGET="$DEST/job_applications-$STAMP.sqlite3"

sqlite3 "$DB" "VACUUM INTO '$TARGET'"
echo "Wrote $TARGET ($(du -h "$TARGET" | cut -f1))"

# Verify before trusting it. A backup nobody has ever opened is a hope, not a
# backup.
if ! sqlite3 "$TARGET" "PRAGMA integrity_check" | grep -q '^ok$'; then
  echo "Integrity check FAILED for $TARGET" >&2
  exit 1
fi

# Fold the live WAL back into the main file now that a good backup exists.
# TRUNCATE checkpoints and then shrinks the -wal file; it can leave it
# partially checkpointed if a writer is active right now, which is fine - the
# next timer run tries again, and it never blocks the app's own writes.
sqlite3 "$DB" "PRAGMA wal_checkpoint(TRUNCATE);" > /dev/null

# Prune oldest first, keeping the most recent $KEEP.
mapfile -t OLD < <(ls -1t "$DEST"/job_applications-*.sqlite3 2>/dev/null | tail -n +"$((KEEP + 1))")
for file in "${OLD[@]:-}"; do
  [ -n "$file" ] && rm -f -- "$file" && echo "Pruned $(basename "$file")"
done
