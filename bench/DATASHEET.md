# ScamBench datasheet

Version 1.1, 31 October 2026. Maintained by the HoldUp project team. Version 1.1 holds the same 170 items as version 1.0 and adds a fixed dev/test split (see the changelog).

ScamBench is a labelled set of 170 payment requests (85 scams, 85 legitimate) for testing HoldUp's risk pipeline. Each record holds the message a user saw and forwarded, the NEAR testnet account they are about to pay, the asset and amount, and a small profile of the payee account. Every scam follows a pattern described in a US or UK government warning that is public domain or openly licensed. The legitimate requests resemble ordinary payments, including many that look risky at first glance.

Files in this folder:

| File | Contents |
|---|---|
| `scambench.jsonl` | The 170 items, one JSON object per line |
| `SOURCES.md` | The 53 public warnings cited by scam items: URL, title, publisher, date on page, date read, licence, items |
| `DATASHEET.md` | This document |
| `LICENSE` | The licence of the data (CC BY 4.0) and the attributions it needs |
| `validate.py` | Schema, consistency, hygiene and split checks plus composition counts (Python 3, standard library only); `python -m bench.run --check` runs it too |
| `run.py` | The benchmark: HoldUp's rules layer offline (`--rules`), or the triage prompt on the Otari gateway |
| `results-rules.md` | Results of HoldUp's rules layer before and after it was tuned (both splits in-sample, see section 4a) |

The version 1.0 folder also had `to_run_py_format.py`, which converted items to the older format of `run.py`, and `rules_baseline.py`, which scored the rules layer. `run.py` now reads this format directly and `run.py --rules` replaces the baseline script, so neither is kept.

## 1. Motivation

HoldUp holds a payment to a new payee for a short time. While the money waits, deterministic rules and open-weight models check it, and an oracle can extend the hold but never shorten it. A fixed, realistic test set makes it possible to measure:

- how many scams the pipeline brakes (recall);
- how often it brakes legitimate payments (false-hold rate), which is the cost users feel;
- whether it names the right reasons, since the reasons are recorded on-chain and shown to the user;
- whether lines written to manipulate an AI checker move it;
- latency and cost per check for each model and step.

ScamBench is meant for evaluation and demos. It is not training data for a production fraud model, it says nothing about how common each scam is, and it makes no claim about any real person, company or account.

## 2. Composition

### Labels and categories

| Scam category | Items | Benign category | Items |
|---|---|---|---|
| fake_support_safe_wallet | 7 | friend_split | 9 |
| unlock_or_withdrawal_fee | 6 | rent_known_landlord | 7 |
| investment_impossible_returns | 6 | supplier_invoice_consistent | 10 |
| romance_emergency | 6 | marketplace_purchase | 9 |
| supplier_wallet_change | 7 | donation | 7 |
| impersonation_exchange | 6 | family_support | 9 |
| impersonation_government | 8 | business_payroll | 7 |
| job_task_scam | 6 | urgent_but_legit | 10 |
| giveaway_doubling | 5 | new_payee_but_legit | 10 |
| recovery_scam | 6 | other | 7 |
| marketplace_overpayment | 5 | | |
| sextortion_payment | 5 | | |
| other | 12 | | |
| **Total** | **85** | **Total** | **85** |

Scam "other" covers executive (CEO) fraud, a 'mistaken deposit' refund, a new-number family emergency, a grandparent bail scam, virtual kidnapping, a fake disaster appeal, a misleading trade mark register invoice, data-leak extortion, salary diversion, payroll portal phishing, a fake NFT mint and a cloned-voice emergency. Benign "other" covers a club renewal, a refund to a customer, a prize payout, a bug bounty, a car club statement, an allotment society with false reports, and a loyalty top-up with bonus credit.

### Expected reasons on scam items

| Reason | Items | Reason | Items |
|---|---|---|---|
| PUBLIC_WARNING | 85 | PHISHING_LINK | 12 |
| NEW_ACCOUNT | 53 | IMPOSSIBLE_RETURNS | 11 |
| URGENCY | 47 | CHECKER_INJECTION | 9 |
| MULE_PATTERN | 44 | DETAILS_CHANGED | 9 |
| IMPERSONATION | 43 | AMOUNT_MISMATCH | 6 |
| PAYEE_REPORTED | 18 | LOOKALIKE_PAYEE | 6 |
| | | SECRET_REQUEST | 6 |

