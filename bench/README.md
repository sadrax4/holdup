# ScamBench

A small labelled set of payment messages, used to compare models on HoldUp's
triage prompt: does the model raise a signal on scams, and stay quiet on
ordinary payments between people?

```
python bench/run.py --check     # validate the dataset
python bench/run.py --replay    # check the harness with recorded answers (no key, no cost)
python bench/run.py             # live run; needs OTARI_API_KEY and costs a few cents
python bench/run.py --models openai/gpt-oss-20b --limit 4 --out bench/results.md
```

For each model the report gives precision and recall on scams, the false-hold
rate on ordinary payments (the share of them a model would have braked), the
median latency and the mean cost per check, as reported by Otari in
`usage.cost_usd`. A replay run only proves that the harness works: its answers
are recorded per label, so its numbers mean nothing.

## Format

`scambench.jsonl` holds one JSON object per line:

| Field | Meaning |
|---|---|
| `id` | Unique, `scam-NNN` or `benign-NNN` |
| `label` | `scam` or `benign` |
| `category` | Free label for the kind of message, such as `invoice_redirection` |
| `text` | The message as a user would forward it |
| `payee`, `amount`, `asset` | The payment the message asks for (amount as a decimal string) |
| `expected_reasons` | HoldUp reason names a careful reviewer would give; empty for benign items |
| `source` | `{kind, url, licence}`: where the item comes from and under which licence |

## Sourcing rules

1. Every brand, person, account and link is fictional. Payees are made-up
   `.testnet` names. No real phone numbers, wallet addresses or live links.
2. Items written for HoldUp are `kind: hand-written`, `url: null`,
   `licence: CC0-1.0`. The first twenty items are of this kind.
3. Items based on a published scam warning may only come from sources in the
   public domain (for example United States federal agencies such as the FTC
   or the FBI) or under the UK Open Government Licence v3.0 (for example
   gov.uk, Action Fraud or the NCSC). Record the page as `url` and its licence
   as `licence` (`public-domain` or `OGL-3.0`), and use `kind: paraphrased`.
4. Always paraphrase: describe the pattern in new words and new fictional
   names. Never copy a published message or a real victim's text.
5. Keep the set balanced between scams and benign payments, and give benign
   items the hard cases: real deadlines, real invoices, new contacts.
6. No backtick characters in `text`: the hosted gateway firewall rejects them
   (otari issue #2704). `python bench/run.py --check` enforces the rules it can.
