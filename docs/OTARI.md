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

Dates are UTC. Runs 1 and 2 were on 2026-10-31 on the in-memory vault; runs 3 and 4 were on
2026-11-01 on NEAR testnet (see the next section). Request ids are shortened to their first 8
characters; the full ids are in the sections below.

| Rubric item | Status | Evidence | Code |
|---|---|---|---|
| Open-weight models (20, +10 for two or more) | Verified live: three open-weight models | 2026-10-31: triage `77016f08`, investigator `7a358dcb`, reviewer `72b3f1d9`; run 3: triage `07cf8e8a`, investigator `c24673f3`; run 4: triage `434e9135`, investigator `fe379661`, reviewer `a52d662f`; all served by deepinfra | `holdup/risk/otari.py`, `holdup/risk/pipeline.py` |
| Model routing and fallbacks | Routing and headers verified live. A fallback shown once, in run 4, and it was HoldUp's own switch to another model family after an empty answer; the gateway's own fallback is not shown | runs 2 to 4: provider, served model, fallback and attempts on every step a provider served. Run 4, hold 2: investigator `718804c7`, fallback yes, attempts 2, note `empty_answer`; `/stats.json`: 1 fallback in 12 steps | `FALLBACK_CHAIN`, `fallbacks_for`, `chat` in `otari.py` |
| MCP integrations | Verified live, again on testnet in runs 3 and 4 | run 2: the gateway called near-intel on every hold; an out-of-scope call refused on hold 1. Run 3: three near-intel calls in `c24673f3`. Run 4: calls on holds 2, 3 and 5, none refused | `holdup/mcp_server.py`, `_mcp_servers` in `pipeline.py` |
| Web search or web fetch | Both verified live on commit 9e03a8b (fetch in runs 3 and 4, search in run 4); search also in run 1 | fetch in `c24673f3` (run 3) and `718804c7` (run 4); search in `17299991` (run 1) and `718804c7` (run 4), where a finding rests on two search sources; `otari_web_search` also on the investigator steps `439d9bb7` and `fe379661` | `_investigate` in `pipeline.py` |
| Code execution | Verified live; on testnet in run 4 | `17299991` (run 1), `72b3f1d9` (run 2); run 4: reviewer `a52d662f` (line items 1,150 against a total of 1,250) and `253fdee8` | `pipeline.py`, `_fill` in `otari.py` |
| Budgets and spending limits | Implemented, not yet shown live | none | `BudgetExceeded` in `otari.py`, `_fail_closed` in `holdup/engine.py` |
| Multiple models serving distinct roles | Verified live: 4 roles on 3 models, on testnet too | run 2, as for open-weight models, plus the gate `1db2c093`; run 3: triage and investigator on two models, and a gate blocked before any model ran; run 4: triage, a blocked gate, investigator (also by fallback on deepseek) and reviewer | `_review` in `pipeline.py` |
| Guardrails | Verified live: monitor mode, and block mode both passing and blocking | monitor `77016f08`, `26e0860c` (run 2), `07cf8e8a` (run 3), `3f883d6a` flagged and `434e9135` passed (run 4); block passed `1db2c093` (run 2); block blocked `fad581a8` (run 3) and `5c0756d9` (run 4) | `holdup/guard.py`, `_gate` in `pipeline.py` |
| Per-user budgets | Implemented, not yet shown live | none | `KeyPool` in `holdup/risk/budgets.py` |

