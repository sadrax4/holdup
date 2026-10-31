"""How strong each reason bit is, how long it holds a payment, and how to say it briefly."""

from __future__ import annotations

from collections.abc import Iterable

from ..models import Finding, Reason, reasons_from_bits

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS

PROVISIONAL_HOLD_MS = HOUR_MS
"""Wait added for any rules finding: time for the full check to finish and the sender to look."""

STRONG_HOLD_MS = DAY_MS
"""Wait the rules add on their own authority: a payee with reports, or a request for a secret."""

REPORT_WINDOW_MS = 30 * DAY_MS
"""Reports older than this no longer brake new payments by themselves."""

STRONG_REASONS = frozenset({Reason.PAYEE_REPORTED, Reason.SECRET_REQUEST})
"""Reasons that justify a day-long brake without any AI step agreeing."""

FAIL_CLOSED_REASONS = frozenset({Reason.AI_UNAVAILABLE, Reason.BUDGET_EXHAUSTED})
"""Reasons that describe a check that could not run rather than the payment itself."""

SHORT_LABELS: dict[Reason, str] = {
    Reason.NEW_ACCOUNT: "new payee account",
    Reason.MULE_PATTERN: "many unrelated senders",
    Reason.IMPERSONATION: "impersonation",
    Reason.URGENCY: "pressure to pay fast",
    Reason.IMPOSSIBLE_RETURNS: "impossible returns",
    Reason.CHECKER_INJECTION: "text aimed at the AI checker",
    Reason.DETAILS_CHANGED: "payment details changed by message",
    Reason.PAYEE_REPORTED: "reports from other senders",
    Reason.PHISHING_LINK: "phishing link",
    Reason.PUBLIC_WARNING: "public scam warnings",
    Reason.LOOKALIKE_PAYEE: "look-alike of a trusted payee",
    Reason.AMOUNT_MISMATCH: "numbers do not add up",
    Reason.SECRET_REQUEST: "asks for a secret",
    Reason.AI_UNAVAILABLE: "AI check unavailable",
    Reason.BUDGET_EXHAUSTED: "AI budget used up",
    Reason.SECOND_OPINION: "second model agrees",
    Reason.UNKNOWN_PAYEE: "payee account does not exist",
}


def rules_hold_ms(findings: Iterable[Finding]) -> int:
    """The wait the rules ask for: none when clean, 24 h for a strong reason, 1 h otherwise."""
    reasons = {finding.reason for finding in findings}
    if not reasons:
        return 0
    return STRONG_HOLD_MS if reasons & STRONG_REASONS else PROVISIONAL_HOLD_MS


def note_for(bits: int, *, limit: int = 120) -> str:
    """A one-line note naming the reasons, e.g. ``Held: new payee account, impersonation.``

    Reasons are listed in bit order; when they do not fit in ``limit`` characters
    the rest are counted instead (``and 2 more``).
    """
    reasons = reasons_from_bits(bits)
    if not reasons:
        return "No warning signs found."
    labels = [SHORT_LABELS[reason] for reason in reasons]
    for shown in range(len(labels), 0, -1):
        hidden = len(labels) - shown
        tail = f", and {hidden} more" if hidden else ""
        text = f"Held: {', '.join(labels[:shown])}{tail}."
        if len(text) <= limit:
            return text
    return f"Held for {len(labels)} reasons."
