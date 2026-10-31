"""ScamBench: how well each model, given HoldUp's triage prompt, tells scams from ordinary payments.

For every item and every model the triage step runs once; a model "brakes" an
item when it reports any signal. The report gives, per model, precision and
recall on scams, the false-hold rate on ordinary payments, the median latency
and the mean cost per check.

    python bench/run.py                       # live: needs OTARI_API_KEY, costs a few cents
    python bench/run.py --models openai/gpt-oss-20b --limit 4
    python bench/run.py --replay              # harness check with recorded answers, not a measurement
    python bench/run.py --check               # validate the dataset only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from holdup.config import Settings, load_settings
from holdup.models import Reason
from holdup.risk import prompts
from holdup.risk.otari import INVESTIGATOR_MODEL, REVIEWER_MODEL, TRIAGE_MODEL, VISION_MODEL, OtariClient, OtariError
from holdup.risk.pipeline import untrusted
from holdup.risk.prompts.contracts import TriageResult
from holdup.risk.replay import replay_client

BENCH = Path(__file__).resolve().parent
DATASET = BENCH / "scambench.jsonl"
DEFAULT_MODELS = (TRIAGE_MODEL, INVESTIGATOR_MODEL, REVIEWER_MODEL, VISION_MODEL)
REQUIRED = ("id", "label", "category", "text", "payee", "amount", "asset", "expected_reasons", "source")
SIGNALS = ("urgency", "impersonation", "secret_request", "details_changed", "injection_suspected", "impossible_returns")


def load_items(path: Path = DATASET) -> list[dict[str, Any]]:
    """The dataset, validated: unique ids, both labels, known reasons, a source with a licence."""
    items = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    problems = []
    seen: set[str] = set()
    for n, item in enumerate(items, 1):
        missing = [key for key in REQUIRED if key not in item]
        if missing:
            problems.append(f"line {n}: missing {', '.join(missing)}")
            continue
        if item["id"] in seen:
            problems.append(f"line {n}: duplicate id {item['id']}")
        seen.add(item["id"])
        if item["label"] not in ("scam", "benign"):
            problems.append(f"{item['id']}: label must be scam or benign")
        if "\u0060" in item["text"]:
            problems.append(f"{item['id']}: backticks are rejected by the hosted gateway firewall")
        try:
            Decimal(item["amount"])
        except InvalidOperation:
            problems.append(f"{item['id']}: amount {item['amount']!r} is not a number")
        unknown = [r for r in item["expected_reasons"] if r not in Reason.__members__]
        if unknown:
            problems.append(f"{item['id']}: unknown reasons {unknown}")
        if not item["source"].get("licence"):
            problems.append(f"{item['id']}: every item needs a licence")
    if problems:
        raise ValueError("invalid dataset:\n" + "\n".join(problems))
    return items


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


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


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


async def check_item(client: OtariClient, model: str, item: dict[str, Any]) -> Check:
    prompt = prompts.load("triage")
    facts = f"PAYMENT FACTS (looked up by HoldUp)\n- payee account: {item['payee']}\n- amount: {item['amount']} {item['asset']}"
    messages = [
        {"role": "system", "content": prompt.text},
        {"role": "user", "content": f"{facts}\n\n{untrusted('FORWARDED MESSAGE', item['text'])}"},
    ]
    started = time.monotonic()
    try:
        result, record = await client.chat(
            "triage", model, messages,
            output=TriageResult,
            metadata={"app": "holdup", "step": "triage", "bench": item["id"]},
            max_tokens=2_000,
            prompt_sha256=prompt.sha256,
        )
    except OtariError as exc:
        return Check(item["id"], model, item["label"], None, round((time.monotonic() - started) * 1000), error=exc.label)
    braked = any(getattr(result, name) for name in SIGNALS)
    return Check(item["id"], model, item["label"], braked, record.latency_ms, record.cost_usd)


async def run_bench(client: OtariClient, items: list[dict[str, Any]], models: list[str]) -> list[ModelScore]:
    scores = []
    for model in models:
        checks = [await check_item(client, model, item) for item in items]
        scores.append(score(model, checks))
    return scores


def render(scores: list[ModelScore], *, replay: bool, items: int) -> str:
    def pct(value: float | None) -> str:
        return f"{value:.0%}" if value is not None else "n/a"

    title = "ScamBench (replay: harness check, not a measurement)" if replay else "ScamBench"
    lines = [
        f"# {title}",
        "",
        f"{items} items, triage prompt {prompts.load('triage').version}, run {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}.",
        "",
        "| Model | Checks | Failed | Precision | Recall | False holds | Median latency (ms) | Cost per check (USD) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in scores:
        latency = f"{s.median_latency_ms:.0f}" if s.median_latency_ms is not None else "n/a"
        cost = f"{s.mean_cost_usd:.6f}" if s.mean_cost_usd is not None else "n/a"
        lines.append(
            f"| {s.model} | {s.checks} | {s.failed} | {pct(s.precision)} | {pct(s.recall)} | "
            f"{pct(s.false_hold_rate)} | {latency} | {cost} |"
        )
    return "\n".join(lines) + "\n"


def _replay_label(items: dict[str, dict[str, Any]]) -> Any:
    """Recorded answers by label: the hero answer for scams, the benign answer otherwise."""

    def resolve(step: str, body: dict[str, Any]) -> str:
        item = items.get((body.get("metadata") or {}).get("bench", ""))
        return "hero" if item and item["label"] == "scam" else "benign"

    return resolve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run ScamBench against the triage prompt.")
    parser.add_argument("--models", help=f"comma-separated catalog ids (default: {', '.join(DEFAULT_MODELS)})")
    parser.add_argument("--limit", type=int, help="only the first N items")
    parser.add_argument("--replay", action="store_true", help="answer from recorded fixtures (no key, no cost)")
    parser.add_argument("--check", action="store_true", help="validate the dataset and exit")
    parser.add_argument("--out", help="also write the markdown report here")
    args = parser.parse_args(argv)

    items = load_items()[: args.limit] if args.limit else load_items()
    if args.check:
        scams = sum(item["label"] == "scam" for item in items)
        print(f"{len(items)} valid items: {scams} scam, {len(items) - scams} benign")
        return 0
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
    report = render(asyncio.run(run_bench(client, items, models)), replay=args.replay, items=len(items))
    print(report)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
