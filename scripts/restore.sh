#!/usr/bin/env bash
# Playforge restore — rebuild a data volume from a scripts/backup.sh archive.
#
# Usage:
#   scripts/restore.sh <archive.tar.gz> [DATA_DIR]
#   PLAYFORGE_DATA_DIR=/srv/data scripts/restore.sh backup.tar.gz
#
# Refuses to run against a data dir that already holds an install unless
# --force is passed, and always moves the existing data aside rather than
# deleting it — a restore into the wrong directory should be undoable.
#
# STOP THE APP FIRST. Restoring under a running container gives the process a
# database that changed beneath it, which is how you get a corrupt WAL.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FORCE=0
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    *) ARGS+=("$arg") ;;
  esac
done

ARCHIVE="${ARGS[0]:-}"
DATA_DIR="${ARGS[1]:-${PLAYFORGE_DATA_DIR:-$REPO_ROOT/data}}"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
log() { printf '  %s\n' "$*"; }

[[ -n "$ARCHIVE" ]] || die "usage: scripts/restore.sh <archive.tar.gz> [DATA_DIR] [--force]"
[[ -f "$ARCHIVE" ]] || die "archive not found: $ARCHIVE"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
chmod 700 "$STAGE"

tar -xzf "$ARCHIVE" -C "$STAGE"
[[ -f "$STAGE/manifest.json" ]] || die "not a Playforge backup (no manifest.json): $ARCHIVE"

printf 'Playforge restore\n  archive:  %s\n  data dir: %s\n\n' "$ARCHIVE" "$DATA_DIR"
sed 's/^/  /' "$STAGE/manifest.json"
printf '\n'

# --- Version sanity check ----------------------------------------------------
# Restoring a newer backup into an older build is the dangerous direction: the
# schema may carry columns this code doesn't know about, and there is no
# downgrade path. The reverse (older backup, newer build) is fine — migrations
# run on the next start.
backup_schema="$(sed -n 's/.*"schema_version": "\([^"]*\)".*/\1/p' "$STAGE/manifest.json")"
code_schema="$(sed -n 's/^SCHEMA_VERSION = \([0-9]*\)$/\1/p' \
  "$REPO_ROOT/backend/app/models/migrations.py" 2>/dev/null || true)"
if [[ -n "$backup_schema" && -n "$code_schema" && "$backup_schema" != "unknown" ]]; then
  if (( backup_schema > code_schema )); then
    printf 'warning: backup is schema v%s but this checkout is v%s — restoring a newer\n' \
      "$backup_schema" "$code_schema" >&2
    printf '         backup into an older build is not supported. Upgrade first.\n' >&2
    (( FORCE )) || die "refusing without --force"
  fi
fi

# --- Guard the existing data dir --------------------------------------------
if [[ -d "$DATA_DIR" ]] && [[ -n "$(ls -A "$DATA_DIR" 2>/dev/null | grep -v '^\.gitkeep$' || true)" ]]; then
  if (( ! FORCE )); then
    die "$DATA_DIR is not empty. Stop the app, then re-run with --force (the current data is moved aside, not deleted)."
  fi
  ASIDE="$DATA_DIR.pre-restore.$(date -u +%Y%m%dT%H%M%SZ)"
  mv "$DATA_DIR" "$ASIDE"
  log "existing data moved to $ASIDE"
fi

mkdir -p "$DATA_DIR"

# --- Restore -----------------------------------------------------------------
for item in app.db app.db-wal app.db-shm master.key; do
  [[ -f "$STAGE/$item" ]] && { cp "$STAGE/$item" "$DATA_DIR/$item"; log "$item"; }
done
[[ -f "$DATA_DIR/master.key" ]] && chmod 600 "$DATA_DIR/master.key"

for dir in projects credentials; do
  [[ -d "$STAGE/$dir" ]] && { cp -R "$STAGE/$dir" "$DATA_DIR/$dir"; log "$dir/"; }
done
[[ -d "$DATA_DIR/credentials" ]] && chmod 700 "$DATA_DIR/credentials"

# The container runs as uid 1000; restoring as root/your user otherwise leaves a
# data dir the app can't write. Only meaningful when running as root.
if [[ "$(id -u)" == "0" ]]; then
  chown -R 1000:1000 "$DATA_DIR"
  log "ownership set to uid 1000 (container app user)"
fi

cat <<NOTE

Restored into $DATA_DIR.

Start the app — any pending schema migrations run automatically on boot:
  docker compose up -d
  curl -fsS http://127.0.0.1:8765/health
NOTE
