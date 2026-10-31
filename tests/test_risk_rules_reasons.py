"""Reason policy: how long the rules hold a payment and the one-line notes."""

from __future__ import annotations

from holdup.models import Finding, Reason
from holdup.risk.reasons import (
    DAY_MS,
    FAIL_CLOSED_REASONS,
    HOUR_MS,
    SHORT_LABELS,
    STRONG_REASONS,
    note_for,
    rules_hold_ms,
)


def finding(reason: Reason) -> Finding:
    return Finding(reason, "", "rules")


def test_rules_hold_one_hour_for_any_finding_and_a_day_for_strong_ones() -> None:
    assert rules_hold_ms([]) == 0
    assert rules_hold_ms([finding(Reason.NEW_ACCOUNT), finding(Reason.URGENCY)]) == HOUR_MS
    assert rules_hold_ms([finding(Reason.NEW_ACCOUNT), finding(Reason.SECRET_REQUEST)]) == DAY_MS
    assert rules_hold_ms([finding(Reason.PAYEE_REPORTED)]) == DAY_MS
    assert {Reason.PAYEE_REPORTED, Reason.SECRET_REQUEST} == STRONG_REASONS
    assert not STRONG_REASONS & FAIL_CLOSED_REASONS


def test_every_reason_has_a_short_label() -> None:
    assert set(SHORT_LABELS) == set(Reason)
    assert all(label and label == label.strip() and "`" not in label for label in SHORT_LABELS.values())


def test_note_for_lists_reasons_in_bit_order() -> None:
    assert note_for(0) == "No warning signs found."
    bits = int(Reason.CHECKER_INJECTION | Reason.NEW_ACCOUNT)
    assert note_for(bits) == "Held: new payee account, text aimed at the AI checker."
    assert note_for(int(Reason.AI_UNAVAILABLE)) == "Held: AI check unavailable."


def test_note_for_counts_what_does_not_fit() -> None:
    every = sum(int(r) for r in Reason)
    note = note_for(every, limit=80)
    assert len(note) <= 80 and note.startswith("Held: new payee account") and note.endswith("more.")
    assert note_for(every, limit=10) == "Held for 17 reasons."
    assert len(note_for(every)) <= 120
