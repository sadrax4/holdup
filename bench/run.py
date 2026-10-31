"""ScamBench: how well HoldUp's checks tell scams from ordinary payment requests.

Two things can be measured on the same items:

- The rules layer (``--rules``), offline and free. Each item goes through
  ``RulesRisk.assess``, the call the engine makes for its quick provisional
  brake, with the item's payee profile standing in for the chain (see
  ``payee_fixture`` and ``BenchVault`` for the mapping). An item is braked when
  the engine's own test, ``asks_to_brake``, says the verdict would go on chain.
  The report gives the confusion matrix, precision, recall, the false-hold rate
  on benign items, precision and recall per reason against
  ``expected_reasons``, results per category and the misclassified ids.
- The triage prompt, on the Otari gateway. For every item and model the triage
  step runs once; a model brakes an item when it reports any signal. The report
  gives precision, recall, the false-hold rate, the median latency and the mean
  cost per check.

    python -m bench.run --check                     # validate the dataset
    python -m bench.run --rules                     # rules layer on the dev split
    python -m bench.run --rules --split all --out report.md   # every split, and write the report
    python -m bench.run --replay                    # triage harness check with recorded answers, not a measurement
    python -m bench.run                             # live triage run: needs OTARI_API_KEY, costs a few cents
    python -m bench.run --models openai/gpt-oss-20b --limit 4 --split dev

``python bench/run.py`` works too. The rules mode looks at the dev split unless ``--split test``
or ``--split all`` asks for more, so that tuning a rule does not show the test numbers by accident.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import logging
import statistics
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    # Run as a script (python bench/run.py): use this checkout's packages, not an installed copy.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.validate import DATASET, REASONS, SPLITS, composition, parse_sources, read_items, twins, validate
from holdup.config import Settings, load_settings, offline_settings
from holdup.engine import asks_to_brake
from holdup.intel import PayeeIntel
from holdup.models import NEAR, Hold, HoldStatus, PayeeStats, PaymentContext, TrustedPayee, UserView
from holdup.risk import prompts
from holdup.risk.otari import INVESTIGATOR_MODEL, REVIEWER_MODEL, TRIAGE_MODEL, VISION_MODEL, OtariClient, OtariError
from holdup.risk.pipeline import find_links, untrusted
from holdup.risk.prompts.contracts import TriageResult
from holdup.risk.replay import replay_client
from holdup.risk.rules import RulesRisk

VERSION = "1.1"
DEFAULT_MODELS = (TRIAGE_MODEL, INVESTIGATOR_MODEL, REVIEWER_MODEL, VISION_MODEL)
SIGNALS = ("urgency", "impersonation", "secret_request", "details_changed", "injection_suspected", "impossible_returns")
AI_ONLY_REASONS = ("PHISHING_LINK", "PUBLIC_WARNING")  # need a fetched page or a web search: no rule checks them
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
CHECK_MS = 1_793_448_000_000  # 2026-10-31 12:00 UTC: the moment every rules check is made
SENDER = "bench-sender.testnet"


def load_items(path: Path = DATASET, *, split: str = "all") -> list[dict[str, Any]]:
    """The validated dataset, or one split of it; raises ``ValueError`` listing every problem."""
    items, problems = read_items(path)
    problems += validate(items).errors
    if problems:
        raise ValueError("invalid dataset:\n" + "\n".join(problems))
    return [item for item in items if split == "all" or item["split"] == split]


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def _pct(value: float | None) -> str:
    return f"{value:.0%}" if value is not None else "n/a"


# ----------------------------------------------------------------------
# the rules layer
# ----------------------------------------------------------------------


def payee_fixture(item: dict[str, Any]) -> dict[str, Any]:
    """The payee as ``PayeeIntel`` reads it offline: what the transaction index would report."""
    profile = item["payee_profile"]
    return {"age_hours": profile["age_hours"], "distinct_inbound_senders_24h": profile["distinct_inbound_senders_24h"]}


class BenchVault:
    """The vault reads the rules make, answered from one item.

    - payee stats: ``cancelled_after_flag`` is ``reports_30d``; when it is above zero the latest
      report was a day before the check (every report it counts is from the last 30 days);
    - the sender's trusted payees: ``trusted_lookalike`` when it is set, otherwise none;
    - every payee account exists: the engine refuses a payment to a missing account before any check.

    ``prior_releases_from_sender`` is not mapped: the rules do not read the sender's history
    (the investigator does, through the near-intel ``sender_history`` tool).
    """

    vault_id = "vault.bench.testnet"

    def __init__(self, item: dict[str, Any], now_ms: int = CHECK_MS) -> None:
        self.item = item
        self.now = now_ms

    async def now_ms(self) -> int:
        return self.now

    async def account_exists(self, account_id: str) -> bool:
        return True

    async def get_payee_stats(self, account_id: str) -> PayeeStats:
        reports = int(self.item["payee_profile"].get("reports_30d", 0))
        return PayeeStats(cancelled_after_flag=reports, last_report_ms=self.now - DAY_MS if reports else None)

    async def get_user(self, account_id: str) -> UserView:
        trusted = [TrustedPayee(p, self.now - 30 * DAY_MS) for p in [self.item["trusted_lookalike"]] if p]
        return UserView(account_id, self.now - 365 * DAY_MS, None, None, False, None, trusted, 0)


def bench_hold(item: dict[str, Any], settings: Settings, now_ms: int = CHECK_MS) -> Hold:
    """The hold a payment of the item creates. The HUSD demo token stands in for USDC, as in the demo."""
    asset = NEAR if item["asset"] == "NEAR" else settings.husd_token or "usdc.bench.testnet"
    decimals = 24 if asset == NEAR else 6
    raw = int(Decimal(item["amount"]) * 10**decimals)
    return Hold(0, SENDER, item["payee"], asset, raw, now_ms, now_ms + 10 * 60_000, HoldStatus.HELD)


@dataclass(frozen=True)
class RulesResult:
    item_id: str
    label: str
    category: str
    split: str
    expected: frozenset[str]
    braked: bool
    reasons: frozenset[str]  # findings the brake rests on
    context: frozenset[str]  # findings passed on as context, not a reason to hold on their own
    hold_for_ms: int

    @property
    def cited(self) -> frozenset[str]:
        return self.reasons | self.context


async def rules_check(item: dict[str, Any], settings: Settings) -> RulesResult:
    """One item through ``RulesRisk.assess`` and the engine's brake test."""
    vault = BenchVault(item)
    table = {"payees": {item["payee"]: payee_fixture(item)}}
    intel = PayeeIntel(settings, vault, fixtures=table)  # type: ignore[arg-type]
    rules = RulesRisk(intel, vault)  # type: ignore[arg-type]
    verdict = await rules.assess(bench_hold(item, settings), PaymentContext(forwarded_text=item["message"]))
    braked = asks_to_brake(verdict)

    def names(context: bool) -> frozenset[str]:
        return frozenset(str(f.reason.name) for f in verdict.findings if f.context is context)

    return RulesResult(
        item_id=item["id"],
        label=item["label"],
        category=item["category"],
        split=item["split"],
        expected=frozenset(item["expected_reasons"]),
        braked=braked,
        reasons=names(False),
        context=names(True),
        hold_for_ms=verdict.hold_for_ms if braked else 0,
    )


