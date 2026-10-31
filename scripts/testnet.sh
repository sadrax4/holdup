#!/usr/bin/env bash
# NEAR testnet helper. It only ever talks to NEAR testnet: the network is forced to testnet, the
# RPC must be a public testnet endpoint (FastNEAR's by default) and account ids must end in .testnet.
#
# Usage: scripts/testnet.sh <command> [args]
#   status                       show the configured accounts and their balances
#   budget [root] [--profile P]  what deploying profile P (hosted by default) under root takes, line by line
#   bootstrap [root]             reserve the root account (default holdup.testnet) with faucet money,
#                                then top it up to the hosted deployment's budget
#   topup <account> --min <NEAR> ask the faucet (at most 4 times, 20 s apart) until the account holds NEAR
#   faucet <account>             request 5 testnet NEAR from the official faucet API for an account
#   deploy|verify-deploy|prove|attack|demo [args]
#                                run the matching holdup command against testnet
#   test [pytest args]           run the tests marked "testnet"
set -euo pipefail
cd "$(dirname "$0")/.."

export HOLDUP_NETWORK=testnet
rpc="${NEAR_RPC_URL:-https://test.rpc.fastnear.com}"
case "$rpc" in
  https://test.rpc.fastnear.com|https://test.rpc.fastnear.com/*) ;;
  https://rpc.testnet.fastnear.com|https://rpc.testnet.fastnear.com/*) ;;
  https://rpc.testnet.near.org|https://rpc.testnet.near.org/*) ;;
  https://archival-rpc.testnet.near.org|https://archival-rpc.testnet.near.org/*) ;;
  *) echo "refusing: NEAR_RPC_URL must be a NEAR testnet endpoint (got $rpc)" >&2; exit 2 ;;
esac
export NEAR_RPC_URL="$rpc"
root="${HOLDUP_ROOT_ACCOUNT:-holdup.testnet}"
case "$root" in
  *.testnet) ;;
  *) echo "refusing: root account must end with .testnet (got $root)" >&2; exit 2 ;;
esac
creds="${HOLDUP_CREDENTIALS:-.holdup/credentials.json}"
if [[ -f $creds ]] && ! grep -q '"network": *"testnet"' "$creds"; then
  echo "refusing: $creds is not a testnet credentials file" >&2
  exit 2
fi

py=.venv/bin/python
cmd="${1:-help}"
shift || true
case "$cmd" in
  status)        exec "$py" scripts/testnet_tools.py status "$@" ;;
  budget)        exec "$py" scripts/testnet_tools.py budget "$@" ;;
  bootstrap)     exec "$py" scripts/testnet_tools.py bootstrap "${1:-$root}" ;;
  topup)         exec "$py" scripts/testnet_tools.py topup "$@" ;;
  faucet)        exec "$py" scripts/testnet_tools.py faucet "$@" ;;
  deploy)        exec "$py" -m holdup deploy --network testnet "$@" ;;
  verify-deploy) exec "$py" -m holdup verify-deploy "$@" ;;
  prove)         exec "$py" -m holdup prove "$@" ;;
  attack)        exec "$py" -m holdup attack "$@" ;;
  demo)          exec "$py" -m holdup demo --network testnet "$@" ;;
  test)          HOLDUP_TESTNET=1 exec .venv/bin/pytest -m testnet "$@" ;;
  *) sed -n '2,16p' "$0"; exit 1 ;;
esac
