# HoldUp

**Give AI the brakes, not the keys.**

HoldUp is a Telegram payments bot on NEAR (with a web console that drives the same engine). Money
sits in HoldVault, a contract with no access keys, no owner and no upgrade path. A payment to
someone you have not trusted yet becomes a *hold*: it waits (10 minutes on a hosted bot, 20 seconds
in the demo) while HoldUp checks it. If anything looks wrong, an oracle makes the wait longer and
records why on-chain. The oracle's only key can call `extend_hold`, which can only add time, up to
72 hours after the payment. It cannot release, cancel, redirect or move money. Only the sender, or a
guardian the sender chose, can cancel, and the money then goes back to the sender. Anyone can
release a hold once its time has come.

The checks are deterministic rules (milliseconds, no model) plus open-weight models routed through
the [Otari](https://otari.ai) gateway: triage, image reading, an investigator with web search, web
fetch, code execution and HoldUp's own MCP server, a reviewer from another model family, and
guardrails. When the AI step fails or runs out of budget, the brake stays on (fail-closed).

## What the vault guarantees

- The wait only grows: `release_at_ms` changes only in `extend_hold`, only upwards, never past
  `created_ms + max_hold_ms`. Nobody can release a hold early.
- Money leaves a hold only to the payee fixed at `send`, or back to the sender.
- Trusted payees are paid instantly, but trust takes effect after a delay, and instant payments are
  capped per asset and per UTC day.
- A guardian change waits too, and the guardian in charge can veto it, so whoever holds a user's
  key cannot quietly remove the guardian first.
- Every cancel after a warning is a report against the payee (each sender counts once), so the next
  sender to that payee is braked by the rules within seconds.
- Each hold, trusted payee and guardian change pays for the storage it leaves, so nobody can use up
  the reserve the other users depend on.

Details, error codes, events and the tests behind each property: [contracts/README.md](contracts/README.md).

## How it fits together

```
 Telegram bot ──┐                                  ┌── rules (no model): payee age, reports,
 web console ───┼── engine ── risk checks ─────────┤   look-alikes, message heuristics, invoice sums
 CLI / demo ────┘     │                            └── Otari pipeline: triage, vision, investigator,
                      │                                reviewer, guardrails (open-weight models)
                      │                                     │        │
                      │                     near-intel MCP ─┘        └─ /validate guardrail endpoint
                      │                     (payee facts, history, page fetch; per-hold tokens)
                      ▼
   HoldVault (keyless contract)
     users ......... send, cancel, trust, set/veto guardian   (function-call keys, no deposits)
     oracle ........ extend_hold only                           (its single key)
     keeper ........ release only                               (its single key; anyone may release)
```

- `contracts/holdvault`: the vault (Rust, near-sdk 5.29.1); `contracts/demo-token`: HUSD, a
  6-decimal NEP-141 demo token. HUSD is not Circle USDC; the demo uses it wherever USDC is not
  configured. Built files and hashes are in `res/`.
- `holdup/engine.py`: the channel-independent payment flow (provisional brake, full verdict,
  fail-closed retries, guardian alerts). `holdup/workers/`: the oracle and the keeper.
- `holdup/risk/`: the rules, the Otari client and pipeline, the evidence builder, recorded answers.
- `holdup/mcp_server.py`, `holdup/guard.py`: the near-intel MCP server and the guardrail endpoint
  that the gateway calls back.
- `holdup/bot/`, `holdup/web/`: the Telegram bot, the web pages and the web console.
- `holdup/chain/`: the NEAR adapter, deployment, proofs, and `FakeVault`, an in-memory vault with
  the contract's exact rules, used by the tests and the offline demo.

## Otari features used

| Feature | Where |
|---|---|
| Open-weight models: `openai/gpt-oss-20b` (triage), `openai/gpt-oss-120b` (investigator), `z-ai/glm-5.3-flash` (images), `deepseek/deepseek-v4.1-flash-1017` (reviewer, another model family) | `holdup/risk/otari.py`, `holdup/risk/pipeline.py` |
| Gateway tools `otari_web_search`, `otari_web_fetch`, `otari_code_execution` (every number the investigator relies on is computed) | investigator and reviewer steps in `pipeline.py` |
| MCP servers passed per request: HoldUp's `near-intel` server with a bearer token that only covers one hold's parties | `pipeline.py`, `holdup/mcp_server.py` |
| Request guardrails: `checker-injection` in monitor mode on triage and the investigator, and a blocking gate that keeps flagged text away from the tool-using models; `gpt-oss-safeguard-120b` and `llama-guard-4-12b` behind HoldUp's guardrail endpoint | `pipeline.py`, `holdup/guard.py`, `holdup/risk/guard_models.py` |
| Response headers (`Otari-Provider`, `Otari-Model`, `Otari-Fallback`, `Otari-Attempt-Count`, `Otari-Request-ID`, `Otari-Guardrails`) and `usage.cost_usd`, shown per step on `/why/<hold>` and hashed into the on-chain evidence | `otari.py`, `holdup/web/` |
| Streaming for tool-using calls, typed errors (budget, guardrail, all candidates failed, firewall), an app-level fallback chain across model families | `otari.py` |
| Per-user keys, budgets and spend ceilings (`organizations/me/keys`, `budgets`, `spend-ceilings`) | `holdup/risk/budgets.py` |

## Try it offline

Python 3.12 and [uv](https://docs.astral.sh/uv/). Nothing below touches a network: the vault is
in memory, time is simulated and the AI steps replay recorded answers.

```bash
make setup           # uv sync --locked
make demo-offline    # the scripted story, about two seconds; also saved to docs/demo-offline.txt
make attack          # three attacks and why each one fails
make prove           # the oracle and user keys try forbidden actions; every one is refused
make serve           # web app and web console on http://127.0.0.1:8000 (open /console)
make test            # the Python tests; `make test-contract` runs the Rust tests
```

The story ([docs/demo-offline.txt](docs/demo-offline.txt)): Priya forwards a fictional "CoinHaven
Support" DM and pays the wallet it names; the rules brake the payment within seconds, the full check
adds a phishing page, public warnings and a two-hour-old payee, and Priya cancels. Dana's supplier
"Northwind Paper Co." sends an invoice with a new wallet and a total that does not add up; her
guardian cancels it. A trusted payee is paid instantly and a dinner split is released after the
wait. Finally the next senders to the reported wallet are braked by the rules before any model is
asked. Screenshots of the hold, `/why`, `/prove` and console pages are in
[docs/screenshots](docs/screenshots).

Every brake links to its evidence: `/h/<id>` shows the hold's timeline, `/why/<id>` every AI step
with model, provider, fallback, attempts and cost, and `/v/<id>.json` the evidence JSON whose
sha256 the oracle stored on-chain. `/prove` checks the deployment: the vault has no keys, its code
hash matches `res/CODE_HASHES`, and the oracle and keeper keys are limited to one method each.

## On NEAR testnet

```bash
make deploy NETWORK=testnet PROFILE=demo    # vault, HUSD, oracle and keeper; writes .holdup/credentials.json
make verify-deploy NETWORK=testnet
make demo                                   # the same story with real transactions and real waits
```

`deploy` signs with `HOLDUP_TREASURY_KEY` for `HOLDUP_ROOT_ACCOUNT` (or NEAR CLI credentials for
that account). With `OTARI_API_KEY` set the checks
call the gateway live. Without a key the scripted demo replays its recorded answers (labelled
"recorded"), and every other command on a real network runs the rules alone: recorded findings
are never put on-chain as evidence about real payments. Hosting the Telegram bot is described in
[deploy/README.md](deploy/README.md).

## Status

- Verified offline: the Python and Rust test suites, the offline demo and the attack walkthrough.
  The contracts are built with cargo-near 0.22.0, checked against nearcore's preparation rules and
  validated with wasm-tools; rebuilding gives byte-identical files.
- Not yet run: a testnet deployment, `make demo` on testnet, and the sandbox contract tests
  (`tests/test_contract_sandbox.py`, which need a running NEAR sandbox).
- Otari: individual gateway features were probed live and those responses are recorded in
  `fixtures/otari/recorded/`. The full pipeline has run here only on recorded answers
  (`fixtures/otari/*.json`, marked synthetic); it has not run live end to end.
- The Telegram bot is tested in-process against a fake Bot API; its webhook has not served real
  users yet.

## License

[MIT](LICENSE)