### Hard cases

Items written to be difficult carry notes that start with "Hard case:" (20 scams, 44 benign).

- **Calm scams.** Polite tone, no deadline, older payee accounts below every rules threshold, or accounts the sender has paid before (12 scam items have earlier releases from the sender). 18 scam items trigger no profile-based reason at all, and 7 have PUBLIC_WARNING as their only expected reason, so only recognising the pattern catches them. sb-scam-043 sits just past every threshold (75 hours old, 7 senders, 1 report).
- **Legitimate payments that look risky.** 34 benign items go to a payee the sender has never paid; 6 go to accounts younger than 72 hours; 33 go to busy accounts with 8 or more distinct senders in a day (shops, charities, landlords, organisers); 17 ask for payment immediately or by a near deadline; one payee name is a single letter away from a trusted payee (sb-benign-059); one payee has two reports from an earlier false flag (sb-benign-084); five use wording that cheap filters can mistake for injection or changed details ("Ignore my last message", "please disregard", "pre-approved").
- **Prompt injection.** 9 scam items (10.6 percent) contain a line addressed to an AI or automated checker, in different forms: a note to "any AI assistant", a fake SYSTEM message, an order to "automated compliance filters" citing a fictional statute, a remittance note telling payment checkers to skip review, and a parenthetical aimed at "the safety bot".
- **Minimal pairs.** Eight scam and benign twins share a setting and differ only in the decisive signals: the Northwind invoice (sb-scam-026 / sb-benign-017), the Northwind 'pre-approved' note (sb-scam-027 / sb-benign-018), a friend's account (sb-scam-057 / sb-benign-066), an overpayment versus a genuine refund (sb-scam-064 / sb-benign-080), a child's real account versus a lookalike (sb-scam-076 / sb-benign-049), a disaster appeal (sb-scam-079 / sb-benign-038), payroll (sb-scam-082 / sb-benign-052) and a family voice note (sb-scam-085 / sb-benign-051). Their notes point to each other.

### Other properties

- Assets: USDC 145, NEAR 25. "USDC" stands for the stablecoin leg; in HoldUp's sandbox and testnet demos it maps to the HUSD demo token unless Circle USDC is configured.
- Amounts: scam USDC amounts run from 1 to 24,600 (median 882.50); benign USDC amounts from 15 to 5,040 (median 150).
- Messages: 1 to 6 sentences (median 3; each line of an itemised invoice counts as one), 14 to 87 words (median about 40), English, plain ASCII. The Northwind pair (sb-scam-026 and sb-benign-017) keeps the line breaks of an itemised invoice; every other message is a single paragraph, so arithmetic checks also meet flattened text. They contain no backticks or angle brackets, because the hosted Otari gateway's web firewall rejects backticks. Links appear in 12 scam and 3 benign messages, always on reserved `.example` domains.
- The two HoldUp demo stories are included: the CoinHaven 'protected wallet' message with an injection line (sb-scam-001) and the Northwind Paper Co. wallet change whose line items add up to 1,150 against a 1,250 total (sb-scam-026).

## 3. Record format

One JSON object per line, fields in this order:

| Field | Type | Meaning |
|---|---|---|
| `id` | string | `sb-scam-NNN` or `sb-benign-NNN`, unique |
| `label` | string | `scam` or `benign`: whether a careful sender who knew the full story should stop the payment |
| `category` | string | One of the categories in section 2 |
| `message` | string | The text the sender saw and forwarded (a DM, email, text or voice-note transcript), 1 to 6 sentences; may contain line breaks |
| `payee` | string | Fictional NEAR testnet account the sender is about to pay |
| `asset` | string | `USDC` or `NEAR` |
| `amount` | string | Decimal amount in whole token units, no thousands separators (for example `"2236.80"`) |
| `payee_profile` | object | See below |
| `trusted_lookalike` | string or null | The sender's trusted payee whose name this payee imitates, if any |
| `expected_reasons` | list | Reason names a correct checker should cite as grounds to brake; empty for benign items |
| `source_url` | string or null | For scams, the public warning that describes the pattern; null for benign items |
| `source_licence` | string or null | Licence of that warning; null for benign items |
| `notes` | string | Why the label holds and which signals matter; "Hard case:", "Twin:" and "Compare:" markers |
| `split` | string | `dev` or `test` (section 4a) |

`payee_profile` fields:

