"""ScamBench: the dataset follows its rules, and the harness scores models correctly."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench import run as bench
from holdup.config import Settings
from holdup.risk.replay import replay_client


def test_the_seed_dataset_is_valid_and_balanced() -> None:
    items = bench.load_items()
    assert len(items) == 20 and sum(item["label"] == "scam" for item in items) == 10
    for item in items:
        assert item["source"]["kind"] == "hand-written" and item["source"]["licence"] == "CC0-1.0"
        assert item["payee"].endswith(".testnet")
        assert (item["label"] == "scam") == bool(item["expected_reasons"])


def test_invalid_items_are_reported(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    item = {
        "id": "x",
        "label": "maybe",
        "category": "c",
        "text": "a" + chr(0x60),
        "payee": "p.testnet",
        "amount": "ten",
        "asset": "USDC",
        "expected_reasons": ["NOT_A_REASON"],
        "source": {},
    }
    bad.write_text(json.dumps(item) + "\n" + json.dumps(item) + "\n")
    with pytest.raises(ValueError) as caught:
        bench.load_items(bad)
    message = str(caught.value)
    for problem in ("duplicate id", "scam or benign", "backticks", "not a number", "unknown reasons", "licence"):
        assert problem in message


def test_scores() -> None:
    checks = [
        bench.Check("s1", "m", "scam", True, 100, 0.001),
        bench.Check("s2", "m", "scam", False, 300, 0.003),
        bench.Check("b1", "m", "benign", True, 200, 0.002),
        bench.Check("b2", "m", "benign", False, 400, 0.004),
        bench.Check("b3", "m", "benign", None, error="timeout"),
    ]
    score = bench.score("m", checks)
    assert (score.checks, score.failed) == (5, 1)
    assert score.precision == 0.5 and score.recall == 0.5 and score.false_hold_rate == 0.5
    assert score.median_latency_ms == 250 and score.mean_cost_usd == pytest.approx(0.0025)
    empty = bench.score("m", [])
    assert empty.precision is None and empty.median_latency_ms is None


async def test_the_harness_runs_on_recorded_answers() -> None:
    items = bench.load_items()
    client = replay_client(Settings(), bench._replay_label({item["id"]: item for item in items}))
    (score,) = await bench.run_bench(client, items, ["openai/gpt-oss-20b"])
    assert score.failed == 0 and score.precision == 1.0 and score.recall == 1.0 and score.false_hold_rate == 0.0
    report = bench.render([score], replay=True, items=len(items))
    assert (
        "harness check, not a measurement" in report and "| openai/gpt-oss-20b | 20 | 0 | 100% | 100% | 0% |" in report
    )


def test_the_command_checks_the_dataset(capsys: pytest.CaptureFixture[str]) -> None:
    assert bench.main(["--check"]) == 0
    assert "20 valid items: 10 scam, 10 benign" in capsys.readouterr().out
