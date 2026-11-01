# Otari in HoldUp

HoldUp checks each held payment with open-weight models through the
[Otari](https://otari.ai) gateway. This page takes the "Devs Wilding" rubric one item at a time:
what HoldUp does, where it is in the code, and what has been seen live, with dates. It uses three
labels:

- **Verified live** (with a date): a saved record of the app, not a test or a probe, shows it
  working against the hosted gateway on that date. The saved records (the app's pages and request
  logs) are kept by the author; they are not in this repository.
- **Partly verified live**: part of it is shown; the rest is said.
- **Implemented, not yet shown live**: the code and its offline tests exist, but no live run of
  the app has shown it yet.

## Summary

Dates are UTC. Runs 1 and 2 were on 2026-10-31 on the in-memory vault; run 3 was on 2026-11-01 on
NEAR testnet (see the next section). Request ids are shortened to their first 8 characters; the
full ids are in the sections below.

| Rubric item | Status | Evidence | Code |
|---|---|---|---|
| Open-weight models (20, +10 for two or more) | Verified live: three open-weight models | 2026-10-31: triage `77016f08`, investigator `7a358dcb`, reviewer `72b3f1d9`; run 3: triage `07cf8e8a`, investigator `c24673f3`; all served by deepinfra | `holdup/risk/otari.py`, `holdup/risk/pipeline.py` |
| Model routing and fallbacks | Routing and headers verified live; a fallback not yet shown | runs 2 and 3: provider, served model, fallback and attempts on every step a provider served; 0 fallbacks | `FALLBACK_CHAIN`, `fallbacks_for` in `otari.py` |
| MCP integrations | Verified live, again on testnet in run 3 | run 2: the gateway called near-intel on every hold; an out-of-scope call refused on hold 1. Run 3: three near-intel calls in `c24673f3` | `holdup/mcp_server.py`, `_mcp_servers` in `pipeline.py` |
| Web search or web fetch | Web fetch verified live (run 3); web search verified live (run 1) | fetch in `c24673f3`, with three findings resting on the fetched page; search in `17299991`; no search in run 3 | `_investigate` in `pipeline.py` |
| Code execution | Verified live | `17299991` (run 1), `72b3f1d9` (run 2) | `pipeline.py`, `_fill` in `otari.py` |
| Budgets and spending limits | Implemented, not yet shown live | none | `BudgetExceeded` in `otari.py`, `_fail_closed` in `holdup/engine.py` |
| Multiple models serving distinct roles | Verified live: 4 roles on 3 models | run 2, as for open-weight models, plus the gate `1db2c093`; run 3: triage and investigator on two models, and a gate blocked before any model ran | `_review` in `pipeline.py` |
| Guardrails | Verified live: monitor mode, and block mode both passing and blocking | monitor `77016f08`, `26e0860c` (run 2), `07cf8e8a` (run 3); block passed `1db2c093` (run 2); block blocked `fad581a8` (run 3) | `holdup/guard.py`, `_gate` in `pipeline.py` |
| Per-user budgets | Implemented, not yet shown live | none | `KeyPool` in `holdup/risk/budgets.py` |

## The live evidence

Times are UTC.

- **Run 1, 2026-10-31, 18:21 to 18:23.** The scripted story of `holdup demo --offline` on the
  in-memory vault, run through a capture script that builds the same runtime with live Otari
  (until commit b138afb the CLI itself ignored `OTARI_MODE=live`). Every AI step went to the
  gateway. No MCP server or guardrail endpoint was attached, because the gateway can only call
  public URLs. The real CLI, with fixes later committed as b138afb, ran the same story live from
  19:01 to 19:04.
- **Run 2, 2026-10-31, 23:09 to 23:30.** The hosted server on a public quick-tunnel URL, in offline
  network mode (in-memory vault, simulated transactions) with live Otari, driven through its web
  console: six payments, 18 model steps, 0 fallbacks, USD 0.043378 in total (`/stats.json`, saved
  at 23:30). Three of the six brakes were first fail-closed, because the 20-second demo hold ended
  before the check did. The gateway called HoldUp's MCP server and guardrail endpoint. Saved
  copies of `/h`, `/why`, `/v`, `/stats`, `/profiles` and `/health` are kept by the author; they
  are not in this repository. A quick-tunnel URL changes whenever the server restarts, so these
  docs do not give it.
- Run 2 ran code older than 009c73e (inferred from the wording on its pages), so it predates all
  the 9e03a8b fixes. Run 3 is the first live run of 9e03a8b.
- **Run 3, 2026-11-01, 09:58 to 10:05.** The bot's own deployment on NEAR testnet (accounts and
  deploy transactions in the [README](../README.md)), commit 9e03a8b with the hosted profile (new
  payees wait 5 minutes), driven through Telegram: two payments as real transactions, 3 model
  steps, 0 fallbacks, 0 fail-closed, USD 0.0108 in total (`/stats.json`, saved at 10:05). The
  gateway called HoldUp's MCP server and guardrail endpoint. Saved copies of `/h`, `/why`, `/v`
  and `/stats` for both payments, and of `/prove`, are kept by the author. Transactions open on
  testnet.near.rocks.
  - Hold 0: 50 HUSD to `safe-wallet-77.holdup-soleimani.testnet` after the fake exchange-support
    sample was forwarded
    ([send](https://testnet.near.rocks/tx/EBygqPpQ1FysLb84HetG8XdapU5hehnz8rxZAs41AZe8) at
    09:58:54). The rules braked it at 09:58:55 for an hour
    ([extend_hold](https://testnet.near.rocks/tx/4Siszk8k6mXXf7xmczkvhP76dxKvD2pdGMH6YxxKvFDN));
    the full check held it at 09:59:59 for 72 hours after the payment
    ([extend_hold](https://testnet.near.rocks/tx/3NusUfChJ83m53LiqLsAnrreu7e2ZVjudthZP9KtZyBr)).
    Steps: triage (`openai/gpt-oss-20b`), a gate that the guardrail blocked, and the investigator
    (`openai/gpt-oss-120b`, 56 seconds). Findings are tagged `[rules]`, `[triage gpt-oss-20b]`,
    `[guardrail]`, `[MCP near-intel]` and `[web fetch]`. The hold's on-chain evidence hash,
    `69cf4ae8f3d9d6aa7280639a51ba6fa85dbeb37c48b3d337dac22d0fbc4faf48`, equals the sha256 of the
    published `/v/0.json`. The vault's `get_hold` shows that the sender cancelled the payment at
    10:05:27, after the pages were saved.
  - Hold 1: 12.5 HUSD to the practice friend `ravi-6.holdup-soleimani.testnet`, with no message
    ([send](https://testnet.near.rocks/tx/5EkBMH8Rr157ipf4UheG4cnpiawuJm3BXHUPuSHzWZM1) at
    09:59:10). With no message there was nothing for a model to read, so none was asked; the rules
    found no reason to brake (the payee's age, under an hour, is shown as context only), and the
    keeper released it at 10:04:16, after the 5-minute wait
    ([release](https://testnet.near.rocks/tx/4bt5ZTK6LfMGq9WowUCE5bdK5sgg6fDczU1C7pJBzrAK)).
- **Gateway probes, 2026-10-31, mostly between 12:06 and 12:29.** Direct calls to the gateway, not
  through the app, to learn its behaviour. Some are in `fixtures/otari/recorded/`. They show what
  the gateway does, not what HoldUp does, so no item below counts as verified on a probe alone.

## Open-weight models (rubric: 30 points)

Every model HoldUp asks for is listed as `open_weights: true` in the gateway's catalog
(`fixtures/otari/recorded/catalog-models.json`, recorded 2026-10-29). Model ids are in
`holdup/risk/otari.py`; the steps are in `holdup/risk/pipeline.py`.

| Model | Role | Status |
|---|---|---|
| `openai/gpt-oss-20b` | triage: reads the forwarded message and reports signals; also the gate request that carries the blocking guardrail | **Verified live on 2026-10-31** (runs 1 and 2; for example triage request `77016f08-7f60-406f-a360-bc4768a11ef8`, provider deepinfra, USD 0.000081) **and on 2026-11-01** (run 3, triage request `07cf8e8a-2500-4459-bdfb-e6ad9ab9839d`, deepinfra, USD 0.000062) |
| `openai/gpt-oss-120b` | investigator: checks the payee and the links with MCP, web search, web fetch and code execution | **Verified live on 2026-10-31** (runs 1 and 2; for example request `7a358dcb-447b-4996-ba4b-1d4d1c15accd`, deepinfra, USD 0.010647) **and on 2026-11-01** (run 3, request `c24673f3-c063-41be-83b0-8b4500731150`, deepinfra, 56 seconds, USD 0.010738) |
| `deepseek/deepseek-v4.1-flash-1017` | reviewer: a blind second opinion from another model family; also the investigator's first fallback | **Verified live on 2026-10-31** (request `72b3f1d9-c7d7-45de-a1eb-6e92c4fc75f2`, deepinfra, served as `deepseek-ai/DeepSeek-V4.1-Flash-1017`, USD 0.010138) |
| `z-ai/glm-5.3-flash` | vision: reads an image the sender shares; also the last fallback family | **Implemented, not yet shown live** in the app. The web console takes text only; Telegram images reach it. A gateway probe on 2026-10-31 read an invoice image with it. |
| `openai/gpt-oss-safeguard-120b` | the `checker-injection` classifier behind HoldUp's `/validate` endpoint, with HoldUp's written policy (`holdup/risk/guard_models.py`) | **Partly verified.** In runs 2 and 3 the gateway called `/validate` and its verdicts came back (see Guardrails). The endpoint falls back to phrase rules when the classifier fails, and the saved pages do not record which of the two answered, so this model's part is not verified. |
| `meta/llama-guard-4-12b` | the `safety` profile behind `/validate` | **Implemented, not used.** The pipeline never requests the `safety` profile. |

A check can involve three model families: gpt-oss, deepseek and, for images, glm. The reviewer,
asked on large payments, when triage and the investigator disagree, or when a judgement waits for
a second opinion, comes from a family the earlier steps did not use, as long as one is left
(`_review` in `pipeline.py`).

## Model routing and fallbacks

**What HoldUp does.** It sends catalog ids, never a pinned `provider:model`, so the gateway picks
the provider. From every answer it records `Otari-Provider`, `Otari-Model` (shown as "Served as"),
`Otari-Fallback`, `Otari-Attempt-Count` and `Otari-Request-ID`, shows them per step on `/why/<id>`
and puts provider, fallback, attempts and request id into the evidence JSON whose hash goes
on-chain. On top of the gateway's routing it has its own fallback chain across model families for
the investigator: `gpt-oss-120b`, then `deepseek-v4.1-flash`, then `glm-5.3-flash` (`FALLBACK_CHAIN`
and `fallbacks_for` in `holdup/risk/otari.py`). Since commit 9e03a8b an empty or malformed answer
also moves to the next family while time is left.

Calls that use gateway tools are streamed, because the hosted CDN cuts non-streamed responses after
about 60 seconds, and each attempt has a wall-clock limit. Gateway errors become typed exceptions
(budget exceeded, guardrail violation, all candidates failed, firewall block and others) so the
pipeline can tell a budget refusal from an outage (`otari.py`). `usage.cost_usd` is recorded per
step and shown on `/why/<id>`.

**Status: partly verified live on 2026-10-31 and 2026-11-01.** Routing and the headers are shown:
in run 2 every model step that a provider served recorded provider deepinfra, attempts 1 and
fallback no (the two refused gate requests recorded no provider), and the catalog id
`deepseek/deepseek-v4.1-flash-1017` was served as the provider's
`deepseek-ai/DeepSeek-V4.1-Flash-1017`. Run 3's triage and investigator recorded the same
(deepinfra, attempts 1, fallback no; the blocked gate request recorded no provider).
**Fallbacks: implemented, not yet shown live.** No fallback happened in any run (`/stats.json`: 0
fallbacks over 18 steps in run 2, 0 over 3 in run 3), and the public console cannot force one.
The offline tests replay one (`fixtures/otari/all_candidates_failed.json`, synthetic).

## MCP integrations

**What HoldUp does.** It runs its own MCP server, `near-intel`, at `/mcp` (`holdup/mcp_server.py`)
and passes it to the investigator in each request (`mcp_servers`, `_mcp_servers` in
`pipeline.py`), with a bearer token that only opens data about that hold's sender and payee. Its
tools: `payee_profile`, `payee_reputation`, `sender_history`, `hold_status`, `fetch_page`. The
server logs each call, and `/why/<id>` lists them, because Chat Completions does not report MCP
calls.

**Status: verified live on 2026-10-31** (run 2) **and again on NEAR testnet on 2026-11-01** (run
3). In run 2 the gateway called `/mcp` with the per-hold token:

- hold 0: `payee_profile(safe-wallet-77.testnet)` and
  `fetch_page(<training host>/fixtures/coinhaven/verify)`; the investigator's findings
  `PHISHING_LINK` ("page asks for 12-word recovery phrase") and `SECRET_REQUEST` cite that page and
  are tagged `[MCP near-intel]`;
- hold 1: `payee_profile(northwind-paper.testnet)`, then
  `sender_history(northwind-paper.testnet, unknown_sender)`, refused with "this token only covers
  the sender and the payee of its hold": the token's scope working live;
- holds 2 and 4: `payee_profile(ravi.testnet)` and `payee_reputation(ravi.testnet)`;
- hold 3: `payee_profile(mum.testnet)`;
- hold 5: `fetch_page(<training host>/fixtures/northwind/invoices/NP-2291)`.

In run 3, hold 0 (investigator request `c24673f3-c063-41be-83b0-8b4500731150`), the gateway called
`payee_profile(safe-wallet-77.holdup-soleimani.testnet)`,
`payee_reputation(safe-wallet-77.holdup-soleimani.testnet)` and `sender_history()`, and near-intel
refused none of them. The investigator's reading of the payee's age is tagged `[MCP near-intel]`
next to the rules' own, and `/why` lists the three calls among the step's tools.

Run 2 also showed two faults, fixed in 9e03a8b: `payee_reputation` counted the hold under check
and the sender's own earlier payments, and the model had to guess the sender's account for
`sender_history` (the history tools now default to the hold's own parties). Run 3 shows the second
fix at work: `sender_history()` came with no arguments and was answered. Whether the reputation
now leaves out the hold under check is not visible on the saved pages.

## Web search or web fetch

**What HoldUp does.** The investigator is offered `otari_web_search` (at most three searches) and,
when the message has links, `otari_web_fetch` (`_investigate` in `pipeline.py`). A claim of a
public warning counts only if it cites a page the gateway reports the search returned, and a
phishing-page claim only if the page was read. If the workspace has web access off, the tool is
dropped, the step is retried without it and a note says so on `/why`.

**Status: web fetch verified live on 2026-11-01 (run 3); web search verified live on 2026-10-31
(run 1), and not used since.**

- Run 1, hold 1 (the Northwind invoice): investigator request
  `17299991-165c-49e9-be2a-adf5bc22bb79` (deepinfra, USD 0.020652) ran `otari_web_search` for
  "northwind-paper.testnet scam", and the gateway reported the search completed with its results.
- Run 1, hold 0: `otari_web_fetch` was called (request `8ff967c9-4873-492d-a791-ef492c21e8fb`) but
  failed with `url_invalid`, because the training page was then on `127.0.0.1`, which the gateway
  cannot reach.
- Run 2: with near-intel attached, the investigator read both linked pages with near-intel's
  `fetch_page` and used neither gateway tool. Commit 9e03a8b leaves `fetch_page` out while the
  gateway's web fetch is offered (it comes back if that fetch fails) and asks for at least one
  search.
- Run 3, hold 0 (investigator request `c24673f3-c063-41be-83b0-8b4500731150`, near-intel
  attached): the investigator called `otari_web_fetch` on the training page the forwarded message
  linked, and the gateway reported the fetch completed. HoldUp counts a page finding as
  `[web fetch]` only when it cites a page the gateway reports fetching, and three did: the page
  asks for a 12-word recovery phrase, it asks for that phrase and a login, and it says the account
  will be frozen in 30 minutes. The first two are what the full check added to the rules' brake.
- Not yet seen: the investigator made no web search in run 3, so a search on 9e03a8b, and a
  finding that rests on a search, have not been seen live.

## Code execution

**What HoldUp does.** The investigator and the reviewer are offered `otari_code_execution` and are
asked to compute every number they rely on. HoldUp then redoes the arithmetic of an amount
mismatch on the numbers in the message before the finding counts. Chat Completions does not report
sandbox runs, so HoldUp infers them from rounds of tools in which the gateway reported no other
call, when no MCP server is attached (`_fill` in `otari.py`).

**Status: verified live on 2026-10-31.**

- Run 1, hold 1: the investigator's answer lists the code it ran, `print(600.00 + 350.00 + 200.00)`
  with result `1150.0`, against a stated total of 1,250.00 (request
  `17299991-165c-49e9-be2a-adf5bc22bb79`). The step cost USD 0.020652, against USD 0.000263 for
  the same model's step on hold 0, which is consistent with sandbox runs.
- Run 2, hold 1: the reviewer (request `72b3f1d9-c7d7-45de-a1eb-6e92c4fc75f2`) shows
  `otari_code_execution` among its tools and cost USD 0.010138, against USD 0.000051 for a reviewer
  step without a sandbox run on hold 3; its finding "line items add up to 1150.00 but the stated
  total is 1250.00 (off by 100.00)" matched HoldUp's own arithmetic.
- Not shown on the pages: when the MCP server is attached, a sandbox run is not inferred, so
  investigator steps in run 2 show no code execution even where the cost suggests one.

## Budgets and spending limits

**What HoldUp does.** A 403 `budget_exceeded` from the gateway becomes `BudgetExceeded`
(`otari.py`), and the pipeline turns it into a fail-closed brake with the on-chain reason
`BUDGET_EXHAUSTED` (`pipeline.py`); the engine keeps the payment held and retries
(`_fail_closed` in `holdup/engine.py`). `/budget` and `/stats` show the spend so far, summed from
`usage.cost_usd`.

**Status: implemented, not yet shown live.** No budget refusal has been seen. The gateway answers
401 to an ordinary API key on the organisation routes
(`fixtures/otari/recorded/org-keys-list-own.json` is one such answer), so the app cannot read the
budgets, and whether a ceiling is set cannot be shown from it. The offline tests replay a refusal
(`fixtures/otari/budget_exceeded.json`, synthetic).

## Multiple models serving distinct roles

**What HoldUp does.** Seven roles are wired: triage, gate, investigator, reviewer, vision and the
`checker-injection` classifier, plus a `safety` classifier that the pipeline does not request yet.
The models are listed under Open-weight models above.

**Status: verified live on 2026-10-31** for four roles on three models: triage and gate
(`gpt-oss-20b`), investigator (`gpt-oss-120b`) and reviewer (`deepseek-v4.1-flash`), all in run 2.
Run 1 used triage and the investigator; its only reviewer answer was rejected as malformed
(`bad_json`, request `87240c05-29af-452c-914b-e8e8699a5a2e`), and the 19:01 CLI rerun had a usable
reviewer answer (request `fe20a3d6-d52e-48d4-9ccb-d5106f923fec`). Run 3 (2026-11-01, on testnet)
used triage (`gpt-oss-20b`) and the investigator (`gpt-oss-120b`), with a gate request between
them that the guardrail blocked before any model ran; the reviewer was not asked. The vision and
classifier roles are as stated in the model table.

## Guardrails

**What HoldUp does.** It serves a guardrail endpoint, `POST /validate` and `GET /profiles`
(`holdup/guard.py`), with a `checker-injection` profile for text written to steer an AI checker. The
triage and investigator requests carry it in monitor mode; triage reads the verdict from the
`Otari-Guardrails` response header. When anything suspects an injection, a separate gate request
carries it in block mode, and text that is blocked, or that the gate could not judge, never
reaches the models with tools (`_guardrails`, `_gate` and `withheld` in `pipeline.py`). Each
request carries a per-hold token in `validate_kwargs`.

**Status: monitor mode verified live on 2026-10-31 and 2026-11-01; block mode verified live, with
a pass on 2026-10-31 and a block on 2026-11-01.**

- Monitor (run 2): the triage rows show `checker-injection, monitor: flagged` with score 1.0 on
  hold 0 (request `77016f08-7f60-406f-a360-bc4768a11ef8`) and hold 4 (request
  `26e0860c-a235-4ad2-ac73-5b7e083ab63b`), and `monitor: passed` with score 0.0 on holds 1, 2, 3
  and 5. Each request named HoldUp's endpoint as the guardrail's URL, and `checker-injection` is
  HoldUp's own profile (listed by its `/profiles`), so these verdicts came from `/validate`
  through the gateway. Holds 0 and 4 carry a `CHECKER_INJECTION` finding from source `guardrail`.
- Block, passed (run 2, hold 3): the gate row shows `checker-injection, block: passed`, request
  `1db2c093-0324-477d-b02f-013cb6ed3366`, deepinfra, USD 0.000008. The gateway accepted the block
  entry and called `/validate`.
- Block, refused (run 2, holds 0 and 4): the gateway refused the gate request in 298 ms and 267 ms,
  with no provider and no cost (requests `8bef9162-5d48-410a-82ab-9917306cda45` and
  `19034f6a-028e-4558-a397-f3a0aad70598`), and the text stayed withheld from the investigator.
  HoldUp then recorded this as a failed step (`request_refused`). Since commit 9e03a8b a bare 403
  on a request with a block-mode guardrail is recorded as the guardrail's block, with a note that
  the gateway answered 403 with no error code; that case has not come up live since, and the saved
  pages do not record the status of the 2026-10-31 refusals.
- Monitor (run 3, hold 0): triage request `07cf8e8a-2500-4459-bdfb-e6ad9ab9839d` shows
  `checker-injection, monitor: flagged` with score 1.0, and the hold carries a `CHECKER_INJECTION`
  finding from source `guardrail`.
- Block, blocked (run 3, hold 0): the gate request `fad581a8-01fc-422a-8a3a-3863a59811d7` was
  refused after 470 ms, with no provider and no cost, by a 403 that this time carried the error
  code `guardrail_violation` ("Request blocked by guardrail policy.", profile
  `checker-injection`). HoldUp recorded it as "blocked the text", not as a failure, and the
  investigator then saw only the facts HoldUp extracted from the message.
- Which classifier produced each verdict (the safeguard model or the fallback rules) is not on the
  saved pages.

## Per-user budgets

**What HoldUp does.** `KeyPool` (`holdup/risk/budgets.py`) shares out the Otari keys listed in
`OTARI_USER_KEYS` by a stable hash of the account, so each account always spends against the same
key; the runtime wires it in (`user_key_resolver` in `holdup/runtime.py`) and the pipeline switches
to that key when one resolves. Each key is meant to carry its own spend ceiling, so one person
cannot use up everyone's AI budget. `OtariAdmin` can create keys, budgets and ceilings through the
organisation routes, but the hosted gateway refuses those routes to an ordinary API key, so the
keys are made in the dashboard.

**Status: implemented, not yet shown live.** `OTARI_USER_KEYS` is not set in
`deploy/holdup.env.example`, and nothing in the saved evidence shows a per-user key or ceiling.

## Project and demo

How to try it, and what is live and what is simulated: [JUDGES.md](JUDGES.md). Keys, attacks and
limits: [THREAT_MODEL.md](THREAT_MODEL.md).

**Video: (link to be added by the author).**

## Appendix: the six payments of run 2

Outcomes as the run recorded them, on code older than 009c73e. Hold 3's 72-hour brake has no fix
yet ([JUDGES.md, Known issues](JUDGES.md#known-issues)); the other mistaken brakes are fixed in
9e03a8b, and these honest payments have not been repeated live on it.

| Hold | Payment | Outcome then | Cost (USD) |
|---|---|---|---|
| 0 | CoinHaven sample, 50 HUSD to `safe-wallet-77.testnet` | braked 72 h: rules within a second, then a secret request and a phishing page read through near-intel | 0.010728 |
| 1 | Northwind invoice, 1,250 HUSD to `northwind-paper.testnet` after trusting `northwindpaper.testnet` | braked 72 h: changed details, look-alike payee, amount mismatch, reviewer agreed | 0.010640 |
| 2 | dinner split, 12.5 HUSD to `ravi.testnet` (honest) | braked 24 h by mistake: the payee's reputation counted this hold's own flag; first fail-closed, because the 20-second hold ended before the check | 0.020727 |
| 3 | honest note from Mum saying "pre-approved", 15 HUSD | braked 72 h by mistake: the investigator alone read it as aimed at the checker, and the payee's reputation counted this hold's own flag; first fail-closed, because the 20-second hold ended before the check | 0.000416 |
| 4 | injection-only sample, 15 HUSD to `ravi.testnet` | braked 72 h: the rules and the guardrail flagged the line aimed at the checker | 0.000504 |
| 5 | a message linking the training invoice page, 40 HUSD to `northwind-paper.testnet` | braked 24 h on the triage model's impersonation reading; first fail-closed, because the 20-second hold ended before the check | 0.000363 |