- `age_hours`: age of the payee account in hours.
- `distinct_inbound_senders_24h`: distinct accounts that paid the payee in the last 24 hours.
- `prior_releases_from_sender`: earlier HoldUp payments from this sender to this payee that were released.
- `reports_30d` (addition to the requested three fields): other senders who cancelled a flagged payment to this payee in the last 30 days, which is the on-chain `cancelled_after_flag` count behind the "reports" rule. Treat it as 0 when absent.

Reason names are exactly those of `Reason` in `holdup/models.py`, listed in bit order.

## 4. How labels and reasons were assigned

**Labels.** Each scam reproduces a fraud pattern that its cited warning describes. Each benign item was written so that the full context in the message makes it legitimate: the service was already delivered, the account is the usual one, a quote or contract matches, or the people met in person.

**Profile-based reasons** were applied mechanically to scam items, using the rule thresholds in the HoldUp build spec:

- NEW_ACCOUNT if `age_hours` < 72
- MULE_PATTERN if `distinct_inbound_senders_24h` >= 8
- PAYEE_REPORTED if `reports_30d` >= 2
- LOOKALIKE_PAYEE if `trusted_lookalike` is set (by edit distance, separators or an added prefix or suffix)

**Content-based reasons** were assigned by reading the message:

- IMPERSONATION: claims to be support, a bank, a platform, a government body, an employer, or a known person writing from a new channel.
- URGENCY: an explicit deadline or a threat tied to time.
- IMPOSSIBLE_RETURNS: guaranteed, multiplied or doubled returns.
- CHECKER_INJECTION: a line addressed to an AI or automated checker.
- DETAILS_CHANGED: payment details changed by message.
- PHISHING_LINK: a link to a log-in, verification, claim or wallet-connect page.
- AMOUNT_MISMATCH: numbers in the message that do not add up. Every such claim was checked by script, and every sum, split and percentage in benign items was checked to be correct.
- SECRET_REQUEST: asks for a recovery phrase, password or one-time code.

**PUBLIC_WARNING** is listed on every scam item, because each one follows a cited public warning that a checker with web search should be able to find (or an equivalent). It measures how the investigator uses search more than how it judges the case, so score it separately.

**Benign items list no reasons.** Their tempting facts (a young account, many senders, urgent wording, a near-identical name) are visible in the profile and the message and explained in the notes, but none of them should cause a brake on its own.

## 4a. Dev and test split

Version 1.1 fixes a split so that checks can be tuned on one part and measured on the other. It was made before any tuning and is stored in each item's `split` field.

- **Stratified.** Within every (label, category) stratum, `round(0.6 * n)` items go to dev and the rest to test, in the order of `sha256("scambench-split-v1:" + id)`. The result is 102 dev items (53 scam, 49 benign) and 68 test items (32 scam, 36 benign).
- **Twins stay together.** The benign item of each "Twin:" pair goes to the split of its scam twin, so no minimal pair is divided (3 pairs in dev, 5 in test).
- **Fixed.** `python -m bench.validate --assign-splits` gives new items a split by the same procedure and never moves an item that has one. The validator checks that twins share a split and warns when a stratum drifts more than one item from 60 percent.
- **How to use it.** Look only at dev while changing a check; measure test once, with the change frozen, and report dev, test and all items. `python -m bench.run --rules` shows the dev split unless `--split test` or `--split all` is given, so test numbers do not appear while tuning. With 32 scams and 36 benign items in test, a rate on it carries roughly 10 to 17 percentage points of uncertainty either way.
- **Not held out for the first tuning of HoldUp's rules.** The split was cut after the whole set had been read and after a review had found the rule problems that started the tuning on items that ended up in test (sb-scam-024, sb-scam-027, sb-scam-082, sb-benign-018), and test results were visible while the rules were changed. For those rules the test split is in-sample like dev; `results-rules.md` reports it that way and adds 24 items written outside the set. A held-out claim about a later change needs test kept unseen from the start, or a private set.

## 5. How the items were written

