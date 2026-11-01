# HoldUp

**Give AI the brakes, not the keys.**

A scam that talks someone into paying ends in a payment the victim signs themselves, and on-chain
that payment is final. HoldUp puts time between the decision and the settlement, and the sender can
take the payment back while it waits.

HoldUp is a Telegram payments bot on NEAR (with a web console that drives the same engine). Money
sits in HoldVault, a contract with no access keys, no owner and no upgrade path. A payment to
someone you have not trusted yet becomes a *hold*: it waits (5 minutes on a hosted bot, 20 seconds
in the demo) while HoldUp checks it. If anything looks wrong, an oracle makes the wait longer and
records why on-chain. The oracle's only key is a NEAR function-call access key for `extend_hold`,
which can only add time, up to 72 hours after the payment. It cannot release, cancel, redirect or
move money. Only the sender, or a guardian the sender chose, can cancel, and the money then goes
back to the sender. Anyone can release a hold once its time has come.

The checks are deterministic rules (milliseconds, no model) plus open-weight models routed through
the [Otari](https://otari.ai) gateway: triage, image reading, an investigator with web search, web
fetch, code execution and HoldUp's own MCP server, a reviewer from another model family, and
guardrails. When the AI step fails or runs out of budget, the brake stays on (fail-closed).

**Try it.** On Telegram, open [@HoldUp_HoldUp_bot](https://t.me/HoldUp_HoldUp_bot), send `/start`,
then `/demo`. Without Telegram: `make setup`, then `make demo-offline`, `make attack` and
`make prove` (no network after setup, about two seconds each). Judges:
[docs/JUDGES.md](docs/JUDGES.md) is the five-minute path.

**What has run live** (dates are UTC). On 2026-11-01 the bot's own deployment on NEAR testnet
(below) took its first two real payments through Telegram, with the AI checks live on Otari. A
payment made after the fake exchange-support sample was braked by the rules within a second, then
held for 72 hours by the full check, which had read the linked scam page through Otari's web fetch;
its sender later cancelled it. An ordinary payment to a practice friend was released by the keeper
after the 5-minute wait. On 2026-10-31 the AI checks ran on Otari for the scripted story and for
six payments through the hosted server's public URL, both on the in-memory vault, and a rehearsal
with throwaway accounts ran three held payments on testnet with the AI checks off. Each web page's
footer and `/health` say which network the bot is on. Details:
[docs/OTARI.md](docs/OTARI.md#the-live-evidence); what has not run live yet: [Status](#status).

**The bot's deployment on NEAR testnet**, live since 2026-11-01 09:08 UTC (commit 9e03a8b, hosted
profile: new payees wait 5 minutes):

| Account | Role | Access keys |
|---|---|---|
| [`vault.holdup-soleimani.testnet`](https://testnet.nearblocks.io/address/vault.holdup-soleimani.testnet) | HoldVault | none; code hash `HRS9GfZHExGfarPm4H8deFfhrNr9wpkECohBfZtb9JTy`, as in `res/CODE_HASHES` |
| [`oracle.holdup-soleimani.testnet`](https://testnet.nearblocks.io/address/oracle.holdup-soleimani.testnet) | oracle | one function-call key, for `extend_hold` on the vault |
| [`keeper.holdup-soleimani.testnet`](https://testnet.nearblocks.io/address/keeper.holdup-soleimani.testnet) | keeper | one function-call key, for `release` on the vault |
| [`husd.holdup-soleimani.testnet`](https://testnet.nearblocks.io/address/husd.holdup-soleimani.testnet) | HUSD demo token | none |
| [`holdup-soleimani.testnet`](https://testnet.nearblocks.io/address/holdup-soleimani.testnet) | root and treasury | one full-access key, on the server |

The payees of the bot's practice samples are keyless accounts under the root: `safe-wallet-77`,
`northwind-paper`, `northwindpaper`, `ravi` and `ravi-0` to `ravi-9`, each followed by
`.holdup-soleimani.testnet`. Deploy transactions include
[creating the vault](https://testnet.near.rocks/tx/8uCTx8PZnSa5mDyUNTVdsj6xxXhtg7ZSR8jgQaAujoZv),
[deploying and initialising it](https://testnet.near.rocks/tx/F886xnobuVV8wMS6ogzfpM3TCn3HBWuJvgEQMUHiGopC),
[deleting its last key](https://testnet.near.rocks/tx/4SDy5Pq3mgDArDFeJyFRHUSyTdRj3574TcZEBZ8GFcHy)
and [creating the token](https://testnet.near.rocks/tx/FnEgaseHhQZWnVuUi2uFqeD9swugv6ubGWhkvzGfEBVJ).
At install `verify-deploy` passed all five checks and `prove` saw the network refuse every
forbidden oracle and keeper transaction. To check the deployment yourself with a public RPC
endpoint: [docs/JUDGES.md](docs/JUDGES.md#check-the-deployment-on-chain).

More: [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) (keys, attacks and limits),
[docs/OTARI.md](docs/OTARI.md) (each rubric item, where it is in the code and what has run live),
[docs/WHY_NOT.md](docs/WHY_NOT.md) (why not a multisig, a delay, a blocklist or a custodian) and
[How this was built](#how-this-was-built).

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

One row per item of the Otari "Devs Wilding" rubric. "Live" means a saved record of the app
running against the hosted gateway on 2026-10-31 or 2026-11-01; request ids, costs and what is
still open are in [docs/OTARI.md](docs/OTARI.md).

| Rubric item | What HoldUp does | Live | Where |
|---|---|---|---|
| Open-weight models | `openai/gpt-oss-20b` (triage and the guardrail gate), `openai/gpt-oss-120b` (investigator), `deepseek/deepseek-v4.1-flash-1017` (reviewer, another model family), `z-ai/glm-5.3-flash` (images); `gpt-oss-safeguard-120b` classifies text behind HoldUp's guardrail endpoint | Yes: 3 models in 4 roles. Images: not yet | `holdup/risk/otari.py`, `holdup/risk/pipeline.py` |
| Model routing and fallbacks | Catalog ids, so the gateway picks the provider; provider, fallback, attempts and request id recorded per step, shown on `/why` and put in the evidence whose sha256 goes on-chain; HoldUp's own fallback chain across model families | Routing: yes. A fallback: not yet | `otari.py` |
| MCP integrations | HoldUp's `near-intel` MCP server, passed per request with a bearer token that only covers one hold's sender and payee | Yes, also on testnet | `pipeline.py`, `holdup/mcp_server.py` |
| Web search or web fetch | `otari_web_search` and, when the message has links, `otari_web_fetch` for the investigator | Fetch: yes (2026-11-01). Search: yes (2026-10-31), not used since | `pipeline.py` |
| Code execution | `otari_code_execution` for the investigator and the reviewer, asked to compute every number they rely on; HoldUp redoes the arithmetic | Yes | `pipeline.py`, `otari.py` |
| Budgets and spending limits | A budget refusal from the gateway becomes a fail-closed brake (`BUDGET_EXHAUSTED`); `/budget` and `/stats` show the spend | Not yet | `otari.py`, `holdup/engine.py` |
| Multiple models serving distinct roles | Triage, gate, investigator, reviewer, vision and the guardrail classifier | Yes: 4 roles | `pipeline.py` |
| Guardrails | `checker-injection`, served by HoldUp's `/validate`: monitor mode on triage and the investigator, block mode on a gate that keeps flagged text away from the models with tools | Monitor: yes. Block: yes, including a block (2026-11-01) | `pipeline.py`, `holdup/guard.py`, `holdup/risk/guard_models.py` |
| Per-user budgets | Each account is mapped to one key in `OTARI_USER_KEYS`, each meant to carry its own spend ceiling. The ceilings are set in the Otari dashboard, because the API answers 401 to an ordinary key | Not yet | `holdup/risk/budgets.py` |

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
make bench-rules     # ScamBench (labelled payments) on the rules layer, dev split: bench/results-rules.md
```

`OTARI_MODE=live OTARI_API_KEY=... .venv/bin/python -m holdup demo --offline` runs the same in-memory
story with the AI checks on the hosted gateway (about 0.05 USD, a few minutes). Without
`OTARI_MODE=live` the offline story replays its recorded answers even when a key is set, and
`make demo-offline` and `make attack` always replay them.

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

## Run it on your own server

On an Ubuntu server, as an ordinary user (no root, Docker or open ports), one command does
everything:

```bash
bash scripts/server.sh install          # or: install --offline, a rehearsal without NEAR
```

It checks the tools, asks for the Otari key and the Telegram bot token (input hidden, stored at
mode 600 in `~/work/secrets`), sets up NEAR testnet with free test money and deploys the
contracts after you type `yes`, then keeps HoldUp running in a tmux session named `holdup`, on a
tmux server of its own (`tmux -L holdup attach -t holdup`). The
app listens on `127.0.0.1` only; two outbound Cloudflare quick tunnels give it a public
`https://*.trycloudflare.com` address (and a separate one for the training pages), and the bot
long-polls Telegram. A supervisor restarts whatever fails. `status`, `logs`, `url`, `update`,
`restart` and `stop` look after it; [deploy/README.md](deploy/README.md) is the guide and
[docs/SERVER.md](docs/SERVER.md) explains how it works. A quick tunnel's address changes whenever
it restarts and has no uptime guarantee.

## ScamBench

`bench/` holds ScamBench 1.1: 170 labelled payment requests, 85 scams that each follow a public US
or UK government warning and 85 honest payments, many of them built to look risky (real deadlines,
busy shops, brand-new accounts). Items have a fixed dev (102) or test (68) split. They were written
with AI assistance and have had no independent annotation; [bench/DATASHEET.md](bench/DATASHEET.md)
says how they were made and what they cannot show.

Counts are dev / test.

| What brakes | Scams braked | Honest payments braked |
|---|---|---|
| Rules only | 36 of 53 / 22 of 32 | 3 of 49 / 0 of 36 |
| Rules and live triage answers, the other models silent (floor) | 43 of 53 / 25 of 32 | 3 of 49 / 1 of 36 |
| Same, the reviewer always agreeing (ceiling) | 52 of 53 / 31 of 32 | 20 of 49 / 9 of 36 |
| The floor before commit 009c73e weighed the models' findings | 51 of 53 / 30 of 32 | 19 of 49 / 9 of 36 |

- Rules: `make bench-rules` reproduces them offline (`SPLIT=all` for every row). The rules were
  tuned with both splits in view, so both splits are in-sample. On 24 items written outside the set
  they braked 5 of 12 scams and 1 of 12 honest payments. Details:
  [bench/results-rules.md](bench/results-rules.md).
- AI layer: the triage model (`gpt-oss-20b`) answered all 170 items live on Otari on 2026-10-31.
  Those cached answers were replayed through the pipeline. In the floor the investigator and the
  reviewer answer "nothing found"; in the ceiling the reviewer answers "hold it" every time it is
  asked. The live pipeline lies between the two; it has not been run on the whole set.
- Commit 009c73e traded some scams braked for far fewer honest payments braked.
- The test split is not clean held-out data for the AI rows either: earlier work had seen test
  results, and the weighing leans on rules tuned on test.
- The cached answers and the scoring scripts are not in this repository. The message of commit
  009c73e gives the floor before and after and the ceiling's scam counts; the same AI-row counts
  came out on commit 9e03a8b (rerun 2026-11-01).
- With 32 scams and 36 honest items in test, a rate on it carries roughly 10 to 17 percentage
  points of uncertainty either way (DATASHEET, section 4a).

## Status

Checked on 2026-11-01 at commit 9e03a8b unless a line gives another date. Dates are UTC.

- Offline: `make test` (1273 passed, 23 skipped) and the contract unit tests (80 vault tests, 6
  token tests) pass; `make demo-offline`, `make attack` and `make prove` run in about two seconds
  each. The contracts are built with cargo-near 0.22.0, checked against nearcore's preparation
  rules and validated with wasm-tools; rebuilding them in a scratch copy gives byte-identical files.
- AI checks, live (2026-10-31): the scripted story ran with its AI steps live on Otari, on the
  in-memory vault. Later that day the hosted server was driven through its public URL with live
  Otari, the in-memory vault and simulated transactions: six payments, 18 model steps, USD 0.0434
  in total, with Otari calling HoldUp's MCP server and guardrail endpoint. Details, request ids and
  what is still open: [docs/OTARI.md](docs/OTARI.md).
- What that run showed, and what commit 9e03a8b changed: the payee's reputation counted the hold
  under check, the 20-second demo hold was shorter than the check, an empty model answer ended the
  step, the web tools went unused once MCP was attached, and a guardrail block was shown as a
  failure. 9e03a8b fixes these. In its live run on 2026-11-01 (next item) three of the fixes showed:
  the check ended well within the 5-minute wait, the investigator used Otari's web fetch with MCP
  attached, and the guardrail's block was recorded as a block. The reputation and empty-answer
  fixes have not come up live yet. One honest brake in the 2026-10-31 run has no fix yet (the open
  issue below).
- NEAR testnet with live AI checks (2026-11-01): the bot's deployment (accounts near the top of
  this page) has run commit 9e03a8b with the hosted profile since 09:08. The public faucet was rate
  limited, so the treasury was funded with test NEAR from NEAR's account helper; no code changed.
  `verify-deploy` and `prove` passed at install. At about 09:58 two payments went through Telegram:
  50 HUSD to `safe-wallet-77` after the fake exchange-support sample, braked by the rules one
  second after the payment (for an hour), then held for 72 hours by the full check about a minute
  later, on a scam page read through Otari's web fetch; and 12.5 HUSD to the practice friend
  `ravi-6`, sent without a message, so no model was asked, released by the keeper after the
  5-minute wait. `/stats` at 10:05: 1 user, 2 holds, 1 brake, 1 release, 0 fail-closed, 3 model
  steps, 0 fallbacks, USD 0.0108 of AI. The vault shows that the sender cancelled the braked
  payment at 10:05, after those pages were saved. Transactions and request ids:
  [docs/OTARI.md](docs/OTARI.md#the-live-evidence).
- Hosted bot: one machine, a home server, reached only through two Cloudflare quick tunnels, with
  the bot long-polling Telegram (`scripts/server.sh`). During the 2026-10-31 run, in offline network
  mode, its `/health` showed the Telegram poller running for `@HoldUp_HoldUp_bot` with no error,
  but the saved evidence of that run comes from the web console. The 2026-11-01 payments on testnet
  were made in a Telegram chat with the bot.
- NEAR testnet rehearsal (2026-10-31, throwaway accounts): the deploy (the root spent 9.57 test
  NEAR), `verify-deploy` and `prove`, then onboarding and three held payments (a cancel, an oracle
  brake followed by a cancel, a keeper release), all as real transactions with the AI checks off.
  A new key was not visible at final finality for a block or two; commit 65d3b96 fixed that, and a
  deploy stopped on purpose then resumed where it stopped. `make demo` on testnet has not run.
- Not yet run: the sandbox tests (`tests/test_contract_sandbox.py`, `tests/test_chain_sandbox.py`;
  they need a running NEAR sandbox). Not yet shown live: the image step, a model fallback, a budget
  refusal, per-user keys and the `safety` guardrail profile. Web search has run live once
  (2026-10-31); the 2026-11-01 run made none.
- Known open issue: a model's claim of text aimed at the checker still brakes for 72 hours on its
  own, even when the guardrail passed the text (seen live on 2026-10-31 on an honest message that
  said "pre-approved"). A change that let the guardrail's pass override the models was rejected in
  review: it released a payment even when every model, the reviewer included, read an injection
  that the guardrail had missed.

## How this was built

HoldUp was built by its author with substantial help from an AI coding assistant (Claude Code).
The assistant wrote most of the code and most of the documentation in this repository, under the
author's direction. The ScamBench items were also written with AI assistance, as
[bench/DATASHEET.md](bench/DATASHEET.md) says.

## License

[MIT](LICENSE)
