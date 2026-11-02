# ScamBench: the rules layer before and after tuning

HoldUp's deterministic rules (`holdup/risk/rules.py`) on ScamBench 1.1, scored offline with
`python -m bench.run --rules --split all`: each item goes through `RulesRisk.assess` with its payee
profile in place of the chain, and counts as braked when the engine's `asks_to_brake` test passes,
the test the engine applies before it puts the quick provisional brake on chain. No model is called.
A rules brake holds a payment for at least an hour, and no later check can shorten it.

"Before" is the rules at commit 2d2c1ee; "after" is this branch. The full report for all items is
at the end of this file.

## Summary

Neither split is held out for these rules (see "How the numbers were made"), so the test rows are
in-sample numbers just like the dev rows.

| Split | Rules | Scams braked | Benign braked | Precision | Recall | False holds |
|---|---|---|---|---|---|---|
| dev (53 scam, 49 benign) | before | 45 | 19 | 70% | 85% | 39% |
| | weighting only | 27 | 3 | 90% | 51% | 6% |
| | after | 36 | 3 | 92% | 68% | 6% |
| test (32 scam, 36 benign) | before | 26 | 21 | 55% | 81% | 58% |
| | weighting only | 20 | 1 | 95% | 62% | 3% |
| | after | 22 | 0 | 100% | 69% | 0% |
| all (85 scam, 85 benign) | before | 71 | 40 | 64% | 84% | 47% |
| | weighting only | 47 | 4 | 92% | 55% | 5% |
| | after | 58 | 3 | 95% | 68% | 4% |

"Weighting only" applies the new weighting (below) to the message patterns, invoice check and
look-alike check of 2d2c1ee. It separates the two parts of the change: the weighting removes 36 of
the 40 false holds and costs 24 scams; the new and changed patterns then win back 11 scams and remove
one more false hold. Those patterns were written with the dataset in view, and the benign items
trigger none of them, so the bench cannot show what they cost on honest text.

The rules now brake about one benign payment in thirty instead of one in two, and miss more
scams. That is the intended trade, but it is not free on every path. When a message or an image is
forwarded and the AI steps are on, a scam the rules let through is still read by the models. A
payment without a message reaches no model, and with `OTARI_MODE=off` no payment does; there the
rules decide alone, and a scam they pass is released after the normal wait for a new payee, with
the context findings shown to the sender but no brake and no guardian alert. The payee facts are
then all the rules have: a reported payee, a look-alike name and the money-mule profile (a young
account that many senders pay) brake; a young account alone does not.

Rough 95 percent intervals (Wilson): recall on all items 58 to 77 percent after, 74 to 90 before;
false holds 1 to 10 percent after, 37 to 58 before. On test alone recall is 51 to 82 percent and
0 false holds in 36 has an upper bound of about 10 percent; since the test items were not held
out, these intervals describe ScamBench, not new messages.

## Text the rules were not written on

A reviewer wrote 24 further items (12 scams, 12 benign) in ScamBench's format, kept outside the
repository. They are the closest thing to unseen text, with two caveats: the sample is tiny, and the
review quoted four of them (three benign phrasings and one payee name), so two of this round's fixes
(the official-office pattern and the added-word look-alike) were made with those in view.

| Rules | Scams braked | Benign braked | Precision | Recall | False holds |
|---|---|---|---|---|---|
| before | 9 of 12 | 8 of 12 | 53% | 75% | 67% |
| weighting only | 5 of 12 | 2 of 12 | 71% | 42% | 17% |
| after | 5 of 12 | 1 of 12 | 83% | 42% | 8% |

On this set the new and changed patterns brake no additional scam; they remove one false hold (an
approval phrase that 2d2c1ee treated as an injection). Recall falls much further here than on
ScamBench (75 to 42 percent against 84 to 68), which is what one would expect if the patterns fit
ScamBench's wording. The rules' recall on real messages should be taken from sets like this one,
not from ScamBench; with 12 scams the interval is wide (about 19 to 68 percent).

## How the numbers were made

1. The whole dataset was read while bringing it into the repository, before the split existed,
   and the problems that started this work (listed below) were found on items that ended up in
   test: sb-scam-024, sb-scam-027, sb-scam-082 and sb-benign-018.
2. In a first round the rules were changed with dev items in view. The report printed every
   split, so test results were visible along the way, and a defect in the invoice arithmetic was
   found on a test item (sb-benign-083: a unit price summed as if it were a line total) and fixed
   generally. Several changes of that round moved no dev result at all; their only support in the
   dataset was test items.
