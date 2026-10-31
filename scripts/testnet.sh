#!/usr/bin/env bash
# NEAR testnet helper. It only ever talks to NEAR testnet: the network is forced to
# testnet, the RPC is the public testnet endpoint and account ids must end in .testnet.
#
# Usage: scripts/testnet.sh <command> [args]
#   status               show the configured accounts and their balances
#   bootstrap [root]     reserve the root account (default holdup.testnet) with faucet funds
#   faucet <account>     request 5 testnet NEAR from the official faucet API for an account
#   deploy|verify-deploy|prove|attack|demo [args]
#                        run the matching holdup command against testnet
#   test [pytest args]   run the tests marked "testnet"
set -euo pipefail
cd "$(dirname "$0")/.."

export HOLDUP_NETWORK=testnet
export NEAR_RPC_URL=https://rpc.testnet.near.org
root="${HOLDUP_ROOT_ACCOUNT:-holdup.testnet}"
case "$root" in
  *.testnet) ;;
  *) echo "refusing: root account must end with .testnet (got $root)" >&2; exit 2 ;;
esac
if [[ -f .holdup/credentials.json ]] && ! grep -q '"network": *"testnet"' .holdup/credentials.json; then
  echo "refusing: .holdup/credentials.json is not a testnet credentials file" >&2
  exit 2
fi

py=.venv/bin/python
cmd="${1:-help}"
shift || true
case "$cmd" in
  status)        exec "$py" scripts/testnet_tools.py status "$@" ;;
  bootstrap)     exec "$py" scripts/testnet_tools.py bootstrap "${1:-$root}" ;;
  faucet)        exec "$py" scripts/testnet_tools.py faucet "$@" ;;
  deploy)        exec "$py" -m holdup deploy --network testnet "$@" ;;
  verify-deploy) exec "$py" -m holdup verify-deploy "$@" ;;
  prove)         exec "$py" -m holdup prove "$@" ;;
  attack)        exec "$py" -m holdup attack "$@" ;;
  demo)          exec "$py" -m holdup demo --network testnet "$@" ;;
  test)          HOLDUP_TESTNET=1 exec .venv/bin/pytest -m testnet "$@" ;;
  *) sed -n '2,13p' "$0"; exit 1 ;;
esac
