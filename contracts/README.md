# HoldUp contracts

Two NEAR contracts, built from one cargo workspace:

| Crate | Output | What it is |
|---|---|---|
| `holdvault` | `res/holdvault.wasm` | **HoldVault**, the keyless payments vault. Payments to payees the sender has not trusted yet wait in a *hold*; oracles can only make a hold longer; only the sender or their guardian can cancel; anyone can release a due hold. |
| `demo-token` | `res/demo_token.wasm` | **HUSD**, "HoldUp Demo Dollar": a plain NEP-141 + NEP-145 token (6 decimals) used on sandbox and testnet when Circle USDC is not configured. It has no value and is not USDC. |

What the vault guarantees:

- **The wait only grows.** `release_at_ms` changes only in `extend_hold`, only upwards, and never past `created_ms + max_hold_ms`. No method shortens a hold and nobody can release one early.
- **The AI has the brakes, not the keys.** An oracle can call `extend_hold` and nothing else that matters: it cannot release, cancel, redirect or move money.
- **Money goes where the sender said, or back.** Funds leave a hold only to the payee fixed in `send`, or return to the sender's vault balance (cancel, failed payout).
- **No keys, no owner, no upgrade.** The configuration is fixed in `new`; there is no setter, no admin method and no code to deploy new code. The deploy deletes the vault account's only key.
- **Exits always work.** Every open hold reserves the storage and deposits it may still need, so a low reserve can block new payments but never a brake, cancel, release or payout callback.

## Build

```bash
scripts/build_contracts.sh            # cargo-near, then checks and hashes
scripts/build_contracts.sh --test     # run the unit tests first
scripts/build_contracts.sh --plain    # plain cargo instead of cargo-near
```

The script writes `res/holdvault.wasm`, `res/demo_token.wasm`, `res/SHA256SUMS` (hex, `sha256sum -c` format) and `res/CODE_HASHES` (base58 of the sha256, which is what RPC `view_account` returns as `code_hash`), then checks both files with `contracts/tools/inspect_wasm.py` and, if installed, `wasm-tools validate`.

Exact toolchain used for the files in `res/`:

| Tool | Version |
|---|---|
| rustc / cargo | 1.97.0 (`rust-toolchain.toml`; the script uses an installed `stable` of the same version instead of downloading it) |
| cargo-near | 0.22.0, installed with `cargo install --locked cargo-near --version 0.22.0 --no-default-features` (the default `ledger` feature needs libudev) |
| wasm-opt | binaryen 116 inside cargo-near (`-Os`, `--enable-bulk-memory --enable-nontrapping-float-to-int`) |
| near-sdk, near-contract-standards | 5.29.1 (exact pins; `Cargo.lock` committed) |
| Build command | `cargo near build non-reproducible-wasm --locked --out-dir <dir>` in each crate |

| File | Size | sha256 | code_hash (base58) |
|---|---|---|---|
| `holdvault.wasm` | 320,516 B | `883598aab29962783b20a322ba15a64b2656885de6bed883c657c0cf2dbeed96` | `AAhrAwh2AJbYvywbjzagsenG5s8uxSyAoZCdV7PPjL4D` |
| `demo_token.wasm` | 153,866 B | `b4985bc0277a5a4cf7c1a47943202661c2a1f6b8107f874766b7946a27ac9331` | `D9y8fWuzUCCM9ugFaSZJ1QfVNnDCsCBUvWJCN7RXfZq2` |

Rebuilding in another directory with the same toolchain produced byte-identical files. The build is `non-reproducible-wasm` in cargo-near's sense (no pinned Docker image), so a different machine or toolchain can produce different bytes; `res/CODE_HASHES` is the reference for the deployed code.

