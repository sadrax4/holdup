# ScamBench

HoldUp's benchmark: 170 payment requests, 85 scams and 85 legitimate payments, each with the message
the sender forwarded, the NEAR account they are about to pay, the amount and a small profile of the
payee account. Every scam follows a pattern described in a public US or UK government warning, and
many of the legitimate items are built to look risky: real deadlines, busy shops, brand-new accounts
of real people. [DATASHEET.md](DATASHEET.md) says how the items were made (with AI assistance), what
they can and cannot show, and how to add more; [SOURCES.md](SOURCES.md) lists the 53 warnings.

```
python -m bench.run --check                      # validate the dataset (python -m bench.validate does too)
python -m bench.run --rules                      # the rules layer on the dev split, offline and free
python -m bench.run --rules --split all --out report.md
python -m bench.run --replay                     # triage harness check with recorded answers (no key, no cost)
python -m bench.run                              # live triage run on Otari: needs OTARI_API_KEY, costs a few cents
python -m bench.run --models openai/gpt-oss-20b --limit 4 --split dev
```

Run them from the repository root (`python bench/run.py` works too). `make bench-rules` (dev by
default, `SPLIT=test` or `SPLIT=all` for more) and `make bench` do the same.

## What it measures

### The rules layer: `--rules`

Every item goes through `RulesRisk.assess`, the call the engine makes for its quick provisional
brake, and counts as braked when the engine's own test, `asks_to_brake`, says the verdict would go on
chain. No model is called. The item's payee profile stands in for the chain:

| Item field | What the rules read |
|---|---|
| `message` | the forwarded text (`PaymentContext.forwarded_text`) |
| `payee`, `asset`, `amount` | the hold (USDC is the HUSD demo token, as in the demo) |
| `payee_profile.age_hours` | the account age that `PayeeIntel` reports from the transaction index |
| `payee_profile.distinct_inbound_senders_24h` | the distinct senders of the last 24 hours, from the same index |
| `payee_profile.reports_30d` | the vault's payee stats: `cancelled_after_flag`, with the latest report a day before the check when there is one |
| `trusted_lookalike` | the sender's on-chain trusted payees (none when it is null) |
| `payee_profile.prior_releases_from_sender` | not read: the rules do not look at the sender's history (the investigator does, through near-intel) |

Every payee account exists: the engine refuses a payment to a missing account before any check.

The report gives the confusion matrix, precision, recall and the false-hold rate on benign items for
the split asked for (with `--split all`, one row each for dev, test and all items); precision and
recall per reason against `expected_reasons` (a reason the
rules only pass on as context counts as cited); results per category; the minimal pairs; and the ids
of every scam the rules miss and every benign item they brake. PHISHING_LINK and PUBLIC_WARNING need
a fetched page or a web search, so no rule checks them. Current numbers, next to the rules before
they were tuned: [results-rules.md](results-rules.md).

### The triage prompt

For every item and model, the triage step runs once with HoldUp's triage prompt and the payment facts
the pipeline gives it; a model brakes an item when it reports any signal. The report gives precision,
recall, the false-hold rate, the median latency and the mean cost per check, as reported by Otari in
`usage.cost_usd`. A `--replay` run only proves that the harness works: its answers are recorded per
label, so its numbers mean nothing.

This measures the prompt, not the pipeline's decision. The pipeline weighs what triage reports
(`weigh_claims` in `holdup/risk/pipeline.py`): a request for a secret, text aimed at the checker,
changed payment details or impossible returns brake alone, while impersonation and pressure brake
only when something independent agrees: a strong finding, a reason of the rules, a weak signal of the
rules for another reason (not a young payee), or the reviewer, a model of another family, answering
that the payment should wait, whatever reasons it lists. The reviewer's own judgement needs a judgement
of another family or a weak signal of the rules, a young payee included. On their own they are kept as
context; when the reviewer they wait for fails, the check fails closed and, once the engine's retries
are spent, holds the day such a judgement held for before. The bench facts also name the item's asset (USDC)
where the pipeline names the HUSD demo token and says that it stands in for USDC.

## Dev and test

Each item has a fixed `split`: 102 dev items (53 scam, 49 benign) and 68 test items (32 scam, 36
benign), stratified by label and category, with the two halves of every minimal pair in the same
split. Change checks while looking only at dev, then measure test once with the change frozen, and
report dev, test and all items. The rules mode shows dev unless asked and the regression test pins
only the dev counts, so a change can be judged without seeing test. The procedure is in
DATASHEET.md, section 4a, which also says why the test split was not held out for the first tuning
of the rules.

## Format

`scambench.jsonl` holds one JSON object per line, fields in this order:

| Field | Meaning |
|---|---|
| `id` | `sb-scam-NNN` or `sb-benign-NNN`, unique |
| `label` | `scam` or `benign` |
| `category` | One of the categories in DATASHEET.md, section 2 |
| `message` | The text the sender saw and forwarded, 1 to 6 sentences, plain ASCII, line breaks allowed |
| `payee`, `asset`, `amount` | The payment: a fictional `.testnet` account, `USDC` or `NEAR`, a decimal string |
| `payee_profile` | `age_hours`, `distinct_inbound_senders_24h`, `prior_releases_from_sender` and optionally `reports_30d` |
| `trusted_lookalike` | The sender's trusted payee whose name the payee imitates, or null |
| `expected_reasons` | HoldUp reason names a correct checker should cite; empty for benign items |
| `source_url`, `source_licence` | For a scam, the public warning it follows and that page's licence; null for benign items |
| `notes` | Why the label holds; "Hard case:", "Twin:" and "Compare:" markers |
| `split` | `dev` or `test` |

## Adding items

Follow DATASHEET.md, section 9. In short: every brand, person, account and link is fictional
(links on `.example` domains only); every message is written fresh, never copied from a source; a
scam item cites a public-domain or Open Government Licence warning that is listed in SOURCES.md;
benign items get the hard cases; no backticks or angle brackets, because the hosted gateway's
firewall rejects backticks (otari issue #2704). Then give the item a split with
`python -m bench.validate --assign-splits` and run `python -m bench.run --check`.

## Licence

The ScamBench data (`scambench.jsonl`, DATASHEET.md, SOURCES.md) is released under CC BY 4.0; see
[LICENSE](LICENSE) for the attributions it carries. The 20 hand-written CC0 items that this folder
held before were dropped because version 1.0 covers each of their patterns (the table is in
DATASHEET.md, section 7). The code here is part of HoldUp and under the repository's MIT licence.