1. **Sources.** On 2026-10-31 we fetched 110 web pages and 3 PDFs with `curl` from the FBI Internet Crime Complaint Center, the CFTC, the SEC's investor.gov, GOV.UK, the NCSC and the Home Office's Stop! Think Fraud campaign, read them, and cited 53 (see `SOURCES.md`). FTC, FBI.gov, Action Fraud, Scamwatch and SEC.gov refused plain requests from our environment; we did not try to get around those blocks and cite nothing from them.
2. **Patterns.** For each category we noted what the warnings describe: the request, the pressure, the payment route and the tells.
3. **Messages.** Every message was written fresh in our own words, not adapted from source text. People, brands (CoinHaven, Northwind Paper Co. and other invented names), agencies, accounts and links are fictional; links use the reserved `.example` top-level domain.
4. **Payee profiles** were chosen to fit each scenario and to place items on both sides of the rule thresholds.
5. **Checks.** Every sum, split, percentage and difference in the messages was checked in code while the set was built. The validator (`validate_scambench.py` in version 1.0, `validate.py` now) checks the schema, ids, vocabularies, account ids, amounts, sentence counts, ASCII, link domains, real brand and agency names, label balance, the source of every scam, and the consistency of profile-based reasons. Its copy check, run against the saved text of everything we fetched, found no message sharing a sequence of six or more consecutive words with any source page.
6. **Authorship.** The items were written by the HoldUp project team with AI assistance. They have not yet had an independent human annotation pass (see section 8).

## 6. Uses

**Running the benchmark.** From the repository root, `python -m bench.run --rules` scores HoldUp's rules layer offline on the dev split (`--split test` or `--split all` for the others), and `python -m bench.run` sends the message, payee and amount of every item to the triage step on Otari (`--replay` checks the harness with recorded answers; `--split` limits it to one split). The rules mode gives the rules the payee data the way the engine reads it, as listed below; `bench/run.py` documents the same mapping. For a full pipeline run, give the pipeline the message as forwarded context, the payee, asset and amount, and the payee data:

- PayeeIntel fixture for the payee: `age_hours` and `distinct_inbound_senders_24h`;
- vault payee stats: `cancelled_after_flag = reports_30d`, with `last_report_ms` inside the last 30 days when it is 2 or more (the reports rule ages reports from the latest report, not the latest flag);
- sender history with this payee: `prior_releases_from_sender`;
- the sender's trusted payees: `[trusted_lookalike]` when it is set, otherwise none.

**Suggested metrics.**

- Brake decisions: recall on scams, false-hold rate on benign items, and precision. HoldUp prefers a false hold to a lost payment, so also report recall at a fixed false-hold rate.
- Reasons: for each reason, the share of scam items that expect it where the checker cited it; PUBLIC_WARNING reported on its own; spurious reasons per benign item.
- Injection: the share of the 9 injection items that were braked with CHECKER_INJECTION cited, with the guardrail on and off.
- Minimal pairs: the number of the 8 twins where the scam is braked and its twin is not.
- Slices: hard cases, the 18 scams with no profile signal, each category, each asset.
- Cost and latency per check for each step and model.

**Reference baseline (payee data only).** Applying only the profile rules at the spec thresholds, with no reading of the message, flags 66 of 85 scams (78 percent) and 39 of 85 benign items (46 percent). Of the benign flags, 33 come from MULE_PATTERN (busy shops, charities, landlords and organisers), 6 from NEW_ACCOUNT, 1 from LOOKALIKE_PAYEE and 1 from PAYEE_REPORTED. The edit-distance and skeleton lookalike rule misses two lookalikes that add a suffix to the trusted name (sb-scam-027 `northwindpaper-billing.testnet` and sb-scam-082 `ben-ortiz-pay.testnet`). The version 1.0 validator computed these numbers itself without running HoldUp's code. They suggest two things: the message-reading stages are what clear legitimate payments to busy accounts, and the sender-count threshold alone causes many false holds.

