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

WEAK_REASONS = frozenset({Reason.NEW_ACCOUNT, Reason.URGENCY, Reason.IMPERSONATION})
"""Signals the rules brake for only together with another one. Each alone is common in honest
payments: a payee who opened an account this week, a real deadline, a message from a business.
A rule can still mark one of its own patterns as strong (a request to move funds to a "protected"
wallet is impersonation that brakes alone) or as weak (a trusted payee's name with a word added)."""

CONTEXT_REASONS = frozenset({Reason.MULE_PATTERN})
"""Facts the rules pass on to the AI checks and the Why? view but do not brake for on their own:
shops, charities and landlords are paid by many different people every day. A busy payee whose
account is also new is the money-mule profile; there it weighs as a weak signal (see ``weigh``)."""

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
    Reason.LOOKALIKE_PAYEE: "look-alike payee name",
    Reason.AMOUNT_MISMATCH: "numbers do not add up",
    Reason.SECRET_REQUEST: "asks for a secret",
    Reason.AI_UNAVAILABLE: "AI check unavailable",
    Reason.BUDGET_EXHAUSTED: "AI budget used up",
    Reason.SECOND_OPINION: "second model agrees",
    Reason.UNKNOWN_PAYEE: "payee account does not exist",
}


def rules_hold_ms(findings: Iterable[Finding]) -> int:
    """The wait the rules ask for: none without a reason, 24 h for a strong reason, 1 h otherwise.

    Context findings are not reasons and ask for no wait.
    """
    reasons = {finding.reason for finding in findings if not finding.context}
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
