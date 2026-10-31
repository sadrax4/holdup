"""ScamBench: the dataset follows its rules, and the harness scores the rules layer and the models correctly."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from bench import run as bench
from bench import validate
from holdup.config import Settings
from holdup.models import Reason
from holdup.risk.replay import replay_client

PROCESS_REASONS = {"AI_UNAVAILABLE", "BUDGET_EXHAUSTED", "SECOND_OPINION", "UNKNOWN_PAYEE"}


@pytest.fixture(scope="module")
def items() -> list[dict]:
    return bench.load_items()


@pytest.fixture(scope="module")
def results(items: list[dict]) -> list[bench.RulesResult]:
    return asyncio.run(bench.run_rules(items))


def item(**changes: object) -> dict:
    base = {
        "id": "sb-benign-001", "label": "benign", "category": "friend_split",
        "message": "Dinner was 20.00, pay me back.", "payee": "dana.testnet", "asset": "USDC", "amount": "10",
        "trusted_lookalike": None, "expected_reasons": [],
        "payee_profile": {"age_hours": 9000, "distinct_inbound_senders_24h": 2, "prior_releases_from_sender": 1},
        "source_url": None, "source_licence": None, "notes": "A dinner split.", "split": "dev",
    }
    return {**base, **changes}


# ----------------------------------------------------------------------
# the dataset
# ----------------------------------------------------------------------


def test_the_dataset_is_valid_balanced_and_split(items: list[dict]) -> None:
    assert len(items) == 170 and sum(i["label"] == "scam" for i in items) == 85
    assert [i for i in items if i["split"] == "dev"] == bench.load_items(split="dev")
    assert len(bench.load_items(split="dev")) == 102 and len(bench.load_items(split="test")) == 68
    report = validate.validate(items, sources=validate.parse_sources())
    assert report.ok, report.errors
    for row in items:
        assert row["payee"].endswith(".testnet") and (row["label"] == "scam") == bool(row["expected_reasons"])
        assert (row["label"] == "scam") == (row["source_licence"] is not None)


def test_the_stored_split_is_the_documented_procedure(items: list[dict]) -> None:
    fresh = [{k: v for k, v in i.items() if k != "split"} for i in items]
    assert validate.assign_splits(fresh) == {i["id"]: i["split"] for i in items}
    pairs = validate.twins(items)
    assert len(pairs) == 16  # eight minimal pairs, mapped both ways
    by_id = {i["id"]: i for i in items}
    assert all(by_id[a]["split"] == by_id[b]["split"] for a, b in pairs.items())


def test_assigning_splits_keeps_existing_ones_and_follows_twins() -> None:
    rows = [item(id=f"sb-benign-{n:03d}", split=None) for n in range(1, 11)]
    rows[0]["split"] = "test"
    split = validate.assign_splits(rows)
    assert split["sb-benign-001"] == "test" and list(split.values()).count("dev") == 6
    scam = item(id="sb-scam-001", label="scam", category="other", split="test", notes="Twin: sb-benign-005.")
    rows[4]["notes"] = "Twin: sb-scam-001."
    assert validate.assign_splits([scam, *rows])["sb-benign-005"] == "test"


def test_reason_names_follow_the_payment_reasons_in_bit_order() -> None:
    assert validate.REASONS == [r.name for r in Reason if r.name not in PROCESS_REASONS]


def test_invalid_items_are_reported() -> None:
    bad = item(
        id="x", label="maybe", message="a" + chr(0x60), amount="ten", expected_reasons=["NOT_A_REASON"], split="train"
    )
    scam = item(id="sb-scam-001", label="scam", category="other", expected_reasons=["URGENCY"], split="later")
    report = validate.validate([bad, bad, scam, item(id="sb-benign-002", extra=1)])
    message = "\n".join(report.errors)
    for problem in ("label must be scam or benign", "needs an http(s) source_url", "must list PUBLIC_WARNING",
                    "split must be dev or test", "unexpected fields ['extra']"):
        assert problem in message, problem
    two = validate.validate([item(id="sb-benign-001"), item(id="sb-benign-001", message="a" + chr(0x60))])
    assert any("duplicate id" in e for e in two.errors) and any("backtick" in e for e in two.errors)


def test_load_items_refuses_an_invalid_file(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps(item(amount="ten")) + "\nnot json\n")
    with pytest.raises(ValueError) as caught:
        bench.load_items(path)
    assert "invalid JSON" in str(caught.value) and "positive decimal string" in str(caught.value)


def test_the_command_checks_the_dataset(capsys: pytest.CaptureFixture[str]) -> None:
    assert bench.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "170 valid items: 85 scam, 85 benign" in out and "split: dev 102 (scam 53, benign 49)" in out


# ----------------------------------------------------------------------
# the rules layer
# ----------------------------------------------------------------------


async def test_the_payee_profile_reaches_the_rules_as_the_engine_reads_it() -> None:
    settings = Settings(network="offline")
    profile = {"age_hours": 5, "distinct_inbound_senders_24h": 30, "prior_releases_from_sender": 0, "reports_30d": 2}
    reported = await bench.rules_check(item(payee_profile=profile, message="Hi"), settings)
    assert reported.braked and reported.reasons == {"NEW_ACCOUNT", "MULE_PATTERN", "PAYEE_REPORTED"}
    assert not reported.context and reported.hold_for_ms == 24 * bench.HOUR_MS
    busy = {"age_hours": 9000, "distinct_inbound_senders_24h": 30, "prior_releases_from_sender": 0}
    shop = await bench.rules_check(item(payee_profile=busy), settings)
    assert not shop.braked and shop.context == {"MULE_PATTERN"}
    near_copy = item(payee="leecarter.testnet", trusted_lookalike="lee-carter.testnet")
    lookalike = await bench.rules_check(near_copy, settings)
    assert lookalike.braked and lookalike.reasons == {"LOOKALIKE_PAYEE"}
    added = await bench.rules_check(item(payee="lee-carter-pay.testnet", trusted_lookalike="lee-carter.testnet"),
                                    settings)
    assert not added.braked and added.context == {"LOOKALIKE_PAYEE"}  # a word added: weak on its own
    quiet = await bench.rules_check(item(), settings)
    assert not quiet.braked and not quiet.cited and quiet.hold_for_ms == 0


def test_rules_scores() -> None:
    def result(label: str, braked: bool) -> bench.RulesResult:
        return bench.RulesResult("i", label, "c", "dev", frozenset(), braked, frozenset(), frozenset(), 0)

    score = bench.score_rules([result("scam", True), result("scam", False), result("benign", True),
                               result("benign", False), result("benign", False)])
    assert (score.tp, score.fn, score.fp, score.tn) == (1, 1, 1, 2)
    assert score.precision == 0.5 and score.recall == 0.5 and score.false_hold_rate == pytest.approx(1 / 3)
    assert bench.score_rules([]).precision is None


def test_the_rules_on_the_dev_split_stay_where_results_rules_md_says(results: list[bench.RulesResult]) -> None:
    """A regression guard on dev only, so that a rule change never needs a look at the test split.

    When a change moves these, rerun ``make bench-rules`` and update bench/results-rules.md.
    """
    dev = bench.score_rules(r for r in results if r.split == "dev")
    assert (dev.tp, dev.fn, dev.fp, dev.tn) == (36, 17, 3, 46)


def test_the_rules_report(results: list[bench.RulesResult], items: list[dict]) -> None:
    dev_items = [i for i in items if i["split"] == "dev"]
    report = bench.render_rules([r for r in results if r.split == "dev"], split="dev", items=dev_items)
    for heading in ("| Split | Items |", "| dev | 102 |", "## Split: dev", "| Scam |", "| Benign |", "### Reasons",
                    "| CHECKER_INJECTION |", "not checked by the rules", "### Categories", "Minimal pairs:",
                    "### Misclassified", "Scams not braked", "Benign items braked"):
        assert heading in report, heading
    assert "sb-benign-059 (urgent_but_legit): LOOKALIKE_PAYEE" in report
    assert "| test |" not in report and "| all |" not in report  # a dev run shows no test numbers
    every = bench.render_rules(results, split="all", items=items)
    assert "| dev | 102 |" in every and "| test | 68 |" in every and "| all | 170 |" in every


def test_the_rules_command_writes_the_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "rules.md"
    assert bench.main(["--rules", "--split", "test", "--out", str(out)]) == 0
    assert out.read_text().startswith("# ScamBench 1.1: the rules layer")
    assert "## Split: test" in capsys.readouterr().out


def test_the_rules_command_looks_at_dev_unless_asked(capsys: pytest.CaptureFixture[str]) -> None:
    assert bench.main(["--rules"]) == 0
    out = capsys.readouterr().out
    assert "## Split: dev" in out and "| test |" not in out and "sb-scam-0" in out


def test_the_script_runs_from_its_own_checkout() -> None:
    """``python bench/run.py`` imports this checkout's packages even when another copy is installed."""
    import subprocess
    import sys

    root = Path(bench.__file__).resolve().parents[1]
    code = "import runpy, sys; sys.argv = ['run.py', '--check']; runpy.run_path('bench/run.py', run_name='__main__')"
    done = subprocess.run([sys.executable, "-I", "-c", code], cwd=root, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert "170 valid items" in done.stdout


# ----------------------------------------------------------------------
# the triage prompt
# ----------------------------------------------------------------------


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


def test_the_triage_facts_match_the_pipeline() -> None:
    facts = bench.triage_facts(item(message="See https://shop.example/x now", trusted_lookalike="dana.testnet"))
    assert facts.startswith("PAYMENT FACTS (looked up by HoldUp)\n- payee account: dana.testnet\n- amount: 10 USDC")
    assert "- payees this sender already trusts: dana.testnet" in facts
    assert "- links in the message: https://shop.example/x" in facts


async def test_the_harness_runs_on_recorded_answers(items: list[dict]) -> None:
    client = replay_client(Settings(), bench._replay_label({i["id"]: i for i in items}))
    (score,) = await bench.run_bench(client, items, ["openai/gpt-oss-20b"])
    assert score.failed == 0 and score.precision == 1.0 and score.recall == 1.0 and score.false_hold_rate == 0.0
    report = bench.render([score], replay=True, items=len(items))
    assert "harness check, not a measurement" in report
    assert "| openai/gpt-oss-20b | 170 | 0 | 100% | 100% | 0% |" in report


def test_the_replay_command_takes_a_split(capsys: pytest.CaptureFixture[str]) -> None:
    assert bench.main(["--replay", "--split", "test", "--limit", "5", "--models", "openai/gpt-oss-20b"]) == 0
    assert "5 items (split: test)" in capsys.readouterr().out
