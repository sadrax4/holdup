#!/usr/bin/env bash
# Build the HoldVault and HUSD demo token contracts into res/ and record their hashes.
#
#   scripts/build_contracts.sh            build with cargo-near (falls back to plain cargo)
#   scripts/build_contracts.sh --test     run the contracts' unit tests first
#   scripts/build_contracts.sh --plain    build with plain cargo even if cargo-near is installed
#
# Outputs: res/holdvault.wasm, res/demo_token.wasm, res/SHA256SUMS (hex) and res/CODE_HASHES
# (base58 of the sha256, as RPC view_account reports code_hash). Every wasm file is checked
# against nearcore's preparation rules by contracts/tools/inspect_wasm.py.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTRACTS="$ROOT/contracts"
RES="$ROOT/res"
TARGET_PROTOCOL="${TARGET_PROTOCOL:-86}"

run_tests=0
plain=0
for arg in "$@"; do
  case "$arg" in
    --test) run_tests=1 ;;
    --plain) plain=1 ;;
    -h | --help)
      sed -n '2,11p' "$0"
      exit 0
      ;;
    *)
      echo "unknown option: $arg" >&2
      exit 2
      ;;
  esac
done

# Use the toolchain pinned in rust-toolchain.toml. If only `stable` is installed and it is that
# exact version, use it instead of letting rustup download a second copy.
pinned="$(sed -n 's/^channel *= *"\(.*\)"/\1/p' "$CONTRACTS/rust-toolchain.toml")"
if [[ -z "${RUSTUP_TOOLCHAIN:-}" ]] && ! rustup toolchain list | grep -q "^$pinned"; then
  if rustc +stable --version 2>/dev/null | grep -q "^rustc $pinned "; then
    export RUSTUP_TOOLCHAIN=stable
  fi
fi
echo "rustc: $(cd "$CONTRACTS" && rustc --version)"

if [[ $run_tests -eq 1 ]]; then
  (cd "$CONTRACTS" && cargo test --locked --workspace)
fi

out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT

if [[ $plain -eq 0 ]] && cargo near --version >/dev/null 2>&1; then
  echo "builder: $(cargo near --version)"
  for crate in holdvault demo-token; do
    (cd "$CONTRACTS/$crate" && cargo near build non-reproducible-wasm --locked --out-dir "$out")
  done
else
  # Plain cargo: `--cfg near` selects near-sdk's on-chain host functions (cargo-near sets it too).
  echo "builder: cargo $(cargo --version | cut -d' ' -f2) (plain, no wasm-opt, no embedded ABI)"
  (cd "$CONTRACTS" && RUSTFLAGS="--cfg near -C link-arg=-s" \
    cargo build --locked --release --target wasm32-unknown-unknown -p holdvault -p demo-token)
  cp "$CONTRACTS/target/wasm32-unknown-unknown/release/holdvault.wasm" "$out/"
  cp "$CONTRACTS/target/wasm32-unknown-unknown/release/demo_token.wasm" "$out/"
fi

mkdir -p "$RES"
install -m 0644 "$out/holdvault.wasm" "$RES/holdvault.wasm"
install -m 0644 "$out/demo_token.wasm" "$RES/demo_token.wasm"

vault_methods="new,deposit_near_for,ft_on_transfer,send,cancel,guardian_cancel,extend_hold,release,on_payout"
vault_methods+=",add_trusted,remove_trusted,set_guardian,veto_guardian_change,get_config,get_hold,get_holds_for"
vault_methods+=",get_active_holds"
vault_methods+=",get_due_holds,get_balance,get_user,get_payee_stats,is_registered,get_solvency"
token_methods="new,mint,ft_transfer,ft_transfer_call,ft_resolve_transfer,ft_total_supply,ft_balance_of"
token_methods+=",ft_metadata,storage_deposit,storage_withdraw,storage_unregister,storage_balance_bounds"
token_methods+=",storage_balance_of"

python3 "$CONTRACTS/tools/inspect_wasm.py" --protocol "$TARGET_PROTOCOL" \
  --expect "holdvault.wasm:$vault_methods" --expect "demo_token.wasm:$token_methods" \
  --write-hashes "$RES" "$RES/holdvault.wasm" "$RES/demo_token.wasm"

if command -v wasm-tools >/dev/null 2>&1; then
  # The exact wasm feature set nearcore enables from protocol 84 on.
  features="-all,floats,mutable-global,sign-extension,saturating-float-to-int,bulk-memory,reference-types"
  for wasm in "$RES/holdvault.wasm" "$RES/demo_token.wasm"; do
    wasm-tools validate --features="$features" "$wasm"
  done
  echo "wasm-tools: both files validate with nearcore's feature set"
fi

echo
cat "$RES/SHA256SUMS" "$RES/CODE_HASHES"