**Why near-sdk 5.29.1 and not 5.30.0.** 5.30.0 builds cleanly, but every contract built with it imports the host function `promise_batch_action_universal_state_init` (near-sdk's promise API references it even when unused). nearcore only provides it from protocol 88 (`universal_accounts`, nearcore 2.14), and a contract with an unknown import cannot be instantiated at all. On 2026-10-31 testnet runs protocol 88 but mainnet runs 86, and nearcore 2.13.x sandboxes (the default `nearprotocol/sandbox:2.13.4` image of near-kit) run 86. 5.29.1 needs protocol 85 and runs on all of them. Moving to 5.30 later is a one-line change; `inspect_wasm.py` then reports "needs protocol 88".

**Plain cargo fallback.** Without cargo-near the script runs `RUSTFLAGS="--cfg near -C link-arg=-s" cargo build --locked --release --target wasm32-unknown-unknown -p holdvault -p demo-token`. `--cfg near` is required with near-sdk 5.27+: without it near-sdk compiles pure-Rust fallbacks instead of host calls (panic messages, for example, would never reach the chain). That output is larger (388 KB, no wasm-opt, no embedded ABI) and passes the same checks.

### What was checked in the wasm

`contracts/tools/inspect_wasm.py` (standard library only) decodes every section and every instruction and applies the rules nearcore uses when it prepares a contract (`runtime/near-vm-runner/src/prepare/prepare_v3.rs`, `features.rs`, `imports.rs` and the runtime parameters at nearcore 2.13.4 and master):

| Check | holdvault | demo_token | nearcore limit |
|---|---|---|---|
| wasm features used | bulk memory, sign extension | same | MVP + mutable globals + sign-ext + sat. float-to-int; bulk memory and reference types from protocol 84; no multi-value, SIMD, threads, tail calls, exceptions, memory64, multi-memory, GC |
| imports | 40, all functions from `env` | 36 | only known `env` host functions; protocol gates per function |
| minimum protocol | 85 (gas-key host functions) | 85 | testnet 88, mainnet 86 |
| size | 320,516 B | 153,866 B | 4 MiB |
| functions / types | 924 / 59 | 468 / 40 | 10,000 / 1,024 |
| tables / elements / globals | 1 / 136 / 1 | 1 / 138 / 1 | 1 / 10,000 / 65,536 |
| largest function body | 14,042 B | 14,042 B | 196,608 B |
| locals | 3,387 | 1,261 | min(1,000,000, size / 2) |
| params per function / total | 9 / 2,145 | 11 / 984 | 64 / 50,000 |
| blocks per function / total | 303 / 5,931 | 303 / 2,748 | 5,000 / 50,000 |
| peak operand stack | 80 B | 80 B | 8,192 B |
| start function, extra memories | none | none | |

`wasm-tools validate --features=-all,floats,mutable-global,sign-extension,saturating-float-to-int,bulk-memory,reference-types` (wasm-tools 1.263.0, the same wasmparser feature set nearcore configures) accepts both files; without `bulk-memory` it rejects them, which is expected for rustc 1.87+ and why protocol 84+ is required. The inspector was also run against hand-written modules using SIMD, multi-value, tail calls, atomics, a foreign import, an unknown host function, a protocol-88 host function and an 8,800-byte operand stack: it rejected each one.

Not verified here: deploying and calling the files on a live node (no sandbox binary or Docker in the build environment). `tests/test_contract_sandbox.py` does exactly that against a sandbox you start.

## Configuration (`new`)

`new(config: ConfigInput)` can be called once, by the vault account itself or by its parent account (the deployer, normally batching `CreateAccount`, `DeployContract` and `new` in one transaction). Anything else panics with `E_BAD_CONFIG`.

| Field | JSON | Rule |
|---|---|---|
| `oracles` | account ids | 1 to 3, unique, not the vault |
| `tokens` | account ids | 0 to 4, unique, not the vault, not named `near` |
| `new_payee_hold_ms` | U64 string | > 0 |
| `max_hold_ms` | U64 string | >= `new_payee_hold_ms`, at most one year |
| `trust_delay_ms`, `guardian_delay_ms` | U64 strings | at most one year |
| `instant_caps` | `{asset_id: U128}` | keys are `near` or a configured token; a missing asset has cap 0 |
| `max_active_holds` | number | 1 to 100 |
| `registration_fee` | U128 string | any; kept as storage reserve (a user record is about 220 bytes = 0.0022 NEAR) |

Unknown fields are rejected. `get_config` returns the same fields plus `"version": "1.0.0"`.

## Methods

Amounts and times are strings (`U128`, `U64`); `u32` values are numbers. The asset id is `"near"` or the token's account id. Only `deposit_near_for` accepts a deposit; every other change method panics if NEAR is attached, so function-call access keys can drive them.

| Method | Who | Effect | Errors |
|---|---|---|---|
| `deposit_near_for(account_id) -> U128` | anyone (payable) | Credits the attached NEAR. An unregistered account is registered first: needs `registration_fee` (kept by the vault) and the caller must be the account itself or its parent. Returns the new NEAR balance. | `E_NOT_REGISTERED`, `E_FEE_TOO_LOW`, `E_INSOLVENT` |
| `ft_on_transfer(sender_id, amount, msg)` | allowed token | Credits `sender_id`, or `credit_to` from `msg = {"credit_to":"<account>"}`. Returns the full amount (refund) if that account is not registered. | `E_NOT_TOKEN`, `E_BAD_MSG` |
| `send(payee, asset, amount, memo_hash?) -> {hold}` | registered user | Moves `amount` into a hold. A payee trusted since at least `trust_delay_ms`, within today's instant cap, is paid at once (`instant: true`); any other payment waits `new_payee_hold_ms`. Attach 100 Tgas (an instant token payment pays out in the same call). | `E_NOT_REGISTERED`, `E_ZERO_AMOUNT`, `E_UNKNOWN_ASSET`, `E_SELF_PAYMENT`, `E_INSUFFICIENT_BALANCE`, `E_TOO_MANY_HOLDS`, `E_MEMO_TOO_LONG`, `E_INSOLVENT` (checked in this order) |
| `cancel(hold_id) -> HoldView` | the sender | Returns a held payment to the sender's balance. | `E_HOLD_NOT_FOUND`, `E_NOT_SENDER`, `E_HOLD_NOT_ACTIVE` |
| `guardian_cancel(hold_id) -> HoldView` | the sender's guardian in charge | Same as `cancel`; the money still goes to the sender. | `E_HOLD_NOT_FOUND`, `E_NOT_GUARDIAN`, `E_HOLD_NOT_ACTIVE` |
| `extend_hold(hold_id, release_at_ms, reason_bits, evidence) -> HoldView` | an oracle | Raises `release_at_ms`, ORs `reason_bits` in, counts the extension and stores `sha256(evidence)` as lowercase hex. The first extension flags the hold and the payee. | `E_NOT_ORACLE`, `E_HOLD_NOT_FOUND`, `E_HOLD_NOT_ACTIVE`, `E_ONLY_LONGER`, `E_ABOVE_MAX_HOLD`, `E_EVIDENCE_TOO_LONG` (> 4096 bytes), `E_NO_REASON` |
| `release(hold_id) -> HoldView` | anyone (the keeper) | Pays a due hold: internally if the payee is a vault user, otherwise by NEAR transfer or `storage_deposit` + `ft_transfer`, settled by `on_payout`. Attach 100 Tgas. | `E_HOLD_NOT_FOUND`, `E_HOLD_NOT_ACTIVE`, `E_NOT_DUE` |
| `on_payout(hold_id) -> HoldView` | the vault only (`#[private]`) | Settles an external payout: success -> `released`; failure -> the sender is refunded, `failed`. Never panics for lack of funds. | `E_HOLD_NOT_ACTIVE` |
| `add_trusted(payee) -> {payee, effective_ms}` | registered user | Trusts `payee` from `now + trust_delay_ms`. Re-adding keeps the existing entry. At most 50. | `E_NOT_REGISTERED`, `E_SELF_PAYMENT`, `E_TOO_MANY_TRUSTED`, `E_INSOLVENT` |
| `remove_trusted(payee) -> bool` | registered user | Removes at once; false if not trusted. | `E_NOT_REGISTERED` |
| `set_guardian(guardian?) -> UserView` | registered user | With no guardian in charge and nothing pending: takes effect now. Otherwise it waits `guardian_delay_ms`, the current guardian stays in charge, and a newer call replaces the pending change. `null` removes the guardian. | `E_NOT_REGISTERED`, `E_BAD_GUARDIAN`, `E_INSOLVENT` |

Views: `get_config()`, `get_hold(hold_id) -> HoldView?`, `get_holds_for(account_id, limit?)` (holds sent by the account, newest first, default 50, max 100), `get_active_holds(account_id)` (held, oldest first), `get_due_holds(limit?)` (held and due, earliest first, default 50, max 100), `get_balance(account_id) -> {near, tokens: {token: amount}}` (zeros when unknown), `get_user(account_id) -> UserView?`, `get_payee_stats(account_id)`, `is_registered(account_id)`, `get_solvency()`.

`HoldView`: `{id, sender, payee, asset, amount, created_ms, release_at_ms, status: "held"|"releasing"|"released"|"cancelled"|"failed", instant, flagged, extensions, reason_bits, evidence_hash, memo_hash, settled_ms, cancelled_by}`. `UserView`: `{account_id, registered_ms, guardian, pending_guardian: {guardian, effective_ms}?, trusted: [{payee, effective_ms}], active_holds}`; `guardian` is the guardian in charge now (a due pending change counts as applied). `PayeeStatsView`: `{holds, flagged, cancelled, cancelled_after_flag, released, last_flag_ms}` (holds and released count held payments only, not instant ones). `get_solvency`: `{account_balance, locked, near_liabilities, settlement_reserve, storage_usage, storage_cost, surplus}`.

Hold lifecycle: `held -> released` (internal payout) · `held -> releasing -> released | failed` (external payout) · `held -> cancelled`. Only `held` can be extended, cancelled or released.

Gas constants: `storage_deposit` 10 Tgas, `ft_transfer` 15 Tgas (both without a share of leftover gas), `on_payout` 20 Tgas plus all leftover gas. `on_payout` is chained on the transfer alone (for tokens `storage_deposit -> ft_transfer -> on_payout`), so its promise result 0 is the transfer's outcome; it reads no returned bytes.

## Events

NEP-297 logs: `EVENT_JSON:{"standard":"holdup","version":"1.0.0","event":"<name>","data":[{...}]}`. Hold events always carry `hold_id`, `sender`, `payee`, `asset`, `amount`. Events are logged only after all checks of a call pass.

| Event | Extra fields |
|---|---|
| `user_registered` | `account_id`, `registered_ms`, `fee` |
| `deposit` | `account_id`, `asset`, `amount`, `balance` (new balance) |
| `hold_created` | `created_ms`, `release_at_ms`, `instant`, `memo_hash` |
| `hold_extended` | `previous_release_at_ms`, `release_at_ms`, `reason_bits` (all reasons so far), `evidence_hash`, `extensions` |
| `hold_cancelled` | `by` (`"sender"` or `"guardian"`), `cancelled_by`, `flagged` |
| `hold_released` | `internal`, `instant` |
| `hold_failed` | `refunded_to` |
| `trust_added` | `owner`, `payee`, `effective_ms` |
| `trust_removed` | `owner`, `payee` |
| `guardian_changed` | `owner`, `guardian` (null = removed), `effective_ms` |

## Error codes

Every panic is `"E_CODE: human text"`: `E_NOT_REGISTERED`, `E_NOT_SENDER`, `E_NOT_GUARDIAN`, `E_NOT_ORACLE`, `E_NOT_TOKEN`, `E_HOLD_NOT_FOUND`, `E_HOLD_NOT_ACTIVE`, `E_NOT_DUE`, `E_ONLY_LONGER`, `E_ABOVE_MAX_HOLD`, `E_EVIDENCE_TOO_LONG`, `E_NO_REASON`, `E_INSUFFICIENT_BALANCE`, `E_UNKNOWN_ASSET`, `E_ZERO_AMOUNT`, `E_SELF_PAYMENT`, `E_TOO_MANY_HOLDS`, `E_FEE_TOO_LOW`, `E_INSOLVENT`, `E_TOO_MANY_TRUSTED`, `E_BAD_GUARDIAN`, `E_BAD_CONFIG`, `E_MEMO_TOO_LONG`, plus `E_BAD_MSG` (malformed `ft_on_transfer` message) and `E_OVERFLOW` (checked arithmetic; unreachable with real balances). On chain the message reads `Smart contract panicked: E_CODE: ...`. near-sdk's own guards use their own texts: `Method on_payout is private`, `Method send doesn't accept deposit`, `The contract has already been initialized`.

## Accounting and solvency

- `near_liabilities` = every user's NEAR balance + NEAR in `held` holds. When a NEAR payout leaves, the amount leaves the account balance and the liabilities together; a failed payout adds it back when the sender is refunded.
- `settlement_reserve`: each open hold reserves what it can still cost the vault, so that exits never need new funds: storage it can still add (`HOLD_SETTLEMENT_BYTES` = 116 bytes: evidence hash, settled time, canceller, payee flag time) and, for token holds, the 0.00125 NEAR `storage_deposit` plus 1 yoctoNEAR a payout attaches.
- Methods that add storage or obligations (registration, `send`, `add_trusted`, `set_guardian`) end with: `account_balance + locked >= near_liabilities + settlement_reserve + storage_usage * byte_cost`, else `E_INSOLVENT`. The attached deposit is already part of `account_balance`. Store collections are flushed first, so `storage_usage` includes the call's own writes.
- Methods that only move existing money (`cancel`, `guardian_cancel`, `extend_hold`, `release`, `on_payout`, `ft_on_transfer`, deposits to registered users) do not check: their storage is covered by the reserve, so they cannot be blocked.
- Settled holds stay in storage as history (about 300 bytes each), paid from the reserve. A reserve of 1 NEAR covers roughly 300 holds; when it runs low, new holds are refused with `E_INSOLVENT` while everything already in the vault can still be cancelled, braked, released and paid out. Anyone can top up the reserve with a plain transfer to the vault account.
- Known transient: a failed NEAR transfer comes back as a separate refund receipt. If `on_payout` runs before that receipt, the vault is short by that amount for a block or two; a storage-adding call in that window can fail with `E_INSOLVENT` unless the surplus covers it.

Measured storage (unit tests, realistic account names): contract state 281 B, registration 222 B, a hold 376 B (464 B when the payee is new), an extension 40 B, a trusted payee 34 B.

## Invariants and the tests that check them

Unit tests live in `holdvault/src/tests/`. `Sim` runs every call like the chain: state is read from storage, written back and flushed, failed calls leave storage untouched, and the vault's balance and storage usage carry over between calls (nearcore's own host functions back the mock, so promise deposits and storage really change them).

1. Only an oracle can extend a hold: `extending::only_oracles_can_extend`, `extending::every_configured_oracle_can_extend`.
2. A hold only gets longer: `extending::holds_only_get_longer`.
3. Never past `created_ms + max_hold_ms`, even after waiting: `extending::extensions_stop_at_the_max_hold`.
4. The evidence hash is sha256 of the exact bytes (checked against Python's hashlib) and evidence is limited to 4096 bytes: `extending::extension_records_reasons_and_the_evidence_hash`, `extending::evidence_is_limited_to_4096_bytes`.
5. A reason is required; reasons accumulate; a hold flags its payee once: `extending::a_reason_is_required`, `extending::extension_records_reasons_and_the_evidence_hash`.
6. Settled or missing holds cannot be extended; a due but unreleased hold can still be braked: `extending::missing_or_settled_holds_cannot_be_extended`, `extending::a_due_hold_can_still_be_braked_before_release`.
7. No method other than `extend_hold` changes `release_at_ms`, and none lowers it: `properties::random_operations_keep_every_invariant`.
8. Nobody can release early, to the millisecond: `releasing::nobody_can_release_early`.
9. A hold settles once; no double release, no cancel or extension while a payout is in flight: `cancelling::a_hold_settles_only_once`, `cancelling::nothing_can_cancel_a_payout_in_flight`, `releasing::near_release_to_an_outside_account`.
10. Only the sender or their guardian in charge can cancel, and the money returns to the sender: `cancelling::only_the_sender_or_guardian_can_cancel`, `guardians::the_first_guardian_takes_effect_at_once`, `guardians::a_guardian_only_guards_their_own_user`.
11. A payout to a registered user is internal (no promise): `releasing::release_to_a_vault_user_is_internal`, `releasing::payee_that_registers_later_is_paid_internally`, `trust::a_trusted_payee_waits_for_the_trust_delay`.
12. External payouts mark the hold `releasing` before any promise, chain `on_payout` on the transfer only (result index 0), and attach the documented deposits and gas: `releasing::near_release_to_an_outside_account`, `releasing::token_release_registers_the_payee_then_transfers`.
13. A failed NEAR or token payout refunds the sender: `releasing::failed_near_payout_refunds_the_sender`, `releasing::failed_token_payout_refunds_the_sender`; returned bytes are ignored: `releasing::transfer_return_data_does_not_matter`; the callback needs a payout in flight: `releasing::payout_callback_needs_a_payout_in_flight`.
14. Trust takes effect only after the delay; re-adding never restarts or shortens it; removal is immediate: `trust::a_trusted_payee_waits_for_the_trust_delay`, `trust::re_adding_never_restarts_or_shortens_the_delay`, `trust::removal_is_immediate_and_trusting_again_restarts_the_delay`, `trust::the_trust_list_is_limited`.
15. The instant cap is per sender, per asset and per UTC day, resets exactly at midnight, is never exceeded, and held payments do not use it; a missing cap means never instant: `trust::instant_payments_are_capped_per_sender_asset_and_utc_day`, `trust::held_payments_do_not_use_the_cap`, `trust::an_asset_without_a_cap_is_never_instant`.
16. Guardian changes wait for the delay while the current guardian stays in charge; a newer change replaces the pending one with a full delay; removal waits too: `guardians::a_guardian_change_waits_and_the_current_guardian_stays_in_charge`, `guardians::a_newer_change_replaces_the_pending_one_with_a_full_delay`, `guardians::removing_the_guardian_waits_too`, `guardians::guardian_rules`.
17. Payee statistics (holds, flagged, cancelled, cancelled after a flag, released, last flag time): `cancelling::cancelling_after_a_flag_counts_against_the_payee`, `cancelling::sender_cancels_and_gets_the_money_back`, `trust::a_trusted_payee_waits_for_the_trust_delay`.
18. Token deposits come only from allowed tokens; unknown targets get a refund; malformed messages are rejected: `deposits::token_deposits_come_only_from_allowed_tokens`, `deposits::token_deposit_for_an_unknown_account_is_returned`, `deposits::malformed_transfer_messages_are_rejected`, `deposits::token_deposit_credits_the_sender_or_credit_to`.
19. Registration needs the fee and the account's own or its parent's call; the fee stays as reserve: `deposits::registration_needs_the_fee`, `deposits::only_the_account_or_its_parent_can_register_it`, `deposits::first_deposit_registers_and_keeps_the_fee`.
20. `send` validates every input and the per-sender active-hold limit: `sending::send_validates_every_input`, `sending::senders_have_a_limit_on_held_payments`.
21. Arithmetic is checked: `deposits::balance_overflow_is_refused`, `sending::send_validates_every_input` (u128::MAX amount).
22. Liabilities equal user balances plus held NEAR, tokens are conserved, and the settlement reserve matches open holds after every step: `properties::random_operations_keep_every_invariant`, `solvency::every_open_hold_reserves_what_it_needs_to_settle`.
23. The vault stays solvent after every step, and the last users can send everything out while the vault keeps exactly its reserve: `solvency::the_last_users_can_withdraw_everything`, `properties::random_operations_keep_every_invariant`.
24. A low reserve blocks new holds but never brakes, cancels, releases or payout callbacks: `solvency::a_low_reserve_blocks_new_holds_but_never_exits`, `solvency::registration_is_refused_when_nothing_pays_for_storage`.
25. The configuration is validated and fixed, and only the vault or its deployer can initialize it: `config::new_rejects_invalid_configurations`, `config::config_json_rejects_unknown_fields`, `config::only_the_vault_or_its_deployer_can_initialize`.
26. Status changes only along `held -> releasing -> released | failed`, `held -> released`, `held -> cancelled`; active-hold indexes and the due list always agree with statuses: `properties::random_operations_keep_every_invariant`.

The property test runs 12 seeded sequences of 400 random calls (deposits, sends to users and outsiders in both assets, valid and invalid extensions by oracles and strangers, cancels, guardian cancels, releases, successful and failed payouts with the refund before or after the callback, trust and guardian changes, waits across UTC midnight) and checks items 7, 22, 23 and 26 after every call.

The token's own tests are in `demo-token/src/lib.rs` (supply, metadata, owner-only mint, transfer, init guard).

## Tests

```bash
cd contracts && cargo test --workspace      # 68 vault tests + 6 token tests, about 5 s
cargo clippy --workspace --all-targets && cargo fmt --all --check
# Sandbox (start a sandbox first; the test never starts one):
NEAR_SANDBOX_URL=http://127.0.0.1:3030 .venv/bin/pytest -m sandbox tests/test_contract_sandbox.py
```

If `rustup` tries to download 1.97.0 although `stable` is 1.97.0, run with `RUSTUP_TOOLCHAIN=stable` (the build script does this by itself).

The sandbox test deploys both files from `res/` the way production does (create account, deploy and `new` in one transaction from the parent, then delete the vault's key), checks the code hash against `res/CODE_HASHES`, and runs registration, token deposit, a braked and cancelled payment, NEAR and token releases to an outside account, a failed payout refund, a guardian cancel, an instant internal payment to a trusted user, the private callback and deposit guards, and solvency. It needs a node with protocol 85+.

## Deploying (for `holdup/chain/deploy.py`)

1. One transaction from the root to the vault account: `CreateAccount`, `Transfer` (code storage is about 3.2 NEAR, plus the reserve), `AddKey` (temporary full-access key), `DeployContract`, `new`. The root is the vault's parent, so it may call `new`.
2. The same for the token: `new(owner_id, total_supply)` must come from the token account or its parent.
3. Register the vault on each token (`storage_deposit`, 0.00125 NEAR).
4. Users must be sub-accounts of the treasury that registers them (`deposit_near_for` from the parent), or register themselves.
5. Delete the vault's key; `view_access_key_list` must be empty and `view_account.code_hash` must equal `res/CODE_HASHES`.

With near-kit, `call` raises `ContractPanicError` when any receipt of the transaction failed. A `release` whose transfer fails (for example, a deleted payee account) therefore raises even though the vault settled it correctly as `failed` and refunded the sender: read the hold afterwards rather than treating that error as a failed release.

## Known limits

- A compromised user key (the HoldUp server signs with function-call keys) can trust a payee and, after `trust_delay_ms`, pay it instantly up to the daily cap per asset. Every other payment it makes waits and can be cancelled by the guardian. Caps and delays bound the loss; they do not remove it.
- An oracle can delay a payment up to `max_hold_ms` (72 h in both deploy profiles), and its flags count against the payee's statistics. It cannot move money.
- `get_due_holds` scans all held payments; fine for thousands of holds, not for millions.
- Registering is per account. Someone can register only their own account or one they created, so payouts to third-party accounts (an exchange deposit address, say) are always real transfers.