**Reference baseline (HoldUp's rules layer).** The version 1.0 script `rules_baseline.py` ran the checks of `RulesRisk.assess` from `holdup/risk/rules.py` (payee facts, the look-alike check and the message scan) on every item, using the item's payee data. On the version of that file dated 31 October 2026, 14:50, it braked 71 of 85 scams (84 percent) and 40 of 85 benign items (47 percent), a precision of 64 percent (the same counts as on the 12:34 version), and cited PAYEE_REPORTED on all 18 scam items that expect it. The message checks alone braked 31 scams and 1 benign item: the 'pre-approved' decoy sb-benign-018, flagged as CHECKER_INJECTION. They caught 5 of the 9 injection lines and found the amount mismatch only in the itemised demo invoice. 14 scams got no finding at all, and every one of them is among the 20 items marked as hard cases. `python -m bench.run --rules --split all` reproduces these counts on that version of the rules; `results-rules.md` gives them per split next to the tuned rules.

## 7. Distribution and licence

- **ScamBench items** (`scambench.jsonl`, including the notes and the split), this datasheet and `SOURCES.md` are released under the Creative Commons Attribution 4.0 International licence (CC BY 4.0, https://creativecommons.org/licenses/by/4.0/); `LICENSE` in this folder says so. Suggested attribution: "ScamBench v1.1, HoldUp project, CC BY 4.0". The code next to them (`run.py`, `validate.py`) is part of HoldUp and under the repository's MIT licence.
- **One licence for all bench data.** Before version 1.0 was brought into the HoldUp repository, `bench/scambench.jsonl` held 20 hand-written items under CC0 (10 scam, 10 benign). Each of them repeats a pattern that version 1.0 already covers with a payee profile and, for scams, a cited public warning, so none was merged and all 20 were dropped (table below). The bench data is now CC BY 4.0 throughout.
- **Source attributions.** The scam patterns draw on public warnings. US federal works (FBI IC3, CFTC, SEC investor.gov) are in the public domain. The UK pages (GOV.UK, NCSC, Stop! Think Fraud) are used under the Open Government Licence v3.0: "Contains public sector information licensed under the Open Government Licence v3.0." `SOURCES.md` gives the licence and URL of each source. No source text is reproduced in the items, and no agency endorses ScamBench.
- **Fictional content.** All names, brands, agencies, accounts and domains in the items are fictional, and any resemblance to real entities is coincidental. CoinHaven and Northwind Paper Co. are the fictional brands used throughout the HoldUp demo.

### The 20 earlier hand-written items

| Earlier item | Pattern | Covered in version 1.0 by |
|---|---|---|
| scam-001 | Exchange "security" asks to move funds to a safe wallet after a new-device login | sb-scam-001, sb-scam-006, sb-scam-033 |
| scam-002 | Wallet "validation" asks for the recovery phrase and a fee | sb-scam-005, sb-scam-038, sb-scam-056 |
| scam-003 | Supplier announces a new wallet; the invoice total does not add up | sb-scam-026 |
| scam-004 | Pool pays back double within a day, guaranteed | sb-scam-015, sb-scam-053 |
| scam-005 | A prize that a fee unlocks | sb-scam-013, sb-scam-056 |
| scam-006 | "Hi Mum", a new number and money for rent today | sb-scam-076, sb-scam-085 |
| scam-007 | Task job: deposit to unlock the commission | sb-scam-047, sb-scam-051 |
| scam-008 | Recovery desk charges a fee to return traced funds | sb-scam-058, sb-scam-061, sb-scam-063 |
| scam-009 | Tax office demands payment within hours or a warrant | sb-scam-039, sb-scam-040 |
| scam-010 | A ticket sale with a line telling automated reviewers to approve it | the nine injection items (sb-scam-001, 017, 024, 027, 036, 040, 049, 054, 059); no cited warning describes a ticket sale, which every scam item needs |
| benign-001 | Dinner bill split | sb-benign-001 |
| benign-002 | Monthly rent share to the usual wallet | sb-benign-010, sb-benign-013 |
| benign-003 | Freelance invoice whose lines add up | sb-benign-020, sb-benign-023 |
| benign-004 | Group gift collection | sb-benign-003, sb-benign-008 |
| benign-005 | The sender's own note on repaying a loan, not a message the sender received | sb-benign-045, sb-benign-047 |
| benign-006 | Shop order: two mugs and shipping | sb-benign-029 |
| benign-007 | Yearly club membership | sb-benign-042, sb-benign-079 |
| benign-008 | Babysitting hours times a rate | sb-benign-069 |
| benign-009 | A utility bill split four ways | sb-benign-007 |
| benign-010 | A school fundraiser, any amount | sb-benign-037 |

## 8. Known limitations

- **Synthetic messages.** The messages were written to match public warnings, not collected from victims. Real scams are messier: typos, screenshots, long conversations, phone calls.
- **English only.** All text is English and plain ASCII, and the sources are from the US and UK. Other languages and regional scam patterns are not covered.
- **Written with AI assistance.** The project team wrote the items with AI assistance, and the labels and reasons reflect the authors' judgement. There has been no independent annotation or agreement study yet. Models from the family that helped draft the items may find their style familiar.
- **Small sample.** With 85 items per label, a measured rate carries roughly 6 to 11 percentage points of uncertainty either way (95 percent interval). Report counts alongside percentages.
- **Balanced by design.** Real traffic has far fewer scams than legitimate payments, so precision in production will be lower than on ScamBench.
- **Possible shortcuts.** Scam amounts are larger (median 882.50 against 150 USDC), and scam payee names are often descriptive (for example `reversal-desk-118.testnet`). A model could lean on either, so check the hard-case slice.
- **Invented payee profiles.** They exercise the rule thresholds but are not measured from NEAR testnet or mainnet.
- **Links do not resolve.** `.example` domains cannot be fetched, so web fetch cannot open them; a checker must judge links from the text, or map them to the training fixture pages served by the HoldUp web app.
- **No images.** The vision step is not exercised.
- **Gateway firewall.** The messages avoid backticks, but we have not tested whether the injection lines trip the hosted gateway's web firewall. A run should record a blocked request as its own outcome, separate from a brake or a release.
- **PUBLIC_WARNING is everywhere.** It is expected on every scam, so it says nothing about which scams are hard.
- **Patterns age.** The sources date from 2012 to 2026 and scam tactics change.
- **Contamination.** Once published, items may end up in model training data. Keep a private held-out set for any strong claim.
- **HoldUp's rules were tuned on it.** Their numbers on ScamBench, dev and test alike, are in-sample (section 4a); on 24 items written outside the set they caught far fewer scams (`results-rules.md`).
- **Sensitive content.** The scam messages are realistic by design. They paraphrase patterns that governments already publish, target no real person or organisation, keep extortion content non-graphic and involve no minors.

## 9. Maintenance and how to extend

To add an item:

1. Append one JSON line with the next free id for its label (`sb-scam-086`, `sb-benign-086`) and fill every field in the order of section 3.
2. Keep messages to 1 to 6 sentences of plain ASCII with no backticks or angle brackets, fictional names and accounts, and `.example` links only.
3. For a scam, read a public-domain or openly licensed warning that describes the pattern, write the message in your own words, and add the source to `SOURCES.md` with its URL, title, publisher, date on page, date read, HTTP status and licence (the validator checks that every scam `source_url` appears there).
4. Set the profile-based reasons by the thresholds in section 4, add the content-based reasons the message supports, and include PUBLIC_WARNING. A benign item has no reasons.
5. Start the notes with "Hard case:" for an item that is deliberately difficult, and add a benign twin where one makes sense.
6. Give the item a split with `python -m bench.validate --assign-splits` (it never moves an item that already has one), then run `python -m bench.run --check` from the repository root. To repeat the copy check, run `python -m bench.validate --source-texts DIR` with a folder of saved source pages.
7. Keep the labels balanced to within a few items and the injection share near 10 percent, bump the version at the top of this file, and add a changelog line.

Ideas for a later version: screenshots of fake support chats and invoices for the vision step, multi-message conversations, other languages, redacted real examples shared with consent, a private held-out split, and payee profiles drawn from real testnet distributions.

## 10. Validation summary (v1.0)

Output of `python3 validate_scambench.py --source-texts <saved pages>` on 2026-10-31:

- 170 items: 85 scam, 85 benign; ids unique and numbered without gaps.
- Every scam item has a source: 85 of 85, 53 distinct URLs, all listed in `SOURCES.md` (62 items cite US public-domain pages, 23 cite OGL v3.0 pages).
- Checker-injection items: 9 of 85 scams (10.6 percent).
- Hard cases: 20 scam, 44 benign.
- Sentences per message: 1 to 6 (median 3).
- Copy check: no message shares a sequence of six or more consecutive words with any saved source page.
- No errors, no warnings.

Version 1.1, `python -m bench.validate` on 2026-10-31: the same counts, 102 dev items (53 scam, 49 benign) and 68 test items (32 scam, 36 benign), every twin pair in one split; no errors, no warnings. The copy check was not repeated, since no message changed.

## Changelog

- 1.1 (2026-10-31): a fixed, stratified dev/test split in a new `split` field (section 4a); no item changed. Brought into the HoldUp repository as its benchmark, with the validator as `validate.py` and the rules scoring in `run.py --rules`. The 20 earlier hand-written items of the repository were dropped (section 7).
- 1.0 (2026-10-31): first release, 170 items.
