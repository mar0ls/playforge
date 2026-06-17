#!/bin/sh
# Bind-mounted /data comes up root-owned; the app runs as uid 1000. Fix ownership
# as root (only when it isn't already 1000, to keep restarts fast), then drop to
# the app user via gosu.
set -e

if [ "$(id -u)" = "0" ]; then
    mkdir -p /data
    if [ "$(stat -c %u /data 2>/dev/null)" != "1000" ]; then
        echo "[entrypoint] chown /data -> app (uid 1000)"
        chown -R app:app /data || true
    fi
    exec gosu app "$@"
fi

# Already non-root (e.g. an override set `user:`).
exec "$@"
