#!/usr/bin/env bash
# Playforge backup — everything needed to rebuild an install from scratch.
#
# What goes in:
#   app.db          consistent snapshot (SQLite is in WAL mode, so a plain `cp`
#                   of the .db file alone can miss committed transactions still
#                   living in the -wal sidecar)
#   master.key      Fernet key for the credential vault. WITHOUT THIS the
#                   encrypted credentials in app.db are unrecoverable.
#   projects/       one git repo per project (full history)
#   credentials/    encrypted credential material
#   manifest.json   app version + schema version, so restore can warn on mismatch
#
# The archive is written mode 0600 and contains secrets in a form that is only as
# strong as the filesystem it sits on. Treat it like a private key: encrypt it
# before it leaves the host (see the note printed at the end).
#
# Usage:
#   scripts/backup.sh [DEST_DIR]          # default: ./backups
#   PLAYFORGE_DATA_DIR=/srv/data scripts/backup.sh /mnt/nas/playforge
#
# Safe to run against a live install: the DB snapshot uses SQLite's online
# backup API, and the project repos are copied with git's own consistency in
# mind (a run committing mid-copy shows up as either the old or the new commit).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${PLAYFORGE_DATA_DIR:-$REPO_ROOT/data}"
DEST_DIR="${1:-$REPO_ROOT/backups}"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
log() { printf '  %s\n' "$*"; }

[[ -d "$DATA_DIR" ]] || die "data dir not found: $DATA_DIR (set PLAYFORGE_DATA_DIR)"

DB="$DATA_DIR/app.db"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
chmod 700 "$STAGE"

printf 'Playforge backup\n  data dir: %s\n' "$DATA_DIR"

# --- 1. Consistent database snapshot ----------------------------------------
snapshot_db() {
  [[ -f "$DB" ]] || { log "no app.db yet — skipping (fresh install?)"; return 0; }

  # A snapshot inherits WAL mode from the source, leaving -wal/-shm sidecars next
  # to the copy. Flipping the *copy* to DELETE checkpoints them into the file and
  # removes them, so the archive holds one self-contained app.db; the app puts it
  # back into WAL on its first connection (PRAGMA listener in models/db.py).
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB" ".backup '$STAGE/app.db'"
    sqlite3 "$STAGE/app.db" "PRAGMA journal_mode=DELETE" >/dev/null
    rm -f "$STAGE/app.db-wal" "$STAGE/app.db-shm"
    log "app.db snapshot via sqlite3"
    return 0
  fi

  if command -v python3 >/dev/null 2>&1; then
    python3 - "$DB" "$STAGE/app.db" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(dst) as d:
    s.backup(d)
with sqlite3.connect(dst) as d:
    d.execute("PRAGMA journal_mode=DELETE")
PY
    rm -f "$STAGE/app.db-wal" "$STAGE/app.db-shm"
    log "app.db snapshot via python3 sqlite3"
    return 0
  fi

  # No snapshot tool: copy the sidecars too. Without a checkpoint they can hold
  # committed transactions, so the set is only consistent when kept together.
  cp "$DB" "$STAGE/app.db"
  for side in "$DB-wal" "$DB-shm"; do
    [[ -f "$side" ]] && cp "$side" "$STAGE/$(basename "$side")"
  done
  log "app.db copied with WAL sidecars (no sqlite3/python3 for a live snapshot)"
  printf 'warning: copied the database without an online snapshot. Stop the app first for a guaranteed-consistent backup.\n' >&2
}
snapshot_db

# --- 2. Secrets and project repos -------------------------------------------
if [[ -f "$DATA_DIR/master.key" ]]; then
  cp "$DATA_DIR/master.key" "$STAGE/master.key"
  chmod 600 "$STAGE/master.key"
  log "master.key"
else
  log "no master.key on the data volume (ANSIBLE_GUI_MASTER_KEY passed by env?)"
  printf 'warning: no master.key found. If the key comes from the environment, back that value up separately or the credential vault is unrecoverable.\n' >&2
fi

for dir in projects credentials; do
  if [[ -d "$DATA_DIR/$dir" ]]; then
    cp -R "$DATA_DIR/$dir" "$STAGE/$dir"
    log "$dir/ ($(find "$DATA_DIR/$dir" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ') entries)"
  fi
done

# --- 3. Manifest -------------------------------------------------------------
app_version="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$REPO_ROOT/backend/app/__init__.py" 2>/dev/null || true)"
schema_version=""
if [[ -f "$STAGE/app.db" ]] && command -v sqlite3 >/dev/null 2>&1; then
  schema_version="$(sqlite3 "$STAGE/app.db" \
    "SELECT value FROM schema_meta WHERE key='schema_version'" 2>/dev/null || true)"
fi

cat > "$STAGE/manifest.json" <<JSON
{
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "app_version": "${app_version:-unknown}",
  "schema_version": "${schema_version:-unknown}",
  "host": "$(hostname)",
  "data_dir": "$DATA_DIR"
}
JSON
log "manifest.json (app v${app_version:-unknown}, schema v${schema_version:-unknown})"

# --- 4. Archive --------------------------------------------------------------
mkdir -p "$DEST_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="$DEST_DIR/playforge-backup-${app_version:-unknown}-$STAMP.tar.gz"

umask 077
tar -czf "$ARCHIVE" -C "$STAGE" .
chmod 600 "$ARCHIVE"

printf '\nWrote %s (%s)\n' "$ARCHIVE" "$(du -h "$ARCHIVE" | cut -f1)"
cat <<'NOTE'

This archive contains the credential master key and your project repos — it is
as sensitive as the SSH keys inside it. Before copying it off this host:

  gpg --symmetric --cipher-algo AES256 <archive>     # then delete the plaintext

Restore with: scripts/restore.sh <archive>
NOTE
