#!/usr/bin/env bash
# Back up what this server cannot rebuild on its own:
#   - the SQLite database (history, evidence documents, invites), copied with SQLite's online
#     backup API while the app keeps running;
#   - ~/work/secrets/holdup.env (HOLDUP_SECRET, from which every user account is derived, and
#     the NEAR keys), plus /data/credentials.json if the contracts were deployed from the server.
# Writes <backup-directory>/holdup-<UTC time>.tar.gz with mode 600; the default directory is
# ~/work/backups. The archive holds secrets: copy it to encrypted storage off this server.
#
#   deploy/backup.sh [backup-directory]
#
# Restoring is described under "Backups" in deploy/README.md.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${1:-$HOME/work/backups}"
ENV_FILE="$DEPLOY_DIR/../../secrets/holdup.env"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

umask 077
mkdir -p "$DEST"
staging="$(mktemp -d "$DEST/.staging.XXXXXX")"
trap 'rm -rf "$staging"' EXIT
cd "$DEPLOY_DIR"

# Exit status 3 from the snippet means there is no database file (offline mode keeps it in memory).
if docker compose exec -T holdup python - <<'EOF'; then
import os
import sqlite3
import sys

source = os.environ.get("HOLDUP_DB") or "/data/holdup.sqlite3"
if source == ":memory:" or not os.path.exists(source):
    print(f"no database file at {source}; skipping the database", file=sys.stderr)
    sys.exit(3)
target = "/data/.backup.sqlite3"
if os.path.exists(target):
    os.remove(target)
src = sqlite3.connect(source, timeout=30)
dst = sqlite3.connect(target)
src.backup(dst)
dst.close()
src.close()
EOF
  docker compose cp holdup:/data/.backup.sqlite3 "$staging/holdup.sqlite3"
  docker compose exec -T holdup rm -f /data/.backup.sqlite3
else
  status=$?
  if [[ $status -ne 3 ]]; then
    echo "error: the database backup failed; is the holdup service running? (docker compose ps)" >&2
    exit 1
  fi
fi

# Present only when `holdup deploy` was run inside the container (see deploy/README.md).
if docker compose exec -T holdup test -f /data/credentials.json; then
  docker compose cp holdup:/data/credentials.json "$staging/credentials.json"
fi

if [[ -r $ENV_FILE ]]; then
  cp "$ENV_FILE" "$staging/holdup.env"
else
  echo "warning: $ENV_FILE not readable; the archive has no secrets file" >&2
fi

if [[ -z "$(ls -A "$staging")" ]]; then
  echo "error: nothing to back up" >&2
  exit 1
fi

archive="$DEST/holdup-$STAMP.tar.gz"
tar -C "$staging" -czf "$archive" .
chmod 600 "$archive"
echo "$archive ($(du -h "$archive" | cut -f1)): $(ls "$staging" | tr '\n' ' ')"
