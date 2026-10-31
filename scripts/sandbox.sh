#!/usr/bin/env bash
# Local NEAR sandbox for integration tests. Downloads the official nearcore sandbox
# build (pinned version, sha256-checked), runs it on 127.0.0.1:3030 and runs the tests
# marked "sandbox" against it. The chain is initialised like the nearprotocol/sandbox
# image (root account "sandbox", test seed "sandbox"), so near-kit's helpers work as-is.
#
# Usage: scripts/sandbox.sh install | start | stop | status | test [pytest args]
set -euo pipefail
cd "$(dirname "$0")/.."

version=2.14.0
url="https://s3-us-west-1.amazonaws.com/build.nearprotocol.com/nearcore/Linux-x86_64/${version}/near-sandbox.tar.gz"
base="${HOLDUP_SANDBOX_DIR:-$HOME/.cache/holdup/near-sandbox/$version}"
bin="$base/bin/near-sandbox"
home="$base/home"
sums=scripts/near-sandbox.sha256
rpc=127.0.0.1:3030

install() {
  if [[ -x "$bin" ]]; then echo "near-sandbox $version already installed at $bin"; return; fi
  mkdir -p "$base/bin"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  curl -fsS --max-time 600 -o "$tmp/near-sandbox.tar.gz" "$url"
  sum="$(sha256sum "$tmp/near-sandbox.tar.gz" | cut -d' ' -f1)"
  if [[ -f "$sums" ]]; then
    expected="$(cut -d' ' -f1 "$sums")"
    [[ "$sum" == "$expected" ]] || { echo "sha256 mismatch: got $sum, expected $expected" >&2; exit 1; }
    echo "sha256 verified: $sum"
  else
    echo "$sum  near-sandbox-$version.tar.gz" > "$sums"
    echo "sha256 recorded in $sums (first download): $sum"
  fi
  tar -xzf "$tmp/near-sandbox.tar.gz" -C "$tmp"
  found="$(find "$tmp" -type f -name near-sandbox | head -1)"
  [[ -n "$found" ]] || { echo "near-sandbox binary not found in the archive" >&2; exit 1; }
  install -m 0755 "$found" "$bin"
  "$bin" --version || true
}

running() { curl -fsS --max-time 2 "http://$rpc/status" >/dev/null 2>&1; }

start() {
  install
  if running; then echo "sandbox already running on $rpc"; return; fi
  if [[ ! -f "$home/config.json" ]]; then
    "$bin" --home "$home" init --chain-id localnet --account-id sandbox --test-seed sandbox >"$base/init.log" 2>&1
  fi
  nohup "$bin" --home "$home" run --rpc-addr "$rpc" --network-addr 127.0.0.1:3031 >"$base/run.log" 2>&1 &
  echo $! > "$base/pid"
  for _ in $(seq 1 90); do
    if running; then echo "sandbox running on $rpc (pid $(cat "$base/pid"))"; return; fi
    sleep 1
  done
  echo "sandbox did not start; last log lines:" >&2
  tail -20 "$base/run.log" >&2
  exit 1
}

stop() {
  if [[ -f "$base/pid" ]] && kill "$(cat "$base/pid")" 2>/dev/null; then echo "sandbox stopped"; else echo "sandbox not running"; fi
  rm -f "$base/pid"
}

cmd="${1:-help}"
shift || true
case "$cmd" in
  install) install ;;
  start)   start ;;
  stop)    stop ;;
  status)  if running; then echo "running on $rpc"; else echo "not running"; fi ;;
  test)    start; NEAR_SANDBOX_URL="http://$rpc" exec .venv/bin/pytest -m sandbox "$@" ;;
  *) sed -n '2,8p' "$0"; exit 1 ;;
esac
