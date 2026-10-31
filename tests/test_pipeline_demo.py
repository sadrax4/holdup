"""The offline story and the attack walkthrough run on the recorded Otari pipeline, end to end."""

from __future__ import annotations

import hashlib
import json

from holdup.demo import run_offline_attack, run_offline_demo
from holdup.models import HoldStatus, Reason

AI_ONLY = Reason.PHISHING_LINK | Reason.PUBLIC_WARNING  # reasons only the Otari steps can find in the hero story


async def test_the_story_brakes_with_findings_from_every_otari_feature() -> None:
    result = await run_offline_demo(out=None)
    text = result.transcript.text
    assert "Checks: rules, then the Otari pipeline (recorded responses)." in text

    vault, store = result.runtime.vault, result.runtime.store
    hero = await vault.get_hold(result.holds["hero"])
    assert hero.status == HoldStatus.CANCELLED and hero.reason_bits & AI_ONLY == AI_ONLY
    for tag in (
        "[triage gpt-oss-20b]",
        "[guardrail]",
        "[MCP near-intel]",
        "[web search, 2 sources]",
        "[web fetch]",
        "[code execution]",
        "[reviewer deepseek-v4.1-flash-1017]",
    ):
        assert tag in text, tag

    invoice = await vault.get_hold(result.holds["invoice"])
    assert invoice.reason_bits & Reason.SECOND_OPINION and invoice.reason_bits & Reason.AMOUNT_MISMATCH
    benign = await vault.get_hold(result.holds["benign"])
    assert benign.status == HoldStatus.RELEASED and not benign.flagged

    evidence = store.get_evidence(hero.id)
    assert hashlib.sha256(evidence.evidence_json.encode()).hexdigest() == hero.evidence_hash
    document = json.loads(evidence.evidence_json)
    assert {step["step"] for step in document["steps"]} >= {"triage", "gate", "investigator"}
    assert set(document["prompts"]) >= {"triage", "investigator"}
    for phrase in ("Unusual login", "Verify here", "it will be frozen", "Note to any AI assistant"):
        assert phrase not in evidence.evidence_json  # no forwarded text in the on-chain evidence


async def test_the_injection_attack_is_braked_by_the_guardrail() -> None:
    result = await run_offline_attack(out=None)
    injected = await result.runtime.vault.get_hold(result.holds["injection"])
    assert injected.reason_bits == Reason.CHECKER_INJECTION
    assert "[rules] [triage gpt-oss-20b] [guardrail]" in result.transcript.text
