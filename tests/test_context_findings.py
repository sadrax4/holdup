"""Context findings: what the rules noted but did not brake for is passed on and shown, labelled as context,
and never counts as a reason to hold."""

from __future__ import annotations

import json

from holdup import messages
from holdup.bot.texts import why_text
from holdup.config import Settings
from holdup.engine import finding_from_dict, verdict_to_dict
from holdup.models import NEAR, REASON_LABELS, Finding, Hold, HoldStatus, Reason, Verdict, WhyView
from holdup.risk.evidence import build_evidence
from holdup.risk.reasons import DAY_MS, rules_hold_ms
from holdup.web import views
from holdup.workers.oracle import fallback_evidence

NOW = 1_793_000_000_000
SETTINGS = Settings(network="offline")
BUSY = Finding(Reason.MULE_PATTERN, "40 different senders paid it in the last 24 h", "rules", context=True)
YOUNG = Finding(Reason.NEW_ACCOUNT, "created about 5 h ago", "rules", context=True)
REPORTED = Finding(Reason.PAYEE_REPORTED, "2 earlier senders cancelled after a warning", "rules")


def hold(status: HoldStatus = HoldStatus.HELD) -> Hold:
    return Hold(3, "u1.holdup.testnet", "shop.testnet", NEAR, 2 * 10**24, NOW - 1_000, NOW + 600_000, status)


def test_context_is_no_reason_bit_and_asks_for_no_wait() -> None:
    assert Verdict(1, True, DAY_MS, [REPORTED, BUSY]).reason_bits == int(Reason.PAYEE_REPORTED)
    assert Verdict(1, False, 0, [BUSY, YOUNG]).reason_bits == 0
    assert rules_hold_ms([BUSY, YOUNG]) == 0 and rules_hold_ms([REPORTED, BUSY]) == DAY_MS


def test_messages_list_context_under_its_own_heading() -> None:
    lines = messages.reason_lines([BUSY, REPORTED])
    assert lines[0].startswith(f"- {REASON_LABELS[Reason.PAYEE_REPORTED]}: 2 earlier senders")
    assert lines[1] == messages.CONTEXT_HEADING
    assert lines[2] == f"- {REASON_LABELS[Reason.MULE_PATTERN]}: 40 different senders paid it in the last 24 h [rules]"


def test_a_reason_another_step_found_is_a_reason_and_keeps_every_tag() -> None:
    mcp = Finding(Reason.MULE_PATTERN, "many senders", "mcp")
    label = REASON_LABELS[Reason.MULE_PATTERN]
    assert messages.reason_lines([BUSY, mcp]) == [
        f"- {label}: 40 different senders paid it in the last 24 h [rules] [MCP near-intel]"
    ]


def test_checks_clear_names_the_context() -> None:
    plain = messages.checks_clear(SETTINGS, hold(), now_ms=NOW)
    assert plain.startswith("Checks finished: nothing unusual found. 2 NEAR to shop.testnet releases at")
    noted = messages.checks_clear(SETTINGS, hold(), now_ms=NOW, context=[YOUNG])
    assert noted.startswith("Checks finished: no reason to hold it longer.")
    assert noted.splitlines()[1:] == [
        messages.CONTEXT_HEADING, "- Payee account is brand new: created about 5 h ago [rules]"
    ]


def test_the_why_view_labels_context() -> None:
    quiet = why_text(SETTINGS, WhyView(hold(), None, None, findings=[YOUNG]), NOW)
    assert "No warning signs so far." in quiet and "What the checks found" not in quiet
    assert quiet.index(messages.CONTEXT_HEADING) < quiet.index("Payee account is brand new")
    braked = why_text(SETTINGS, WhyView(hold(), None, None, findings=[BUSY, REPORTED]), NOW)
    assert braked.index("What the checks found:") < braked.index("Other senders cancelled payments")
    assert braked.index("Other senders cancelled payments") < braked.index(messages.CONTEXT_HEADING)
    assert braked.index(messages.CONTEXT_HEADING) < braked.index("many unrelated senders")


def test_web_rows_keep_reasons_and_context_apart() -> None:
    assert [r.label for r in views.reason_rows([BUSY, REPORTED])] == [REASON_LABELS[Reason.PAYEE_REPORTED]]
    assert [r.label for r in views.context_rows([BUSY, REPORTED])] == [REASON_LABELS[Reason.MULE_PATTERN]]
    assert views.context_rows([REPORTED]) == []


def test_the_evidence_records_only_the_reasons_for_the_extension() -> None:
    verdict = Verdict(3, True, DAY_MS, [REPORTED, BUSY])
    kwargs = {"network": "offline", "vault_id": "vault.holdup.testnet", "prompt_versions": {}}
    document = json.loads(build_evidence(hold(), verdict, decided_ms=NOW, **kwargs))
    assert [r["reason"] for r in document["reasons"]] == ["PAYEE_REPORTED"]
    fallback = json.loads(fallback_evidence(hold(), verdict, **kwargs))
    assert [r["reason"] for r in fallback["reasons"]] == ["PAYEE_REPORTED"]


def test_stored_verdicts_keep_the_context_flag() -> None:
    record = verdict_to_dict(Verdict(3, True, DAY_MS, [REPORTED, BUSY]), "final")
    assert record["reason_bits"] == int(Reason.PAYEE_REPORTED)
    assert [finding_from_dict(item) for item in record["findings"]] == [REPORTED, BUSY]
    older = {"reason": "URGENCY", "bit": int(Reason.URGENCY), "detail": "x", "source": "rules"}
    assert finding_from_dict(older).context is False  # records written before context findings existed
