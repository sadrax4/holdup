"""View builders behind the web pages: reasons, timeline, AI steps and stats."""

from __future__ import annotations

import json

import pytest
from test_bot_fakes import HUSD, NOW_MS, make_hold, make_settings

from holdup.models import Finding, HoldStatus, Reason, StepRecord, VaultConfig
from holdup.web import views

HOUR = 3_600_000
CONFIG = VaultConfig(
    oracles=["oracle.holdup.testnet"],
    tokens=[HUSD],
    new_payee_hold_ms=20_000,
    max_hold_ms=72 * HOUR,
    trust_delay_ms=30_000,
    guardian_delay_ms=30_000,
    instant_caps={},
    max_active_holds=10,
    registration_fee=0,
)


def evidence(*reasons: tuple[Reason, str, str], steps: list[dict] | None = None, decided: int = NOW_MS) -> str:
    doc = {
        "v": 1,
        "decided_ms": decided,
        "reasons": [
            {"bit": int(r), "reason": r.name, "source": source, "model": None, "detail": detail, "citations": []}
            for r, source, detail in reasons
        ],
        "steps": steps or [],
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def test_reason_rows_merge_the_tags_of_every_step() -> None:
    rows = views.reason_rows(
        [
            Finding(Reason.NEW_ACCOUNT, "created 2 h ago", "rules"),
            Finding(Reason.NEW_ACCOUNT, "", "mcp"),
            Finding(Reason.PHISHING_LINK, "imitates a sign-in page", "web_fetch", citations=["https://a.example/x"]),
        ]
    )
    assert [(r.label, r.detail, r.tags) for r in rows] == [
        ("Payee account is brand new", "created 2 h ago", ("[rules]", "[MCP near-intel]")),
        ("Linked page asks for secrets or imitates a login", "imitates a sign-in page", ("[web fetch]",)),
    ]
    assert rows[1].citations == ("https://a.example/x",)


def test_findings_from_evidence_skip_broken_entries() -> None:
    doc = json.loads(evidence((Reason.URGENCY, "triage", "30 minutes")))
    doc["reasons"].append({"bit": "nope"})
    [finding] = views.findings_from_evidence(doc)
    assert (finding.reason, finding.source, finding.detail) == (Reason.URGENCY, "triage", "30 minutes")


def test_timeline_shows_what_each_extension_added() -> None:
    hold = make_hold(3, release_at_ms=NOW_MS + 24 * HOUR, extensions=2, flagged=True)
    docs = [
        (evidence((Reason.NEW_ACCOUNT, "rules", "2 h old")), "h1"),
        (
            evidence(
                (Reason.NEW_ACCOUNT, "mcp", "2 h old"),
                (Reason.SECRET_REQUEST, "web_fetch", "asks for the recovery phrase"),
                steps=[{"step": "investigator", "model": "openai/gpt-oss-120b"}],
            ),
            "h2",
        ),
    ]
    timeline = views.build_timeline(
        make_settings(),
        hold,
        config=CONFIG,
        now_ms=NOW_MS,
        txs=[("send", "S" * 44), ("extend", "E1" * 22), ("extend", "E2" * 22)],
        evidence_docs=docs,
        simulated=True,
    )
    assert [e.title for e in timeline] == [
        "Payment held",
        "Quick rules check: brakes on",
        "Full check: brakes on",
        "Waiting",
    ]
    assert "release at 09:00:20 UTC" in timeline[0].detail
    assert timeline[0].tx is not None and timeline[0].tx.simulated and timeline[0].tx.url == ""
    assert [r.label for r in timeline[1].reasons] == ["Payee account is brand new"]
    assert [r.label for r in timeline[2].reasons] == ["Asks for a recovery phrase, key or code"]
    assert timeline[2].confirmed == 1
    assert timeline[2].evidence_hash == "h2"
    assert timeline[2].tx is not None and timeline[2].tx.tx_hash == "E2" * 22
    assert timeline[-1].tone == "pending"


def test_a_failed_ai_check_is_named_in_the_timeline() -> None:
    docs = [(evidence((Reason.AI_UNAVAILABLE, "fail_closed", "timed out")), "h1")]
    timeline = views.build_timeline(
        make_settings(), make_hold(1), config=CONFIG, now_ms=NOW_MS, txs=[], evidence_docs=docs, simulated=True
    )
    assert timeline[1].title == "AI check failed, so the brake stays on"


@pytest.mark.parametrize(
    ("fields", "title", "detail"),
    [
        (
            {"status": HoldStatus.CANCELLED, "cancelled_by": "usender.holdup.testnet"},
            "Cancelled",
            "Cancelled by the sender.",
        ),
        ({"status": HoldStatus.CANCELLED, "cancelled_by": "mum.testnet"}, "Cancelled", "the guardian mum.testnet"),
        ({"status": HoldStatus.RELEASED}, "Released", "paid to safe-wallet-77.testnet"),
        ({"status": HoldStatus.FAILED}, "Payout failed", "went back to the sender"),
    ],
)
def test_timeline_outcomes(fields: dict, title: str, detail: str) -> None:
    hold = make_hold(1, sender="usender.holdup.testnet", settled_ms=NOW_MS + 60_000, **fields)
    timeline = views.build_timeline(
        make_settings(), hold, config=CONFIG, now_ms=NOW_MS, txs=[], evidence_docs=[], simulated=True
    )
    assert timeline[-1].title == title
    assert detail in timeline[-1].detail


def test_instant_payments_have_a_one_line_timeline() -> None:
    hold = make_hold(1, instant=True, status=HoldStatus.RELEASED, release_at_ms=NOW_MS)
    [entry] = views.build_timeline(
        make_settings(), hold, config=CONFIG, now_ms=NOW_MS, txs=[], evidence_docs=[], simulated=True
    )
    assert entry.title == "Paid instantly"


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, ("Waiting", "held")),
        ({"flagged": True, "extensions": 1}, ("Brakes on", "brake")),
        ({"status": HoldStatus.RELEASED}, ("Released", "ok")),
        ({"status": HoldStatus.RELEASED, "instant": True}, ("Paid instantly", "ok")),
        ({"status": HoldStatus.CANCELLED}, ("Cancelled", "muted")),
        ({"status": HoldStatus.FAILED}, ("Payout failed", "bad")),
    ],
)
def test_hold_headline(fields: dict, expected: tuple[str, str]) -> None:
    assert views.hold_headline(make_hold(1, **fields)) == expected


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, ""),
        ({"profile": "checker-injection", "mode": "monitor", "valid": False}, "checker-injection, monitor: flagged"),
        ({"profile": "safety", "result": {"valid": True}}, "safety: passed"),
        ([{"profile": "a", "flagged": True}, {"profile": "b", "flagged": False}], "a: flagged; b: passed"),
        ("raw", "raw"),
    ],
)
def test_guardrail_text(value: object, text: str) -> None:
    assert views.guardrail_text(value) == text