3. A review of that round found that many new patterns copied the wording of a single item, that
   the young-and-busy payee profile no longer braked, and that some changes had dropped forms
   2d2c1ee caught (lines addressed to an assistant or a reviewer, obligations such as "you will
   need to pay within", payment-address changes, daily percentages, escrow). The second round
   restored those forms, removed or narrowed the patterns that copied one item or also match
   honest messages, and added the honest phrasings from the review as tests. Its examples name
   further test items (sb-scam-009) and fresh items.
4. The final rules were measured once on dev, test, all items and the fresh set.

`python -m bench.run --rules` (and `make bench-rules`) now show the dev split only, and the
regression test pins only the dev counts, so a future change can be judged without seeing test.

## What changed

| Rule | Before | After |
|---|---|---|
| MULE_PATTERN (8 or more senders in 24 h) | brakes alone | context on an established account; next to NEW_ACCOUNT it is the money-mule profile and the two brake together |
| NEW_ACCOUNT (younger than 72 h) | brakes alone | weak: brakes only with a second, different weak signal or a strong one |
| URGENCY | brakes alone | weak; a time window counts when it is put on the reader ("pay within", "you will need to pay within", "or else"), not when someone else promises it ("our team will reply within"), and not when the demand word is part of an account id; new: threats of arrest, fines or legal action, and requests to keep a payment from the bank or the police |
| IMPERSONATION | brakes alone | weak, except a request to move one's funds to a "protected", escrow or holding account, which is strong; new: an official's title before a name (not a trade's: "Letting Agent"), a government office named as the sender's own, more forms of the security alert |
| CHECKER_INJECTION | any match brakes, including "pre-approved" | a line addressed to an automated reader brakes alone, in more shapes; an assistant, reviewer, moderator or model addressed by name needs an instruction to clear the payment in the same sentence; a line start counts as the start of an address; approval words alone ("pre-approved") are context, which still sends the text through the blocking guardrail |
| IMPOSSIBLE_RETURNS | four narrow patterns | also a fixed gain per day, week or month of 50 percent a year or more in a sentence about earnings (not fees, loans or prices), "doubled my savings", money back double, a send-and-get-back claim where more comes back |
| DETAILS_CHANGED | a new wallet announced in two phrasings | also temporary or different accounts, "our wallet address has changed", old details in other word orders, updates to payroll or bank details; a bare "address" or "bank" counts only in a sentence about paying |
| SECRET_REQUEST | request verbs in the base form; any negation nearby spares it | also "-ing" forms, and "use your recovery phrase" when it is to go into someone else's page or form; a negation spares a request only when it governs the verb ("never share", "we will never ask you to type"), not "it won't take long so share" |
| AMOUNT_MISMATCH | line items only on separate lines | also items in running text (weak there, since prose mentions amounts that are not items); unit prices count times their quantity, "per order" once; amounts already paid and deductions such as "minus the bursary" are allowed for; with one unit price and no quantity, the total must be a whole number of units away |
| LOOKALIKE_PAYEE | edit distance and separators | also a trusted name with a word added (`acme-supplies-billing`), weak because one owner often runs several such names; never a sub-account of the trusted payee |
| PAYEE_REPORTED, UNKNOWN_PAYEE | strong | unchanged |

Findings that do not brake stay in the verdict as context findings: they are kept out of the
reason bits and the evidence recorded on chain, the investigator step receives them as a labelled
block of facts, and the brake messages, the Why? view and the web pages list them under "Context,
not a reason to hold on its own". Once an AI step has found a reason to hold, the context counts
towards the length of its hold (three signals make 72 hours), but never as a strong signal.

## Per reason, all items

A reason counts as cited when the rules report it, as a reason or as context.

| Reason | Expected | Before: cited / correct | Before: precision, recall | After: cited / correct | After: precision, recall |
|---|---|---|---|---|---|
| NEW_ACCOUNT | 53 | 59 / 53 | 90%, 100% | 59 / 53 (14 as context) | 90%, 100% |
| MULE_PATTERN | 44 | 77 / 44 | 57%, 100% | 77 / 44 (44 as context) | 57%, 100% |
| IMPERSONATION | 43 | 8 / 7 | 88%, 16% | 17 / 16 (2 as context) | 94%, 37% |
| URGENCY | 47 | 21 / 15 | 71%, 32% | 23 / 21 (4 as context) | 91%, 45% |
| IMPOSSIBLE_RETURNS | 11 | 0 / 0 | n/a, 0% | 9 / 9 | 100%, 82% |
| CHECKER_INJECTION | 9 | 6 / 5 | 83%, 56% | 10 / 9 (1 as context) | 90%, 100% |
| DETAILS_CHANGED | 9 | 4 / 4 | 100%, 44% | 9 / 9 | 100%, 100% |
| PAYEE_REPORTED | 18 | 19 / 18 | 95%, 100% | 19 / 18 | 95%, 100% |
| LOOKALIKE_PAYEE | 6 | 5 / 4 | 80%, 67% | 7 / 6 | 86%, 100% |
| AMOUNT_MISMATCH | 6 | 1 / 1 | 100%, 17% | 1 / 1 | 100%, 17% |
| SECRET_REQUEST | 6 | 3 / 3 | 100%, 50% | 5 / 5 | 100%, 83% |

PHISHING_LINK and PUBLIC_WARNING need a fetched page or a web search; no rule checks them. Five of
the six expected amount mismatches are percentages, differences or sums written in prose with whole
numbers; the invoice check does not read those, since it needs a labelled total and line items with
two-decimal amounts. The one CHECKER_INJECTION context finding is sb-benign-018, whose
"pre-approved" is approval wording with no line addressed to a checker.

## The problems found in review

- MULE_PATTERN alone braked 33 of 85 benign items. On an established account it is now context. A
  young account that many senders pay still brakes: 33 items have that profile, 32 of them scams.
- The look-alike check missed names with an added word (sb-scam-027 and sb-scam-082, both in
  test). Both are now flagged, as a weak signal; both are braked by other reasons as well.
- The invoice check only worked with line items on separate lines. It now reads running text too,
  as a weak signal.
- The injection patterns flagged sb-benign-018 and missed 4 of the 9 injection lines. All 9 are
  now flagged; sb-benign-018 is passed on as context only.

## What still happens

- Three false holds remain. sb-benign-041 pays a two-day-old charity account that eleven people
  paid in the last day, the money-mule profile; sb-benign-059 pays a plumber whose name is one letter from a
  trusted payee; sb-benign-084 pays an allotment society with two reports left by an earlier false
  flag. All three rest on payee facts kept strong on purpose.
- 27 scams are not braked by the rules. Most are calm: a fee to unlock or recover money, a romance
  or job story, an overpayment, with no secret request, no injection, no changed details and no
  promised returns. Their young or busy payee accounts and their deadlines reach the AI steps as
  context.
- Two minimal pairs are not separated: the overpayment scam sb-scam-064 and the voice-note scam
  sb-scam-085 are not braked, and neither are their twins.
- The precision of the message patterns on honest text is unmeasured: no benign ScamBench item
  contains the phrasings they look for. The unit tests hold the honest phrasings found so far
  (deadlines, official words, prose bills, safety advice about recovery phrases); benign items
  with such wording belong in the next version of the dataset.

## Full report: the rules after tuning, all items

HoldUp's deterministic checks (`holdup/risk/rules.py`) on each item, offline: `RulesRisk.assess` with the item's payee profile in place of the chain, braked when the engine's `asks_to_brake` test passes, as for the quick provisional brake. No model is called.

| Split | Items | Scams braked | Scams missed | Benign braked | Benign passed | Precision | Recall | False holds |
|---|---|---|---|---|---|---|---|---|
| dev | 102 | 36 | 17 | 3 | 46 | 92% | 68% | 6% |
| test | 68 | 22 | 10 | 0 | 36 | 100% | 69% | 0% |
| all | 170 | 58 | 27 | 3 | 82 | 95% | 68% | 4% |

### Split: all (170 items: 85 scam, 85 benign)

|  | Braked | Not braked |
|---|---|---|
| Scam | 58 | 27 |
| Benign | 3 | 82 |

Precision 95% (58 of 61 brakes were scams), recall 68% (58 of 85 scams braked), false-hold rate 4% (3 of 85 benign items braked).

#### Reasons

Against `expected_reasons`. A reason counts as cited whether the brake rests on it or the rules only pass it on as context; benign items expect no reason, so every reason cited on one lowers precision.

| Reason | Expected | Cited as reason | Cited as context | Correct | Precision | Recall | Note |
|---|---|---|---|---|---|---|---|
| NEW_ACCOUNT | 53 | 45 | 14 | 53 | 90% | 100% | |
| MULE_PATTERN | 44 | 33 | 44 | 44 | 57% | 100% | |
| IMPERSONATION | 43 | 15 | 2 | 16 | 94% | 37% | |
| URGENCY | 47 | 19 | 4 | 21 | 91% | 45% | |
| IMPOSSIBLE_RETURNS | 11 | 9 | 0 | 9 | 100% | 82% | |
| CHECKER_INJECTION | 9 | 9 | 1 | 9 | 90% | 100% | |
| DETAILS_CHANGED | 9 | 9 | 0 | 9 | 100% | 100% | |
| PAYEE_REPORTED | 18 | 19 | 0 | 18 | 95% | 100% | |
| PHISHING_LINK | 12 | - | - | - | - | - | not checked by the rules |
| PUBLIC_WARNING | 85 | - | - | - | - | - | not checked by the rules |
| LOOKALIKE_PAYEE | 6 | 7 | 0 | 6 | 86% | 100% | |
| AMOUNT_MISMATCH | 6 | 1 | 0 | 1 | 100% | 17% | |
| SECRET_REQUEST | 6 | 5 | 0 | 5 | 100% | 83% | |

#### Categories

| Label | Category | Items | Braked | Share braked |
|---|---|---|---|---|
| scam | fake_support_safe_wallet | 7 | 6 | 86% |
| scam | giveaway_doubling | 5 | 5 | 100% |
| scam | impersonation_exchange | 6 | 5 | 83% |
| scam | impersonation_government | 8 | 7 | 88% |
| scam | investment_impossible_returns | 6 | 5 | 83% |
| scam | job_task_scam | 6 | 3 | 50% |
| scam | marketplace_overpayment | 5 | 1 | 20% |
| scam | other | 12 | 7 | 58% |
| scam | recovery_scam | 6 | 3 | 50% |
| scam | romance_emergency | 6 | 3 | 50% |
| scam | sextortion_payment | 5 | 3 | 60% |
| scam | supplier_wallet_change | 7 | 7 | 100% |
| scam | unlock_or_withdrawal_fee | 6 | 3 | 50% |
| benign | business_payroll | 7 | 0 | 0% |
| benign | donation | 7 | 1 | 14% |
| benign | family_support | 9 | 0 | 0% |
| benign | friend_split | 9 | 0 | 0% |
| benign | marketplace_purchase | 9 | 0 | 0% |
| benign | new_payee_but_legit | 10 | 0 | 0% |
| benign | other | 7 | 1 | 14% |
| benign | rent_known_landlord | 7 | 0 | 0% |
| benign | supplier_invoice_consistent | 10 | 0 | 0% |
| benign | urgent_but_legit | 10 | 1 | 10% |

Minimal pairs: in 6 of 8 twin pairs the scam is braked and its benign twin is not.

#### Misclassified

Scams not braked (27):

- sb-scam-002 (fake_support_safe_wallet): nothing
- sb-scam-008 (unlock_or_withdrawal_fee): context: URGENCY
- sb-scam-009 (unlock_or_withdrawal_fee): context: MULE_PATTERN, IMPERSONATION
- sb-scam-011 (unlock_or_withdrawal_fee): context: URGENCY
- sb-scam-019 (investment_impossible_returns): nothing
- sb-scam-020 (romance_emergency): context: MULE_PATTERN
- sb-scam-021 (romance_emergency): context: NEW_ACCOUNT
- sb-scam-023 (romance_emergency): nothing
- sb-scam-035 (impersonation_exchange): nothing
- sb-scam-045 (impersonation_government): context: IMPERSONATION
- sb-scam-048 (job_task_scam): context: MULE_PATTERN
- sb-scam-050 (job_task_scam): context: NEW_ACCOUNT
- sb-scam-051 (job_task_scam): nothing
- sb-scam-058 (recovery_scam): context: MULE_PATTERN
- sb-scam-061 (recovery_scam): nothing
- sb-scam-062 (recovery_scam): context: NEW_ACCOUNT
- sb-scam-064 (marketplace_overpayment): context: NEW_ACCOUNT
- sb-scam-065 (marketplace_overpayment): context: NEW_ACCOUNT
- sb-scam-067 (marketplace_overpayment): context: NEW_ACCOUNT
- sb-scam-068 (marketplace_overpayment): nothing
- sb-scam-071 (sextortion_payment): context: URGENCY
- sb-scam-073 (sextortion_payment): nothing
- sb-scam-075 (other): context: NEW_ACCOUNT
- sb-scam-077 (other): context: NEW_ACCOUNT
- sb-scam-080 (other): nothing
- sb-scam-081 (other): context: URGENCY
- sb-scam-085 (other): context: NEW_ACCOUNT

Benign items braked (3):

- sb-benign-041 (donation): NEW_ACCOUNT, MULE_PATTERN
- sb-benign-059 (urgent_but_legit): LOOKALIKE_PAYEE
- sb-benign-084 (other): PAYEE_REPORTED; context: MULE_PATTERN
