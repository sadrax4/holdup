#!/usr/bin/env bash
# Back up what this server cannot rebuild by itself:
#   - HoldUp's files in the secrets directory: holdup.env (HOLDUP_SECRET, from which every user's
#     NEAR account and key are derived, the bot token and the Otari key) and
#     holdup-credentials.json (the treasury, oracle and keeper keys), plus any older credentials
#     files that `server.sh testnet --force-new` moved aside;
#   - the SQLite database (history and evidence documents), copied with SQLite's online backup
#     API while HoldUp keeps running.
#
#   bash deploy/backup.sh [DIR]        (or: bash scripts/server.sh backup [DIR])
#
# Writes DIR/holdup-<UTC time>.tar.gz with mode 600; DIR defaults to $HOLDUP_HOME/backups
# (~/work/backups). The archive holds secrets: keep it private. Nothing is uploaded anywhere.
# Restoring is described under "Backups" in deploy/README.md.
set -euo pipefail
set +x # the files handled here hold secrets
umask 077

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

[[ $(id -u) -ne 0 ]] || die "do not run this as root"
HOLDUP_HOME="${HOLDUP_HOME:-$HOME/work}"
SECRETS_DIR="${HOLDUP_SECRETS_DIR:-$HOLDUP_HOME/secrets}"
DATA_DIR="${HOLDUP_DATA_DIR:-$HOLDUP_HOME/holdup-data}"
DEST="${1:-$HOLDUP_HOME/backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

[[ -d $SECRETS_DIR ]] || die "no secrets directory at $SECRETS_DIR"
[[ $(stat -c %u -- "$SECRETS_DIR") == "$(id -u)" ]] || die "$SECRETS_DIR belongs to another user"
mkdir -p -m 700 -- "$DEST"
staging="$(mktemp -d "$DEST/.staging.XXXXXX")"
trap 'rm -rf -- "$staging"' EXIT
mkdir -m 700 -- "$staging/secrets"

shopt -s nullglob
copied=0
for file in "$SECRETS_DIR/holdup.env" "$SECRETS_DIR"/holdup-credentials.json*; do
  if [[ -f $file ]]; then
    cp -p -- "$file" "$staging/secrets/"
    copied=$((copied + 1))
  fi
done
((copied > 0)) || echo "warning: no HoldUp files in $SECRETS_DIR" >&2

db="$DATA_DIR/holdup.sqlite3"
if [[ -f $db ]]; then
  python3 - "$db" "$staging/holdup.sqlite3" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=30)
target = sqlite3.connect(sys.argv[2])
source.backup(target)
target.close()
source.close()
PY
else
  echo "note: no database at $db (the offline rehearsal keeps it in memory)" >&2
fi

archive="$DEST/holdup-$STAMP.tar.gz"
tar -C "$staging" -czf "$archive.partial" .
chmod 600 -- "$archive.partial"
mv -- "$archive.partial" "$archive"
echo "$archive ($(du -h -- "$archive" | cut -f1)): $(cd "$staging" && find . -type f | sort | tr '\n' ' ')"
