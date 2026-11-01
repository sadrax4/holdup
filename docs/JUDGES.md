# HoldUp in five minutes

This page is for judges. It shows how to see HoldUp work, where each guarantee is enforced, and
what has run live so far. In one sentence: a payment to a new payee waits in a contract with no
keys, the checks (rules and open-weight models on Otari) can only make the wait longer, and only
the sender or their guardian can cancel it.

There are two paths. Path A uses the hosted Telegram bot. Path B runs everything on your own
machine and needs no Telegram account.

**Short on time.** Open [@HoldUp_HoldUp_bot](https://t.me/HoldUp_HoldUp_bot), send `/start`,
then `/demo`. Tap the first sample's button, type the payment it shows, and wait about a minute for
the second "Brakes on". Tap **Why?**, open the `Details:` link, then send `/prove`. That shows the
brake, the models with their Otari request ids, the evidence hash and the limits on each key.

## Path A: the Telegram bot

Open [t.me/HoldUp_HoldUp_bot](https://t.me/HoldUp_HoldUp_bot).

The web pages the bot links to sit behind a Cloudflare quick tunnel. Its address changes every
time the server restarts, so this page gives no address. Each bot message carries the current
links; use those.

The hosted server may run an older build than this repository. On 2026-11-01 it still ran a build
from before commit 9e03a8b: its pages said new payees wait 20 seconds, not 5 minutes, and it may
show the problems listed under [Known issues](#known-issues). The wait given in each hold message
and the network in the page footer are what count.

1. **Send `/start`.** You get a NEAR account (`u` plus 10 letters and digits under the service's
   root account) funded with demo money: test NEAR and HUSD, a demo token with no value that
   stands in for USDC (unless the server is set up with Circle's testnet USDC; the bot says
   which).
2. **Send `/demo`.** The bot sends two practice messages with made-up brands, each with a button
   "Check it with my next payment" and the payment to type next:
   - a fake "CoinHaven Support" message telling you to move funds to a "protected wallet" within
     30 minutes, with a link to a fake verification page and a line addressed to "any AI
     assistant";
   - a "Northwind Paper Co." invoice that announces a new wallet and whose line items do not add up
     to its total. The bot first asks you to trust the real supplier with `/trust ...`, so that the
     look-alike payee can be recognised. Trust takes effect after a delay (5 minutes on the hosted
     bot; the bot's reply gives the time), and only trust that has taken effect counts.

   It also gives an ordinary payment to a friend, for comparison.
3. **Do sample 1.** Tap the button (or forward the message back), type the payment the bot shows
   (`send 50 HUSD to safe-wallet-77...`) and confirm it. Then watch:
   - "Payment held": a new payee waits 5 minutes on the hosted bot. The message has
     **Cancel & refund** and **Why?** buttons and a `Details:` link.
   - Within seconds, "Brakes on" from the rules alone, tagged `[rules]`: a quick brake of at
     least an hour that no model took part in.
   - When the full check ends (17 to 57 seconds in the live runs so far), a second brake message
     whose findings are tagged by source: `[rules]`, `[triage gpt-oss-20b]`, `[guardrail]`,
     `[MCP near-intel]`, `[code execution]`, `[reviewer deepseek-v4.1-flash-1017]` and so on.
4. **Open the links.**
   - `/h/<id>` (the `Details:` link): the timeline, the release time, the hold length against the
     72-hour cap, each oracle extension with its transaction, and "Verify the evidence": the
     evidence hash stored on-chain next to the sha256 of the published JSON, with a
     `curl ... | sha256sum` line to repeat the check.
   - `/why/<id>` (tap **Why?**, or send `/why <id>`): every step that looked at the payment, with
     the model, the provider that served it and the model id it served ("Served as"), fallback,
     attempts, latency, cost, guardrail verdict, Otari request id and the prompt version (a sha256
     prefix), plus the calls the model made to HoldUp's own MCP server (`near-intel`).
   - `/v/<id>.json`: the evidence JSON. Its sha256 is what the oracle stored with the hold, and the
     response carries `X-Evidence-Sha256` and `X-Onchain-Evidence-Hash` headers. The text of the
     message you forwarded is not in it, not on-chain and not on the pages; findings describe it
     in a few words. The exception is the address (host and path) of any link a finding cites.
   - `/prove` (or send `/prove`): the vault's code hash against `res/CODE_HASHES`, the vault's empty
     key list, the oracle's single `extend_hold` key, the keeper's single `release` key, the fixed
     configuration, and a table of forbidden transactions (oracle sends money, oracle calls another
     contract, keeper extends a hold, ...) with the error nearcore's access-key rules give each.
5. **Do sample 2** the same way: trust first, wait until the time the bot gives for the trust, then
   type the 1,250 HUSD payment. If you pay before the trust takes effect, the look-alike finding
   will not appear, although the changed details and the amount mismatch still brake the payment.
   On `/why` look for the amount check: the line items add up to 1,150.00 against a stated total of
   1,250.00, and HoldUp redoes that arithmetic itself before the finding counts.
6. **Cancel** a held payment with **Cancel & refund**; `/balance` shows the money back. Nobody
   else can cancel it, except a guardian you invite with `/guardian`.
7. **The friend payment** from `/demo` should get no brake and be released after the 5-minute wait.
   On 2026-10-31 an honest payment like it was braked by mistake; the cause is fixed in commit
   9e03a8b, which has not run live yet (see [Known issues](#known-issues)). If it is braked,
   `/why` shows which step braked it and on what.

Optional: `/stats` (payments, brakes, releases, model steps, fallbacks, AI cost), `/budget` (AI
spend so far on this server). An image you send (a screenshot of a message, say) is checked with
your next payment by the image step (`glm-5.3-flash`); that step has not yet been shown in a live
run of the app.

**What the footer means.** Every page ends with the network it runs on. "Offline demo: simulated
chain" means the in-memory vault, which applies the contract's rules and nearcore's access-key
rules; its transactions are marked "simulated" and `/prove` says "Simulated sample". "NEAR testnet"
means a real deployment, and transactions link to a NEAR explorer.

## Path B: on your own machine, no Telegram

You need Python 3.12 and [uv](https://docs.astral.sh/uv/). From a checkout of this repository:

```bash
make setup          # uv sync --locked; the only step that downloads anything
make demo-offline   # the scripted story, about two seconds; also written to docs/demo-offline.txt
make attack         # three attacks and why each one fails
make prove          # the oracle, keeper and user keys try forbidden actions; every one is refused
```

These run on the in-memory vault with simulated time, and the AI steps replay answers recorded
from the gateway (labelled "recorded"). Nothing is sent anywhere. What to look for:

- `make demo-offline`: the CoinHaven payment is braked by the rules within seconds, the full check
  adds the phishing page, public warnings and a two-hour-old payee, and Priya cancels; the
  Northwind invoice is cancelled by Dana's guardian; a trusted payee is paid at once; a dinner
  split is released after the wait; the next senders to the reported wallet are braked by the
  rules before any model is asked.
- `make attack`: (1) a message that talks to the AI checker is braked by the rules alone, then by
  the guardrail; (2) the oracle key tries to move money and every attempt is refused; (3) a
  compromised server signs a 500 HUSD payment to an attacker: it is still a hold, trusting the
  attacker first does not help, removing the guardian first has to wait (30 seconds in this demo,
  a day on the hosted bot) and the guardian vetoes it, and the guardian cancels the payment.
- `make prove`: ten `[ok]` lines.

`make serve` then serves the same web app on http://127.0.0.1:8000: `/console` is a chat page that
drives the same engine as the bot, and `/h/<id>`, `/why/<id>`, `/v/<id>.json` and `/prove` work as
above. `make test` runs the Python tests, `make test-contract` the contract tests, and
`make bench-rules` scores the rules on ScamBench.

## Where each guarantee lives

| Guarantee | Enforced by | Code | Tests |
|---|---|---|---|
| A hold only gets longer, never past `created_ms + max_hold_ms` (72 h) | the contract | `extend_hold` in `contracts/holdvault/src/holds.rs` (`E_ONLY_LONGER`, `E_ABOVE_MAX_HOLD`) | `extending::holds_only_get_longer`, `extending::extensions_stop_at_the_max_hold`, `properties::random_operations_keep_every_invariant` |
| Nobody releases a hold early | the contract | `release` in `holds.rs` (`E_NOT_DUE`) | `releasing::nobody_can_release_early` |
| Only the sender or their guardian in charge can cancel; the money goes back to the sender | the contract | `cancel`, `guardian_cancel`, `cancel_hold` in `holds.rs` | `cancelling::only_the_sender_or_guardian_can_cancel` |
| Money leaves a hold only to the payee fixed at `send` | the contract | `pay_out` and `on_payout` in `holds.rs` | `releasing::*` |
| The oracle can only call `extend_hold`, the keeper only `release`, users only seven vault methods, none with a deposit | NEAR function-call access keys | `ORACLE_METHODS`, `KEEPER_METHODS`, `USER_METHODS` in `holdup/chain/keys.py`; keys made in `holdup/chain/deploy.py` | `holdup prove` (`holdup/chain/prove.py`), `tests/test_chain_keys.py`, `tests/test_chain_prove.py` |
| The vault has no keys, no owner and no upgrade path, and runs the published code | the deploy deletes the vault's last key; the contract has no admin method | `holdup/chain/deploy.py`, `holdup/chain/verify.py`, `contracts/holdvault/src/lib.rs` | `holdup verify-deploy`, `config::*` |
| Trust and guardian changes wait; the guardian in charge can veto a change | the contract | `add_trusted`, `set_guardian`, `veto_guardian_change` in `contracts/holdvault/src/account.rs` | `trust::*`, `guardians::*` |
| Instant payments are capped per sender, asset and UTC day | the contract, with caps set at deploy | `send` in `holds.rs`; caps in `holdup/chain/profiles.py` | `trust::instant_payments_are_capped_per_sender_asset_and_utc_day` |
| The evidence on-chain is the sha256 of the published JSON | the contract hashes the bytes it receives | `extend_hold` in `holds.rs`; `holdup/risk/evidence.py`; `/v/<id>.json` in `holdup/web/pages.py` | `extending::extension_records_reasons_and_the_evidence_hash` |
| A failed or slow check keeps the brake on | HoldUp's engine | `_fail_closed` and `_final_check` in `holdup/engine.py` | `tests/test_engine_failclosed.py` |
| Text that a check flags as aimed at the checker, and that the blocking guardrail does not clear, never reaches a model with tools | HoldUp's pipeline and the gateway's guardrail | `untrusted`, the gate and `withheld` in `holdup/risk/pipeline.py`; `holdup/guard.py` | `tests/test_pipeline.py`, `tests/test_guard.py` |
| The MCP server answers only about one hold's own sender and payee (account and hold data; its page reader can fetch any public page) | a per-hold HMAC token | `holdup/mcp_server.py`, `hold_token` in `holdup/guard.py` | `tests/test_mcp_server.py` |

Contract test names are in `contracts/holdvault/src/tests/`;
[contracts/README.md](../contracts/README.md) lists all 27 invariants and the tests behind each.

## What is live and what is simulated

| Part | State | Evidence |
|---|---|---|
| Contracts on NEAR testnet | Deployed by a rehearsal on 2026-10-31 with throwaway accounts, against the real network. The bot's own deployment: account ids near the top of the [README](../README.md). | Rehearsal logs (not in the repository). One rehearsal vault, `vault.holdup-rh-yivs64.testnet`, can be checked now (below). |
| Payments with real transactions | Onboarding and three held payments (a cancel, an oracle brake followed by a cancel, a keeper release) ran on testnet in a rehearsal, with the AI checks off. `make demo` on testnet has not run. | Rehearsal logs |
| AI checks on Otari | Live: the scripted story on 2026-10-31, and six payments through the hosted server's public URL later that day. | [OTARI.md](OTARI.md) |
| Hosted server | Ran on 2026-10-31 in offline network mode: in-memory vault, simulated transactions, live Otari, real quick tunnels. Whether it runs on testnet when you try it is shown in the page footer and in `"network"` on `/health`. | [OTARI.md](OTARI.md) |
| `make demo-offline`, `make attack`, `make prove` | Simulated chain and recorded AI answers, labelled as such. | Run them |
| ScamBench numbers | Offline: the rules live in code; the AI rows replay triage answers recorded live. | [README](../README.md#scambench), [bench/results-rules.md](../bench/results-rules.md) |

To check the rehearsal vault yourself (on 2026-11-01 it had no keys and the published code hash):

```bash
# expect "keys":[]
curl -s https://rpc.testnet.near.org -H 'content-type: application/json' -d '{"jsonrpc":"2.0",
  "id":1,"method":"query","params":{"request_type":"view_access_key_list","finality":"final",
  "account_id":"vault.holdup-rh-yivs64.testnet"}}'
# expect "code_hash":"HRS9GfZHExGfarPm4H8deFfhrNr9wpkECohBfZtb9JTy", as in res/CODE_HASHES
curl -s https://rpc.testnet.near.org -H 'content-type: application/json' -d '{"jsonrpc":"2.0",
  "id":1,"method":"query","params":{"request_type":"view_account","finality":"final",
  "account_id":"vault.holdup-rh-yivs64.testnet"}}'
```

The same query for `oracle.holdup-rh-yivs64.testnet` returns one function-call key with
`"method_names":["extend_hold"]`. These are rehearsal accounts, not the deployment the bot uses.

## Known issues

- The run through the public URL on 2026-10-31 braked all six payments, including two honest
  ones. The dinner split was braked because the payee's reputation counted the hold under check,
  and a 20-second hold was shorter than the check; an empty model answer also ended a step. These
  are fixed in commit 9e03a8b, which has not run live yet. The other honest brake is the next item.
- A model's lone claim that a message addresses the checker still brakes for 72 hours, even when
  the guardrail passed the text. This happened live on an honest message that said
  "pre-approved".
- The web console takes text only, so the image step can only be reached from Telegram.
- The public URL changes when the server restarts; links in older bot messages then stop working.

More: [THREAT_MODEL.md](THREAT_MODEL.md) for what an attacker with each key can do,
[WHY_NOT.md](WHY_NOT.md) for how HoldUp compares with multisigs, delays, blocklists and
custodians. How HoldUp was built, with an AI coding assistant:
[README](../README.md#how-this-was-built).