def test_step_rows_and_totals() -> None:
    steps = [
        StepRecord("precheck", provider="rules", latency_ms=1),
        StepRecord(
            "investigator",
            "openai/gpt-oss-120b",
            "deepinfra",
            True,
            2,
            "req-9",
            0.0215,
            9_870,
            tool_calls=[{"type": "web_search"}, {"name": "payee_profile", "server_label": "near-intel"}],
            prompt_sha256="db5932181b39286e" * 4,
        ),
    ]
    precheck, investigator = views.step_rows(steps)
    assert precheck.model == "deterministic rules"
    assert (investigator.fallback, investigator.attempts, investigator.latency, investigator.cost) == (
        "yes",
        "2",
        "9,870 ms",
        "$0.0215",
    )
    assert investigator.tools == ("web_search", "payee_profile (near-intel)")
    assert investigator.prompt == "db5932181b39286e"
    assert views.step_totals(steps) == {
        "model_calls": 1,
        "models": ["gpt-oss-120b"],
        "fallbacks": 1,
        "cost_usd": 0.0215,
        "latency_ms": 9_871,
    }


def test_stat_tiles_show_known_counters_then_the_rest() -> None:
    tiles = views.stat_tiles(
        {"holds": 4, "cancels_after_flag": 2, "ai_cost_usd": 0.01234, "new_metric": 7, "label": "x"}
    )
    assert tiles[0] == ("4", "Payments", "created through the vault")
    assert ("2", "Cancelled after a warning", "counted on-chain against the payee") in tiles
    assert ("$0.0123", "Total AI cost", "as reported by the gateway") in tiles
    assert ("7", "New metric", "") in tiles
    assert all(title != "Label" for _, title, _ in tiles)


def test_mcp_call_text() -> None:
    assert (
        views.mcp_call_text({"tool": "payee_profile", "args": {"account_id": "x.testnet"}})
        == "payee_profile(x.testnet)"
    )