Still not shown live: the vision model (GLM), the `meta/llama-guard-4-12b` profile, a budget
refusal, per-user keys, and a fallback that the gateway itself makes (`Otari-Fallback: true` on a
single request).

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
- **Run 4, 2026-11-01, 10:51 to 11:10 (filming).** The same deployment (commit 9e03a8b, hosted
  profile), while the demo videos were filmed. Five payments, holds 2 to 6, as real transactions,
  from three more sender accounts, made through the web console (the saved pages do not name the
  channel). The deployment's `/stats.json`, saved when it counted all seven
  holds, is cumulative with run 3: 4 users, 7 holds, 1 paid instantly, 4 brakes, 4 cancels (all
  after a flag), 2 releases, 0 fail-closed, 4 AI checks, 12 model steps, 1 fallback, USD 0.104354.
  Run 3 was 2 holds, 3 steps and USD 0.0108, so run 4 was 5 payments, 3 AI checks, 9 model steps,
  1 fallback and USD 0.093554, which is also the sum of the per-step costs on the saved `/why`
  pages. Saved copies of `/h`, `/why` and `/v` for holds 0 to 6 and of `/stats.json` are kept by the
  author; they are not in this repository. For holds 2, 4 and 5 the on-chain evidence hash equals
  the sha256 of the published `/v/<id>.json`, as for hold 0. The triage, gate and investigator
  prompt versions on the pages are those of run 3. Transactions open on testnet.near.rocks.
  - Hold 2: 50 HUSD to `safe-wallet-77.holdup-soleimani.testnet` from a second sender account,
    with a 331-character message that links the same scam page as hold 0
    ([send](https://testnet.near.rocks/tx/HvdJB7Dhy9HkAVkeLw6jTnupKU54d7Dohk8uUeuyzH8c) at 10:51).
    The rules braked it within the minute
    ([extend_hold](https://testnet.near.rocks/tx/6qDBTb44gbiqAzQj76pDqPp6Rn3kTdze3AAu1ccFqCpD));
    the full check held it for 72 hours at 10:52
    ([extend_hold](https://testnet.near.rocks/tx/8nTNRjTz212JUCbrtpfYDnmtUQ7EdiZrWeimmPjeZEeY));
    its sender cancelled it at 10:52. Steps: triage (`openai/gpt-oss-20b`, request
    `3f883d6a-33fe-4ae1-9d6c-079299de941c`, deepinfra, monitor flagged, USD 0.000057); a gate that
    the guardrail blocked (`5c0756d9-f466-4db7-acfd-123d064f91e5`, 468 ms, no provider, no cost);
    and the investigator, whose first answer, from `openai/gpt-oss-120b`, was empty, so HoldUp's
    own chain asked `deepseek/deepseek-v4.1-flash-1017` (request
    `718804c7-09c8-4448-a451-4a2702a8ae5a`, deepinfra, 31.8 seconds, USD 0.031118; see Model
    routing and fallbacks). That answer used `otari_web_fetch` and `otari_web_search` and the
    near-intel server, and the findings carry `[web fetch]` and `[web search, 2 sources]` tags. The
    evidence JSON, 4,056 bytes, records the fallback and the two attempts. To fit the 4,096-byte cap
    the builder dropped its citations and cut its details to 80 characters, as `evidence.py` does
    when a verdict is too large; the page still lists the sources.
  - Hold 3: 12.5 HUSD to the practice friend `ravi-4.holdup-soleimani.testnet`, with a
    121-character message
    ([send](https://testnet.near.rocks/tx/66pVFN53DUst8e3kdFfRpJ33Yizq7ksnygNHWTjx5Md1) at 10:52).
    The message is private; the pages show only its length. Three models read it:
    triage (request `193d1e96-9b51-4e2d-92fc-cda77f3e4ba1`, monitor passed, USD 0.000062), the
    investigator (`openai/gpt-oss-120b`, `439d9bb7-d6ae-4206-9974-bf81be83de3c`, 48.2 seconds,
    USD 0.020924) and a reviewer (`deepseek/deepseek-v4.1-flash-1017`,
    `253fdee8-7833-4e7e-ae96-d10a62379455`, 2.7 seconds, USD 0.01016); the pages do not say why a
    reviewer was asked. No fallback, and no finding: the payee's age is shown as context only.
    The payment got no brake and was released at 10:57, after the 5-minute wait
    ([release](https://testnet.near.rocks/tx/68jcHTz6wsMCFpYpMwmLgQwkfv6vn7B9QsDUTjGfpRXM)).
    There is no evidence JSON, because no brake was recorded. The three steps cost USD 0.031146.
  - Hold 4: 20 HUSD to `safe-wallet-77.holdup-soleimani.testnet` from a third sender account, with
    no message on its page, so no model was asked (0 model steps)
    ([send](https://testnet.near.rocks/tx/5Bng35mguwfjqHSDosjZcRRTTRyJfwfjRzxRH2oF8EBs) at 11:08).
    The rules braked it for 24 hours on two findings: the payee's age, and "2 earlier senders
    cancelled after a warning; latest report less than an hour ago" (the reason `PAYEE_REPORTED`).
    Holds 0 and 2 are the two earlier cancels to this payee, by two different senders. The rules
    wrote the brake twice in the same minute
    ([first](https://testnet.near.rocks/tx/EKgnFNvjhV8AgMLzS5diqbFgUkzzWY6bq7X71NRNwpqC),
    [second](https://testnet.near.rocks/tx/ExY7M8CDhgh5it7SKumYms8GuZvnJxGYP2VxAfuc6Dev)), and its
    sender cancelled it at 11:08. This is the reports rule working on the deployed app; the earlier
    runs described here did not show it.
  - Hold 5: 1,250 HUSD to `northwind-paper.holdup-soleimani.testnet` from a fourth sender account,
    which had a trusted payee `northwindpaper.holdup-soleimani.testnet`, with a 459-character
    message ([send](https://testnet.near.rocks/tx/3drDfWStr1tR79pzvjHwLzHT1g9GXp86EtcVyyPFLoHs)
    at 11:09). The rules braked it within the minute
    ([extend_hold](https://testnet.near.rocks/tx/EgZyyZicg62E64ohBkme77ekbxrhihZ4qZa9F38YWTXN));
    the full check held it for 72 hours at 11:10
    ([extend_hold](https://testnet.near.rocks/tx/EJphSuc81vY6dzSaoC7CMdASQiCXn81NnBe6gEigaz41));
    its sender cancelled it at 11:10. Steps: triage (`434e9135-c2ce-4f17-a90e-8dfa6be9fbde`,
    monitor passed with score 0.0, USD 0.000102), the investigator
    (`fe379661-1ab2-479c-98c6-b38ec207e9da`, 56.9 seconds, USD 0.020976) and the reviewer
    (`a52d662f-f64f-44e9-84f3-e219be034de0`, 4.0 seconds, USD 0.010155), which ran
    `otari_code_execution`. No fallback. The findings are a new payee, changed details, a
    look-alike of the trusted payee, a deadline, and "line items add up to 1,150.00 but the stated
    total is 1,250.00" (see Code execution).
  - Hold 6: 40 HUSD from the same sender to the trusted payee `northwindpaper...` ("a trusted payee
    within the daily cap"), paid instantly at 11:10
    ([send](https://testnet.near.rocks/tx/ELTo9zLfJz89jAcdPD4jfxehnqXLpkFMFfxuD97Nw8M4)). No hold,
    no AI step, no evidence.
- **Gateway probes, 2026-10-31, mostly between 12:06 and 12:29.** Direct calls to the gateway, not
  through the app, to learn its behaviour. Some are in `fixtures/otari/recorded/`. They show what
  the gateway does, not what HoldUp does, so no item below counts as verified on a probe alone.

## Open-weight models (rubric: 30 points)

Every model HoldUp asks for is listed as `open_weights: true` in the gateway's catalog
(`fixtures/otari/recorded/catalog-models.json`, recorded 2026-10-29). Model ids are in
`holdup/risk/otari.py`; the steps are in `holdup/risk/pipeline.py`.

| Model | Role | Status |
|---|---|---|
| `openai/gpt-oss-20b` | triage: reads the forwarded message and reports signals; also the gate request that carries the blocking guardrail | **Verified live on 2026-10-31** (runs 1 and 2; for example triage request `77016f08-7f60-406f-a360-bc4768a11ef8`, provider deepinfra, USD 0.000081) **and on 2026-11-01** (run 3, triage request `07cf8e8a-2500-4459-bdfb-e6ad9ab9839d`, deepinfra, USD 0.000062; run 4, for example `434e9135-c2ce-4f17-a90e-8dfa6be9fbde`, deepinfra, USD 0.000102) |
| `openai/gpt-oss-120b` | investigator: checks the payee and the links with MCP, web search, web fetch and code execution | **Verified live on 2026-10-31** (runs 1 and 2; for example request `7a358dcb-447b-4996-ba4b-1d4d1c15accd`, deepinfra, USD 0.010647) **and on 2026-11-01** (run 3, request `c24673f3-c063-41be-83b0-8b4500731150`, deepinfra, 56 seconds, USD 0.010738; run 4, requests `439d9bb7-d6ae-4206-9974-bf81be83de3c` and `fe379661-1ab2-479c-98c6-b38ec207e9da`, deepinfra, USD 0.020924 and USD 0.020976). On hold 2 of run 4 its answer was empty and HoldUp moved on (see Model routing and fallbacks). |
| `deepseek/deepseek-v4.1-flash-1017` | reviewer: a blind second opinion from another model family; also the investigator's first fallback | **Verified live on 2026-10-31** (request `72b3f1d9-c7d7-45de-a1eb-6e92c4fc75f2`, deepinfra, served as `deepseek-ai/DeepSeek-V4.1-Flash-1017`, USD 0.010138) **and on 2026-11-01** (run 4, reviewer requests `a52d662f-f64f-44e9-84f3-e219be034de0`, USD 0.010155, and `253fdee8-7833-4e7e-ae96-d10a62379455`, USD 0.01016, deepinfra, both served as `deepseek-ai/DeepSeek-V4.1-Flash-1017`). Also **shown as the investigator's fallback** in run 4 (request `718804c7-09c8-4448-a451-4a2702a8ae5a`, deepinfra, same served model, USD 0.031118). |
| `z-ai/glm-5.3-flash` | vision: reads an image the sender shares; also the last fallback family | **Implemented, not yet shown live** in the app. The web console takes text only; Telegram images reach it. A gateway probe on 2026-10-31 read an invoice image with it. |
| `openai/gpt-oss-safeguard-120b` | the `checker-injection` classifier behind HoldUp's `/validate` endpoint, with HoldUp's written policy (`holdup/risk/guard_models.py`) | **Partly verified.** In runs 2 to 4 the gateway called `/validate` and its verdicts came back (see Guardrails). The endpoint falls back to phrase rules when the classifier fails, and the saved pages do not record which of the two answered, so this model's part is not verified. |
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

**Status: routing and the headers verified live on 2026-10-31 and 2026-11-01; a fallback shown
once, on 2026-11-01, and it was HoldUp's own switch to another model family, not the gateway's.**
Routing and the headers: in run 2 every model step that a provider served recorded provider
deepinfra, attempts 1 and fallback no (the two refused gate requests recorded no provider), and
the catalog id `deepseek/deepseek-v4.1-flash-1017` was served as the provider's
`deepseek-ai/DeepSeek-V4.1-Flash-1017`. Runs 3 and 4 recorded the same on every other step a
provider served (deepinfra, attempts 1, fallback no; the blocked gate requests recorded no
provider).

The fallback is hold 2 of run 4. Its investigator step shows the model
`deepseek/deepseek-v4.1-flash-1017` with a "fallback" badge, provider deepinfra, served as
`deepseek-ai/DeepSeek-V4.1-Flash-1017`, Fallback yes, Attempts 2, request
`718804c7-09c8-4448-a451-4a2702a8ae5a`, 31,796 ms, USD 0.031118, and this note:
"openai/gpt-oss-120b: the model answered with no text after 1 rounds of tools (empty_answer); asked
deepseek/deepseek-v4.1-flash-1017". So the first model, `gpt-oss-120b`, answered with nothing after
one round of tools, HoldUp's own chain asked the next family, and its answer was used; the step
went on to use the gateway's web fetch and web search and near-intel (see those sections). The
evidence JSON, whose hash is on-chain, records `fallback: true` and `attempts: 2` for the step.
`/stats.json` counts it: 0 fallbacks over 18 steps in run 2, 0 over 3 in run 3, 1 over 9 in run 4.
What the page does not show:

- The empty first request has no row, request id or cost of its own. The provider and served model
  on the page are those of the answer that was used, and HoldUp adds the cost of the unusable first
  answer to the step (`chat` in `otari.py`), so the USD 0.031118 covers both.
- Whether the gateway itself fell back. `_fill` in `otari.py` sets a step's Fallback to yes when
  HoldUp's chain moved on or when the `Otari-Fallback` header says true, and adds each request's
  `Otari-Attempt-Count` into Attempts; the page shows only the merged values. Attempts 2 is one
  attempt per request as HoldUp counts (each request counts at least one), but the page cannot show
  the gateway's header by itself. What it proves is HoldUp's own fallback.

**Gateway-side fallbacks: implemented, not yet shown live.** The offline tests replay a
cross-family fallback (`fixtures/otari/all_candidates_failed.json`, synthetic).

## MCP integrations

**What HoldUp does.** It runs its own MCP server, `near-intel`, at `/mcp` (`holdup/mcp_server.py`)
and passes it to the investigator in each request (`mcp_servers`, `_mcp_servers` in
`pipeline.py`), with a bearer token that only opens data about that hold's sender and payee. Its
tools: `payee_profile`, `payee_reputation`, `sender_history`, `hold_status`, `fetch_page`. The
server logs each call, and `/why/<id>` lists them, because Chat Completions does not report MCP
calls.

**Status: verified live on 2026-10-31** (run 2) **and again on NEAR testnet on 2026-11-01** (runs
3 and 4). In run 2 the gateway called `/mcp` with the per-hold token:

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

In run 4 the gateway called near-intel on every hold that had an investigator step, and none of
the logged calls is marked refused:

- hold 2: `payee_profile`, `hold_status`, `payee_profile`, `payee_reputation` and `sender_history()`
  (the log is per hold, so it does not say which of the two investigator requests made which call;
  `hold_status` had not been logged before);
- hold 3: `payee_profile(ravi-4.holdup-soleimani.testnet)` and
  `payee_reputation(ravi-4.holdup-soleimani.testnet)`;
- hold 5: `payee_profile(northwind-paper.holdup-soleimani.testnet)` and `sender_history()`.

Findings tagged `[MCP near-intel]` came from these: the payee's age on holds 2, 3 (as context) and
5, and on hold 2 a note that the hold was flagged for injection attempts in the withheld message.

Run 2 also showed two faults, fixed in 9e03a8b: `payee_reputation` counted the hold under check
and the sender's own earlier payments, and the model had to guess the sender's account for
`sender_history` (the history tools now default to the hold's own parties). Run 3 shows the second
fix at work: `sender_history()` came with no arguments and was answered, and run 4 shows it again
on holds 2 and 5. Whether the reputation now leaves out the hold under check is not visible on the
saved pages of runs 3 and 4.

## Web search or web fetch

**What HoldUp does.** The investigator is offered `otari_web_search` (at most three searches) and,
when the message has links, `otari_web_fetch` (`_investigate` in `pipeline.py`). A claim of a
public warning counts only if it cites a page the gateway reports the search returned, and a
phishing-page claim only if the page was read. If the workspace has web access off, the tool is
dropped, the step is retried without it and a note says so on `/why`.

**Status: web fetch verified live on 2026-11-01 (runs 3 and 4); web search verified live on
2026-10-31 (run 1) and again on 2026-11-01 (run 4), so both are shown on commit 9e03a8b.**

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
- Run 3 made no web search.
- Run 4, hold 2 (investigator request `718804c7-09c8-4448-a451-4a2702a8ae5a`, the answer HoldUp
  asked for after an empty first one, near-intel attached): the step lists `otari_web_fetch` and
  `otari_web_search`. Three findings are tagged `[web fetch]` on the training page the message
  linked: it asks the reader for a full 12-word recovery phrase on a look-alike login form, it
  imitates a CoinHaven security page to collect wallet credentials, and it threatens a freeze
  within 30 minutes. One finding is tagged `[web search, 2 sources]`: "Matches public fraud
  warnings", "Wallet-safety sources say no legitimate service ever asks for a recovery phrase",
  citing two public pages, one on foxwallet.com and one on robinhood.com. HoldUp counts a
  public-warning finding only if it cites a page the gateway reports its search returned, so the
  search completed with those results. This is a finding that rests on a search, seen live on
  9e03a8b.
  The query is not on the page, and the evidence JSON of this hold dropped its citations to fit
  its size cap (see Run 4 above), so the sources are on `/h/2` and `/why/2` only.
- Run 4, holds 3 and 5: the gpt-oss-120b investigator steps (`439d9bb7-d6ae-4206-9974-bf81be83de3c`
  and `fe379661-1ab2-479c-98c6-b38ec207e9da`) list `otari_web_search` among their tools; the Tools
  line lists a call whether or not it completed. No finding of either carries a search tag (hold 3
  had no finding, and hold 5's findings are tagged rules, triage, investigator, near-intel,
  code execution and reviewer). So all three investigator steps of run 4 called the search, as
  9e03a8b asks.

## Code execution

**What HoldUp does.** The investigator and the reviewer are offered `otari_code_execution` and are
asked to compute every number they rely on. HoldUp then redoes the arithmetic of an amount
mismatch on the numbers in the message before the finding counts. Chat Completions does not report
sandbox runs, so HoldUp infers them from rounds of tools in which the gateway reported no other
call, when no MCP server is attached (`_fill` in `otari.py`).

**Status: verified live on 2026-10-31, and again on testnet on 2026-11-01.**

- Run 1, hold 1: the investigator's answer lists the code it ran, `print(600.00 + 350.00 + 200.00)`
  with result `1150.0`, against a stated total of 1,250.00 (request
  `17299991-165c-49e9-be2a-adf5bc22bb79`). The step cost USD 0.020652, against USD 0.000263 for
  the same model's step on hold 0, which is consistent with sandbox runs.
- Run 2, hold 1: the reviewer (request `72b3f1d9-c7d7-45de-a1eb-6e92c4fc75f2`) shows
  `otari_code_execution` among its tools and cost USD 0.010138, against USD 0.000051 for a reviewer
  step without a sandbox run on hold 3; its finding "line items add up to 1150.00 but the stated
  total is 1250.00 (off by 100.00)" matched HoldUp's own arithmetic.
- Run 4, hold 5 (the Northwind invoice, commit 9e03a8b): the reviewer step (request
  `a52d662f-f64f-44e9-84f3-e219be034de0`, deepseek, deepinfra, 4.0 seconds, USD 0.010155) lists
  `otari_code_execution` as its tool and cost about what run 2's sandbox reviewer did, against USD
  0.000051 for a reviewer without one. The reviewer has no MCP server, so HoldUp infers the sandbox
  run as described above; the gateway did not report it. The reviewer's finding says the line items
  add up to 1150.00 against a stated total of 1250.00 (off by 100.00). The rules' own finding on the
  hold says 1,150.00 against 1,250.00, and the page tags the sums finding `[rules]`,
  `[code execution]` and `[reviewer deepseek-v4.1-flash-1017]`. HoldUp counts a model's amount
  mismatch only after its own arithmetic on the numbers in the message confirms it
  (`verify_amount_check` in `pipeline.py`), so the figures were confirmed by HoldUp, not only
  reported by a model. The `[code execution]` tag comes from the investigator's own sums finding in
  the evidence JSON ("line items add up to 1150.0"), which passed the same check; the investigator
  step lists no sandbox run, as in the last item below.
- Run 4, hold 3 (an honest payment): the reviewer (request
  `253fdee8-7833-4e7e-ae96-d10a62379455`, 2.7 seconds, USD 0.01016) also lists
  `otari_code_execution`, at about the same cost. No finding came of it.
- Not shown on the pages: when the MCP server is attached, a sandbox run is not inferred, so
  investigator steps in runs 2 and 4 show no code execution even where the cost suggests one.

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

**Status: verified live on 2026-10-31, and on testnet on 2026-11-01,** for four roles on three
models: triage and gate
(`gpt-oss-20b`), investigator (`gpt-oss-120b`) and reviewer (`deepseek-v4.1-flash`), all in run 2.
Run 1 used triage and the investigator; its only reviewer answer was rejected as malformed
(`bad_json`, request `87240c05-29af-452c-914b-e8e8699a5a2e`), and the 19:01 CLI rerun had a usable
reviewer answer (request `fe20a3d6-d52e-48d4-9ccb-d5106f923fec`). Run 3 (2026-11-01, on testnet)
used triage (`gpt-oss-20b`) and the investigator (`gpt-oss-120b`), with a gate request between
them that the guardrail blocked before any model ran; the reviewer was not asked. Run 4 (also on
testnet) added the reviewer on holds 3 and 5 (`253fdee8`, `a52d662f`) and a second blocked gate on
hold 2 (`5c0756d9`), so all four roles have now run on testnet, though no single hold used all
four. On hold 2 the investigator role was served by `deepseek-v4.1-flash` after the fallback; the
page lists two models for that hold's three steps. The vision and classifier roles are as stated
in the model table.

## Guardrails

**What HoldUp does.** It serves a guardrail endpoint, `POST /validate` and `GET /profiles`
(`holdup/guard.py`), with a `checker-injection` profile for text written to steer an AI checker. The
triage and investigator requests carry it in monitor mode; triage reads the verdict from the
`Otari-Guardrails` response header. When anything suspects an injection, a separate gate request
carries it in block mode, and text that is blocked, or that the gate could not judge, never
reaches the models with tools (`_guardrails`, `_gate` and `withheld` in `pipeline.py`). Each
request carries a per-hold token in `validate_kwargs`.

**Status: monitor mode verified live on 2026-10-31 and 2026-11-01; block mode verified live, with
a pass on 2026-10-31 and blocks on 2026-11-01 (runs 3 and 4).**

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
- Monitor (run 4): the triage row of hold 2 (request `3f883d6a-33fe-4ae1-9d6c-079299de941c`) shows
  `checker-injection, monitor: flagged` with score 1.0, and the hold carries a `CHECKER_INJECTION`
  finding from source `guardrail`. The triage rows of hold 3
  (`193d1e96-9b51-4e2d-92fc-cda77f3e4ba1`) and hold 5 (`434e9135-c2ce-4f17-a90e-8dfa6be9fbde`)
  show `monitor: passed`, with score 0.0 on hold 5; neither payment was braked on that account,
  and no gate ran for them.
- Block, blocked (run 4, hold 2): the gate request `5c0756d9-f466-4db7-acfd-123d064f91e5` was
  refused after 468 ms, with no provider and no cost, by a 403 with the error code
  `guardrail_violation` and profile `checker-injection`. HoldUp recorded it as "blocked the text",
  so the later steps saw only facts HoldUp extracted from the message. As in run 3, the bare-403
  case of 9e03a8b has not come up.
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

**Video (4:25): https://youtu.be/nX-clzhrwfU.** It was filmed on 2026-11-01 against the bot's
testnet deployment, through the web console that drives the same engine as the Telegram bot. Captions
are burned into the picture. The payments made while the demo videos were filmed are run 4 above.

## Appendix: the six payments of run 2

Outcomes as the run recorded them, on code older than 009c73e. Hold 3's 72-hour brake has no fix
yet ([JUDGES.md, Known issues](JUDGES.md#known-issues)); the other mistaken brakes are fixed in
9e03a8b. Of these honest payments, only a payment of the kind of hold 2 has been repeated live on
it: 12.5 HUSD to a practice friend with a short message, released with no brake (run 4, hold 3).
The note from Mum (hold 3 below) has not been repeated live.

| Hold | Payment | Outcome then | Cost (USD) |
|---|---|---|---|
| 0 | CoinHaven sample, 50 HUSD to `safe-wallet-77.testnet` | braked 72 h: rules within a second, then a secret request and a phishing page read through near-intel | 0.010728 |
| 1 | Northwind invoice, 1,250 HUSD to `northwind-paper.testnet` after trusting `northwindpaper.testnet` | braked 72 h: changed details, look-alike payee, amount mismatch, reviewer agreed | 0.010640 |
| 2 | dinner split, 12.5 HUSD to `ravi.testnet` (honest) | braked 24 h by mistake: the payee's reputation counted this hold's own flag; first fail-closed, because the 20-second hold ended before the check | 0.020727 |
| 3 | honest note from Mum saying "pre-approved", 15 HUSD | braked 72 h by mistake: the investigator alone read it as aimed at the checker, and the payee's reputation counted this hold's own flag; first fail-closed, because the 20-second hold ended before the check | 0.000416 |
| 4 | injection-only sample, 15 HUSD to `ravi.testnet` | braked 72 h: the rules and the guardrail flagged the line aimed at the checker | 0.000504 |
| 5 | a message linking the training invoice page, 40 HUSD to `northwind-paper.testnet` | braked 24 h on the triage model's impersonation reading; first fail-closed, because the 20-second hold ended before the check | 0.000363 |