async def run_rules(items: Sequence[dict[str, Any]]) -> list[RulesResult]:
    settings = offline_settings()
    return [await rules_check(item, settings) for item in items]


@dataclass(frozen=True)
class RulesScore:
    items: int
    tp: int
    fn: int
    fp: int
    tn: int

    @property
    def precision(self) -> float | None:
        return _ratio(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> float | None:
        return _ratio(self.tp, self.tp + self.fn)

    @property
    def false_hold_rate(self) -> float | None:
        return _ratio(self.fp, self.fp + self.tn)


def score_rules(results: Iterable[RulesResult]) -> RulesScore:
    results = list(results)
    scam = [r for r in results if r.label == "scam"]
    benign = [r for r in results if r.label == "benign"]
    return RulesScore(
        items=len(results),
        tp=sum(r.braked for r in scam),
        fn=sum(not r.braked for r in scam),
        fp=sum(r.braked for r in benign),
        tn=sum(not r.braked for r in benign),
    )


def _in_split(results: Iterable[RulesResult], split: str) -> list[RulesResult]:
    return [r for r in results if split == "all" or r.split == split]


def _reason_rows(results: Sequence[RulesResult]) -> list[str]:
    rows = []
    for reason in REASONS:
        expected = {r.item_id for r in results if reason in r.expected}
        as_reason = {r.item_id for r in results if reason in r.reasons}
        as_context = {r.item_id for r in results if reason in r.context}
        cited = as_reason | as_context
        correct = len(expected & cited)
        if reason in AI_ONLY_REASONS:
            rows.append(f"| {reason} | {len(expected)} | - | - | - | - | - | not checked by the rules |")
            continue
        rows.append(
            f"| {reason} | {len(expected)} | {len(as_reason)} | {len(as_context)} | {correct} | "
            f"{_pct(_ratio(correct, len(cited)))} | {_pct(_ratio(correct, len(expected)))} | |"
        )
    return rows


def _category_rows(results: Sequence[RulesResult]) -> list[str]:
    groups: dict[tuple[str, str], list[RulesResult]] = collections.defaultdict(list)
    for r in results:
        groups[(r.label, r.category)].append(r)
    rows = []
    for (label, category), group in sorted(groups.items(), key=lambda kv: (kv[0][0] != "scam", kv[0][1])):
        braked = sum(r.braked for r in group)
        rows.append(f"| {label} | {category} | {len(group)} | {braked} | {_pct(braked / len(group))} |")
    return rows


def _found(r: RulesResult) -> str:
    parts = []
    if r.reasons:
        parts.append(", ".join(sorted(r.reasons, key=REASONS.index)))
    if r.context:
        parts.append("context: " + ", ".join(sorted(r.context, key=REASONS.index)))
    return "; ".join(parts) or "nothing"


def render_rules(results: Sequence[RulesResult], *, split: str, items: Sequence[dict[str, Any]]) -> str:
    """The markdown report on ``split``: a summary row (one per split for ``all``), then the details.

    Only the requested split is shown, so a run on dev never shows the test numbers.
    """
    chosen = _in_split(results, split)
    score = score_rules(chosen)
    scams = sum(r.label == "scam" for r in chosen)
    lines = [
        f"# ScamBench {VERSION}: the rules layer",
        "",
        "HoldUp's deterministic checks (`holdup/risk/rules.py`) on each item, offline: `RulesRisk.assess` with the "
        "item's payee profile in place of the chain, braked when the engine's `asks_to_brake` test passes, as for "
        "the quick provisional brake. No model is called.",
        "",
        "| Split | Items | Scams braked | Scams missed | Benign braked | Benign passed | Precision | Recall "
        "| False holds |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name in (*SPLITS, "all") if split == "all" else (split,):
        s = score_rules(_in_split(results, name))
        lines.append(
            f"| {name} | {s.items} | {s.tp} | {s.fn} | {s.fp} | {s.tn} | {_pct(s.precision)} | {_pct(s.recall)} "
            f"| {_pct(s.false_hold_rate)} |"
        )
    lines += [
        "",
        f"## Split: {split} ({len(chosen)} items: {scams} scam, {len(chosen) - scams} benign)",
        "",
        "|  | Braked | Not braked |",
        "|---|---|---|",
        f"| Scam | {score.tp} | {score.fn} |",
        f"| Benign | {score.fp} | {score.tn} |",
        "",
        f"Precision {_pct(score.precision)} ({score.tp} of {score.tp + score.fp} brakes were scams), "
        f"recall {_pct(score.recall)} ({score.tp} of {score.tp + score.fn} scams braked), "
        f"false-hold rate {_pct(score.false_hold_rate)} ({score.fp} of {score.fp + score.tn} benign items braked).",
        "",
        "### Reasons",
        "",
        "Against `expected_reasons`. A reason counts as cited whether the brake rests on it or the rules only "
        "pass it on as context; benign items expect no reason, so every reason cited on one lowers precision.",
        "",
        "| Reason | Expected | Cited as reason | Cited as context | Correct | Precision | Recall | Note |",
        "|---|---|---|---|---|---|---|---|",
        *_reason_rows(chosen),
        "",
        "### Categories",
        "",
        "| Label | Category | Items | Braked | Share braked |",
        "|---|---|---|---|---|",
        *_category_rows(chosen),
    ]
    by_id = {r.item_id: r for r in chosen}
    pairs = sorted((a, b) for a, b in twins(items).items() if a in by_id and b in by_id and a.startswith("sb-scam"))
    if pairs:
        held_apart = sum(by_id[a].braked and not by_id[b].braked for a, b in pairs)
        lines += ["", f"Minimal pairs: in {held_apart} of {len(pairs)} twin pairs the scam is braked and its benign "
                      "twin is not."]
    missed = [r for r in chosen if r.label == "scam" and not r.braked]
    false_holds = [r for r in chosen if r.label == "benign" and r.braked]
    lines += ["", "### Misclassified", "", f"Scams not braked ({len(missed)}):", ""]
    lines += [f"- {r.item_id} ({r.category}): {_found(r)}" for r in missed] or ["- none"]
    lines += ["", f"Benign items braked ({len(false_holds)}):", ""]
    lines += [f"- {r.item_id} ({r.category}): {_found(r)}" for r in false_holds] or ["- none"]
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------
# the triage prompt on Otari
# ----------------------------------------------------------------------


@dataclass
class Check:
    item_id: str
    model: str
    label: str
    braked: bool | None  # None when the call failed
    latency_ms: int | None = None
    cost_usd: float | None = None
    error: str | None = None


@dataclass
class ModelScore:
    model: str
    checks: int
    failed: int
    precision: float | None
    recall: float | None
    false_hold_rate: float | None
    median_latency_ms: float | None
    mean_cost_usd: float | None


def score(model: str, checks: list[Check]) -> ModelScore:
    done = [c for c in checks if c.braked is not None]
    tp = sum(c.braked and c.label == "scam" for c in done)
    fp = sum(c.braked and c.label == "benign" for c in done)
    fn = sum(not c.braked and c.label == "scam" for c in done)
    tn = sum(not c.braked and c.label == "benign" for c in done)
    latencies = [c.latency_ms for c in done if c.latency_ms is not None]
    costs = [c.cost_usd for c in done if c.cost_usd is not None]
    return ModelScore(
        model=model,
        checks=len(checks),
        failed=len(checks) - len(done),
        precision=_ratio(tp, tp + fp),
        recall=_ratio(tp, tp + fn),
        false_hold_rate=_ratio(fp, fp + tn),
        median_latency_ms=statistics.median(latencies) if latencies else None,
        mean_cost_usd=statistics.fmean(costs) if costs else None,
    )


def triage_facts(item: dict[str, Any]) -> str:
    """The PAYMENT FACTS block the pipeline gives the triage step, for this item."""
    trusted = item["trusted_lookalike"] or "none"
    links = ", ".join(find_links(item["message"])) or "none"
    return (
        "PAYMENT FACTS (looked up by HoldUp)\n"
        f"- payee account: {item['payee']}\n- amount: {item['amount']} {item['asset']}\n"
        f"- payees this sender already trusts: {trusted}\n- links in the message: {links}"
    )


async def check_item(client: OtariClient, model: str, item: dict[str, Any]) -> Check:
    prompt = prompts.load("triage")
    messages = [
        {"role": "system", "content": prompt.text},
        {"role": "user", "content": f"{triage_facts(item)}\n\n{untrusted('FORWARDED MESSAGE', item['message'])}"},
    ]
    started = time.monotonic()
    try:
        result, record = await client.chat(
            "triage",
            model,
            messages,
            output=TriageResult,
            metadata={"app": "holdup", "step": "triage", "bench": item["id"]},
            max_tokens=2_000,
            prompt_sha256=prompt.sha256,
        )
    except OtariError as exc:
        return Check(
            item["id"], model, item["label"], None, round((time.monotonic() - started) * 1000), error=exc.label
        )
    braked = any(getattr(result, name) for name in SIGNALS)
    return Check(item["id"], model, item["label"], braked, record.latency_ms, record.cost_usd)


async def run_bench(client: OtariClient, items: list[dict[str, Any]], models: list[str]) -> list[ModelScore]:
    scores = []
    for model in models:
        checks = [await check_item(client, model, item) for item in items]
        scores.append(score(model, checks))
    return scores


def render(scores: list[ModelScore], *, replay: bool, items: int, split: str = "all") -> str:
    title = f"ScamBench {VERSION}" + (" (replay: harness check, not a measurement)" if replay else "")
    lines = [
        f"# {title}",
        "",
        f"{items} items (split: {split}), triage prompt {prompts.load('triage').version}, "
        f"run {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}.",
        "",
        "| Model | Checks | Failed | Precision | Recall | False holds | Median latency (ms) | Cost per check (USD) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in scores:
        latency = f"{s.median_latency_ms:.0f}" if s.median_latency_ms is not None else "n/a"
        cost = f"{s.mean_cost_usd:.6f}" if s.mean_cost_usd is not None else "n/a"
        lines.append(
            f"| {s.model} | {s.checks} | {s.failed} | {_pct(s.precision)} | {_pct(s.recall)} | "
            f"{_pct(s.false_hold_rate)} | {latency} | {cost} |"
        )
    return "\n".join(lines) + "\n"


def _replay_label(items: dict[str, dict[str, Any]]) -> Any:
    """Recorded answers by label: the hero answer for scams, the benign answer otherwise."""

    def resolve(step: str, body: dict[str, Any]) -> str:
        item = items.get((body.get("metadata") or {}).get("bench", ""))
        return "hero" if item and item["label"] == "scam" else "benign"

    return resolve


# ----------------------------------------------------------------------


def check_dataset(path: Path = DATASET) -> int:
    """``--check``: every validation rule, then the composition."""
    items, problems = read_items(path)
    sources = parse_sources()
    report = validate(items, sources=sources)
    for line in composition(items, sources):
        print(line)
    for warning in report.warnings:
        print("WARNING:", warning)
    for error in [*problems, *report.errors]:
        print("ERROR:", error)
    if problems or not report.ok:
        return 1
    scams = sum(item["label"] == "scam" for item in items)
    print(f"{len(items)} valid items: {scams} scam, {len(items) - scams} benign")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run ScamBench on the rules layer or the triage prompt.")
    parser.add_argument("--check", action="store_true", help="validate the dataset and exit")
    parser.add_argument("--rules", action="store_true", help="score the rules layer offline (no model is called)")
    parser.add_argument(
        "--split", choices=(*SPLITS, "all"), help="which items to use (default: dev for --rules, all otherwise)"
    )
    parser.add_argument("--models", help=f"comma-separated catalog ids (default: {', '.join(DEFAULT_MODELS)})")
    parser.add_argument("--limit", type=int, help="only the first N items")
    parser.add_argument("--replay", action="store_true", help="answer from recorded fixtures (no key, no cost)")
    parser.add_argument("--out", help="also write the markdown report here")
    args = parser.parse_args(argv)

    if args.check:
        return check_dataset()
    split = args.split or ("dev" if args.rules else "all")
    if args.rules:
        chosen = load_items(split=split)
        results = asyncio.run(run_rules(chosen))
        report = render_rules(results, split=split, items=chosen)
    else:
        items = load_items(split=split)
        items = items[: args.limit] if args.limit else items
        models = [m.strip() for m in args.models.split(",")] if args.models else list(DEFAULT_MODELS)
        if args.replay:
            # The recorded answers were made with the full pipeline; the bench sends triage alone.
            logging.getLogger("holdup.risk.replay").setLevel(logging.ERROR)
            client = replay_client(Settings(), _replay_label({item["id"]: item for item in items}))
        else:
            settings = load_settings()
            if not settings.otari_api_key:
                print("OTARI_API_KEY is not set; use --replay for a harness check.", file=sys.stderr)
                return 2
            client = OtariClient(settings)
        scores = asyncio.run(run_bench(client, items, models))
        report = render(scores, replay=args.replay, items=len(items), split=split)
    print(report)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
