"""Deterministic payment checks: no model involved, done in milliseconds.

They give the engine its instant provisional brake and keep the brake working
when the AI step is off or failing. The AI step reads messages far better;
these rules only catch the obvious. Finding details are templated: they name
what was found and never quote the forwarded message.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from ..interfaces import VaultPort
from ..models import Finding, Hold, PaymentContext, Reason, StepRecord, Verdict
from .reasons import REPORT_WINDOW_MS, note_for, rules_hold_ms

if TYPE_CHECKING:
    from ..intel import PayeeIntel

log = logging.getLogger(__name__)

SOURCE = "rules"
NEW_ACCOUNT_HOURS = 72
MULE_MIN_SENDERS = 8
REPORTS_TO_BRAKE = 2
DETAIL_MAX = 160


# ----------------------------------------------------------------------
# message heuristics
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class TextSignal:
    """One reason found in a message. The detail lists what matched in our own words, never quoting the text."""

    reason: Reason
    detail: str


@dataclass(frozen=True)
class _Pattern:
    reason: Reason
    label: str
    regex: re.Pattern[str]
    negatable: bool = False  # skip matches preceded by "never", "don't" ... in the same clause


def _rule(reason: Reason, label: str, pattern: str, *, negatable: bool = False, cased: bool = False) -> _Pattern:
    return _Pattern(reason, label, re.compile(pattern, 0 if cased else re.IGNORECASE), negatable)


_ASK = (
    r"(?:send|share|give|enter|type|paste|provide|confirm|verify|validate|sync|import|submit|reply with"
    r"|tell|read out|ask(?:s|ing)? for|need|requires?|input|write down|dm)"
)
_SECRET_PHRASE = (
    r"(?:seed|recovery|secret|backup|mnemonic|wallet)[- ](?:phrase|words?)|mnemonic"
    r"|(?:12|24|twelve|twenty[- ]four)[- ]word(?:s| phrase)?"
)
_PRIVATE_KEY = r"private[- ]keys?|secret[- ]keys?|keystore(?: file)?"
_CODE = r"(?:one[- ]time|verification|login|security|2fa|sms|auth(?:entication)?)[- ](?:code|pin|passcode)|otp"
_FUNDS = r"(?:funds?|money|usdc|usdt|crypto|assets|balance|near|tokens|savings|coins)"
_OBJECT = r"(?:it|this|that|the (?:payment|transfer|transaction|message|request))"
_PAYMENT_TARGET = (
    r"(?:(?:usdc |usdt |crypto |payment |receiving |near )?(?:wallet|account|address)|bank(?: account)?"
    r"|(?:payment|bank|banking|wallet|account) details)"
)
_FREEZE = r"(?:frozen|suspended|locked|closed|blocked|terminated|deleted|seized|confiscated|lost)"

_PATTERNS: tuple[_Pattern, ...] = (
    # Requests for secrets. A request verb must come first, and "never share your
    # recovery phrase" style warnings are skipped.
    _rule(Reason.SECRET_REQUEST, "asks for a recovery phrase",
          rf"\b{_ASK}\b[^.!?\n]{{0,48}}?\b(?:{_SECRET_PHRASE})\b", negatable=True),
    _rule(Reason.SECRET_REQUEST, "asks for a private key",
          rf"\b{_ASK}\b[^.!?\n]{{0,48}}?\b(?:{_PRIVATE_KEY})\b", negatable=True),
    _rule(Reason.SECRET_REQUEST, "asks for a one-time code",
          rf"\b{_ASK}(?: (?:me|us))?(?: (?:the|your|that))? (?:\d-digit )?(?:{_CODE})\b", negatable=True),
    _rule(Reason.SECRET_REQUEST, "asks for a one-time code",
          r"\b(?:send|give|tell|forward|read)(?: (?:me|us)) (?:the|your|that) (?:\d-digit )?code\b",
          negatable=True),
    # Pressure.
    _rule(Reason.URGENCY, "a short deadline",
          r"\bwithin (?:the next )?(?:\d{1,3}|an?|one|two|three|few) ?(?:minutes?|mins?|hours?|hrs?)\b"),
    _rule(Reason.URGENCY, "a short deadline",
          r"\bbefore (?:midnight|tonight|(?:the )?end of (?:the )?day|it(?:'s| is) too late)\b"),
    _rule(Reason.URGENCY, "a short deadline", r"\b(?:expires?|expiring) (?:today|tonight|soon|in \d)"),
    _rule(Reason.URGENCY, "a threat to freeze or lose funds",
          rf"\bor (?:it|they|your (?:account|wallet|{_FUNDS})) (?:will|would|could|may) (?:be )?"
          rf"(?:permanently )?{_FREEZE}\b"),
    _rule(Reason.URGENCY, "a threat to freeze or lose funds",
          rf"\b(?:account|wallet|{_FUNDS})\b[^.!?]{{0,40}}\b(?:will|would|is going to|are going to|could|may) "
          rf"(?:be )?(?:permanently )?{_FREEZE}\b"),
    _rule(Reason.URGENCY, "a threat to freeze or lose funds",
          r"\b(?:account|wallet|funds?|assets?|balance)\b (?:has|have|is|are|was|were) (?:been )?(?:temporarily )?"
          r"(?:frozen|suspended|locked|compromised|restricted|blocked)\b"),
    _rule(Reason.URGENCY, "urgent wording",
          r"\b(?:urgent(?:ly)?|immediately|asap|act now|time[- ]sensitive|(?:last|final) (?:chance|notice|warning))\b"),
    # Text written for the AI checker rather than for the person.
    _rule(Reason.CHECKER_INJECTION, "addresses AI assistants",
          r"\b(?:note|message|instructions?|attention|memo|notice|reminder) (?:to|for) (?:any |all |the )?"
          r"(?:ai|a\.i\.|assistants?|bots?|models?|llms?|language models?|checkers?|reviewers?|scanners?"
          r"|filters?|moderators?|fraud (?:checks?|systems?|filters?))\b"),
    _rule(Reason.CHECKER_INJECTION, "addresses AI assistants",
          r"\b(?:dear|hey|hi|hello) (?:ai|assistant|bot|model|checker)\b"
          r"|\b(?:if you are|as) an? (?:ai|language model|llm)\b"),
    _rule(Reason.CHECKER_INJECTION, "asks to ignore instructions",
          r"\b(?:ignore|disregard|forget|override|bypass) (?:(?:all|any|the|your|these|those|of) )*"
          r"(?:(?:previous|prior|above|earlier|preceding|system|safety|security) )?"
          r"(?:instructions|rules|prompts?|guidelines|warnings?|checks?|polic(?:y|ies))\b"),
    _rule(Reason.CHECKER_INJECTION, "says the payment is pre-approved",
          r"\bpre-? ?(?:approved|authori[sz]ed|verified|cleared|screened)\b"),
    _rule(Reason.CHECKER_INJECTION, "says the payment is pre-approved",
          r"\b(?:this|the) (?:payment|transfer|transaction|request) (?:is|has been|was) (?:already )?"
          r"(?:whitelisted|marked safe|verified safe)\b"),
    _rule(Reason.CHECKER_INJECTION, "asks to mark it safe",
          rf"\b(?:mark|classify|flag|label|treat|rate|consider|report|tag|score) {_OBJECT} (?:as )?"
          r"(?:safe|legit(?:imate)?|approved|trusted|verified|clean|low[- ]risk|benign|harmless|genuine"
          r"|not (?:a )?scam)\b"),
    _rule(Reason.CHECKER_INJECTION, "asks not to hold the payment",
          r"\b(?:do not|don't|never) (?:flag|block|hold|delay) (?:this|the|my) (?:payment|transfer|transaction)\b"),
    _rule(Reason.CHECKER_INJECTION, "prompt manipulation words", r"\b(?:system prompt|jailbreak|developer mode)\b"),
    # Changed payment details.
    _rule(Reason.DETAILS_CHANGED, "announces a new wallet or account",
          rf"\b(?:we|i|they|our company)(?:'ve| have| has)? (?:moved|changed|switched|updated|migrated) "
          rf"(?:to )?(?:a |our |my |the )?(?:new )?{_PAYMENT_TARGET}\b"),
    _rule(Reason.DETAILS_CHANGED, "announces a new wallet or account",
          rf"\b(?:our|my|the) (?:new|updated|changed) {_PAYMENT_TARGET}\b"),
    _rule(Reason.DETAILS_CHANGED, "says not to use the old details",
          r"\b(?:do not|don't|no longer|stop) (?:use|using|pay|paying|send|sending)(?: to)? (?:the|our|my) "
          r"(?:old|previous|usual|former)\b"),
    _rule(Reason.DETAILS_CHANGED, "says not to use the old details",
          r"\b(?:old|previous|former) (?:wallet|account|address) (?:is|was) "
          r"(?:no longer|closed|deactivated|not monitored)"),
    _rule(Reason.DETAILS_CHANGED, "asks to update records",
          r"\bupdate (?:your|our|the) "
          r"(?:records|payment details|bank details|beneficiary|payee details|wallet address)\b"),
    # Investment bait.
    _rule(Reason.IMPOSSIBLE_RETURNS, "guaranteed profit",
          r"\b(?:guaranteed|risk[- ]free|no[- ]risk) (?:returns?|profits?|income|gains?|yield)\b"),
    _rule(Reason.IMPOSSIBLE_RETURNS, "multiplied money",
          r"\b(?:double|triple|quadruple|2x|3x|5x|10x|100x|x2|x3|x5|x10) (?:your )?"
          r"(?:money|funds|crypto|investment|deposit|usdc|usdt|near|coins?|tokens?)\b"),
    _rule(Reason.IMPOSSIBLE_RETURNS, "a fixed daily or weekly percentage",
          r"\b\d{1,4}(?:\.\d+)? ?% (?:daily|weekly|per day|per week|a day|a week|every day|each day)\b"),
    _rule(Reason.IMPOSSIBLE_RETURNS, "a send-and-get-back offer",
          r"\b(?:send|deposit|transfer) \$?\d[\d,.]* (?:\w+ )?(?:and|to) (?:get|receive) \$?\d[\d,.]* (?:\w+ )?back\b"),
    # Fake support and account-security lures.
    _rule(Reason.IMPERSONATION, "claims to be a support or security team",
          r"\b(?:support|security|fraud|compliance|verification|risk|trust (?:and|&) safety|account protection) "
          r"(?:team|department|desk|cent(?:er|re)|agent|officer|specialist|unit|division)\b"),
    _rule(Reason.IMPERSONATION, "signed as a support desk",
          r"\b[A-Z][\w&'.-]*(?: [A-Z][\w&'.-]*){0,2} (?:Support|Security|Help ?[Dd]esk|Customer (?:Care|Service))\b",
          cased=True),
    _rule(Reason.IMPERSONATION, "asks to move funds to a protected wallet",
          rf"\b(?:move|transfer|send|withdraw) (?:all )?(?:of )?(?:your )?{_FUNDS} (?:to|into) (?:a|the|this|our|your) "
          r"(?:new )?(?:safe|secure|secured|protected|backup|holding|escrow|recovery|quarantine) "
          r"(?:wallet|account|address|vault)\b"),
    _rule(Reason.IMPERSONATION, "an account-security alert",
          r"\b(?:unusual|suspicious|unauthori[sz]ed|unrecognized) "
          r"(?:login|log-in|sign[- ]in|activity|access|device)\b"),
)

_NEGATION = re.compile(r"\b(?:never|not|no one|nobody|don't|doesn't|won't|cannot|can't)\b", re.IGNORECASE)
_CLAUSE_BREAK = re.compile(r"[,;:.!?]")
_INVISIBLE = re.compile("[\u200b\u2060-\u2064\ufeff]")  # zero-width characters with no use in plain text
_SPOOFING = re.compile("[\u202a-\u202e\U000e0000-\U000e007f]")  # bidi overrides, Unicode tag characters
_STRIP = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f]")
_TYPOGRAPHY = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                             "\u2013": "-", "\u2014": "-", "\u2212": "-"})


def _clean(text: str) -> str:
    """NFKC, invisible characters removed, curly quotes straightened; line breaks kept."""
    text = unicodedata.normalize("NFKC", text)
    return _STRIP.sub("", text).translate(_TYPOGRAPHY)


def _hidden_characters(text: str) -> bool:
    return bool(_SPOOFING.search(text)) or len(_INVISIBLE.findall(text)) >= 3


def _negated(text: str, start: int) -> bool:
    clause = _CLAUSE_BREAK.split(text[max(0, start - 48):start])[-1]
    return bool(_NEGATION.search(clause))


def _matches(pattern: _Pattern, text: str) -> bool:
    """Whether ``pattern`` matches anywhere it is not negated."""
    return any(
        not (pattern.negatable and _negated(text, match.start())) for match in pattern.regex.finditer(text)
    )


def scan_message(text: str | None) -> list[TextSignal]:
    """Cheap pattern checks on a forwarded message: at most one signal per reason, in bit order."""
    if not text or not text.strip():
        return []
    flat = re.sub(r"\s+", " ", _clean(text)).strip()
    labels: dict[Reason, list[str]] = {}
    for pattern in _PATTERNS:
        found = labels.setdefault(pattern.reason, [])
        if pattern.label not in found and _matches(pattern, flat):
            found.append(pattern.label)
    if _hidden_characters(text):
        labels.setdefault(Reason.CHECKER_INJECTION, []).append("hidden characters")
    signals = [TextSignal(reason, ", ".join(found)[:DETAIL_MAX]) for reason, found in labels.items() if found]
    mismatch = invoice_mismatch(text)
    if mismatch is not None:
        items, total = mismatch
        detail = f"line items add up to {items:,.2f} but the stated total is {total:,.2f}"
        signals.append(TextSignal(Reason.AMOUNT_MISMATCH, detail))
    return sorted(signals, key=lambda s: int(s.reason))


# ----------------------------------------------------------------------
# invoice arithmetic
# ----------------------------------------------------------------------

_CURRENCY = "$\u20ac\u00a3"  # dollar, euro and pound signs
_MONEY = re.compile(r"(?<![\w.,])(-\s?)?(?:[" + _CURRENCY + r"]\s?)?(\d{1,3}(?:,\d{3})+|\d+)\.(\d{2})(?![\d])")
_SEGMENT_BREAK = re.compile(r"\n|;|\|| - |(?<=[.!?])\s+(?=[A-Z])")
_SUBTOTAL_LABEL = re.compile(r"\bsub-? ?total\s*[:=]?\s*(?:usdc|usdt|usd|husd|near|eur|gbp)?\s*$", re.IGNORECASE)
_TOTAL_LABEL = re.compile(
    r"\b(?:grand total|total (?:due|amount|payable)|amount due|balance due|total)\s*(?:\(\w+\))?\s*[:=]?\s*"
    r"(?:usdc|usdt|usd|husd|near|eur|gbp)?\s*$",
    re.IGNORECASE,
)
_DEDUCTION = re.compile(
    r"\b(?:discount|credit|rebate|deduction|less|refund|deposit paid|already paid)\b", re.IGNORECASE
)


def _amount(match: re.Match[str]) -> Decimal:
    value = Decimal(f"{match.group(2).replace(',', '')}.{match.group(3)}")
    return -value if match.group(1) else value


def invoice_mismatch(text: str | None) -> tuple[Decimal, Decimal] | None:
    """``(sum of line items, stated total)`` when an invoice's numbers disagree, else ``None``.

    Conservative on purpose: it needs a labelled total and at least two line
    items with two-decimal amounts. In each line the last unlabelled amount is
    the line total (so "40 x 15.00 = 600.00" counts once), deductions count
    negative, and a subtotal is accepted when the lines before it add up to it.
    """
    if not text:
        return None
    total: Decimal | None = None
    subtotal: Decimal | None = None
    before_subtotal: list[Decimal] = []
    items: list[Decimal] = []
    for segment in _SEGMENT_BREAK.split(_clean(text)):
        line_item: Decimal | None = None
        for match in _MONEY.finditer(segment):
            prefix = segment[max(0, match.start() - 32):match.start()]
            if _SUBTOTAL_LABEL.search(prefix):
                subtotal, before_subtotal = _amount(match), list(items)
            elif _TOTAL_LABEL.search(prefix):
                total = _amount(match)
            else:
                line_item = _amount(match)
        if line_item is not None:
            negative = line_item > 0 and _DEDUCTION.search(segment)
            items.append(-line_item if negative else line_item)
    if total is None or len(items) < 2:
        return None
    item_sum = sum(items, Decimal(0))
    if item_sum == total:
        return None
    if subtotal is not None:
        before = sum(before_subtotal, Decimal(0))
        after = item_sum - before
        if (not before_subtotal or before == subtotal) and subtotal + after == total:
            return None
    return item_sum, total


# ----------------------------------------------------------------------
# look-alike payees
# ----------------------------------------------------------------------

_NETWORK_SUFFIXES = (".testnet", ".near")


def osa_distance(a: str, b: str) -> int:
    """Optimal string alignment distance: Damerau-Levenshtein where an adjacent swap costs one."""
    previous2: list[int] = []
    previous = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        current = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                current[j] = min(current[j], previous2[j - 2] + 1)
        previous2, previous = previous, current
    return previous[-1]


def _strip_network(account_id: str) -> str:
    name = account_id.lower()
    for suffix in _NETWORK_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def skeleton(account_id: str) -> str:
    """The account name with the network suffix, separators and digits removed."""
    return re.sub(r"[^a-z]", "", _strip_network(account_id))


def looks_alike(a: str, b: str) -> bool:
    """Whether two different account ids are close enough to be confused."""
    if a == b:
        return False
    left, right = _strip_network(a), _strip_network(b)
    if len(skeleton(a)) >= 4 and skeleton(a) == skeleton(b):
        return True
    limit = 1 if min(len(left), len(right)) < 6 else 2
    return osa_distance(left, right) <= limit


def lookalike_of(payee: str, trusted: Iterable[str]) -> str | None:
    """The trusted payee that ``payee`` could be mistaken for, if any (never itself)."""
    known = sorted({t for t in trusted if t})
    if payee in known:
        return None
    return next((candidate for candidate in known if looks_alike(payee, candidate)), None)


# ----------------------------------------------------------------------
# payee facts
# ----------------------------------------------------------------------


def _ago(ms: int) -> str:
    """``about 2 h ago``, ``about 3 days ago`` or ``less than an hour ago``."""
    hours = ms / 3_600_000
    if hours < 1:
        return "less than an hour ago"
    if hours < 48:
        return f"about {round(hours)} h ago"
    return f"about {round(hours / 24)} days ago"


def payee_findings(profile: dict[str, Any], now_ms: int) -> list[Finding]:
    """Findings from what the chain says about the payee (see ``PayeeIntel.profile``)."""
    findings: list[Finding] = []
    if profile.get("exists") is False:
        findings.append(Finding(Reason.UNKNOWN_PAYEE, "no NEAR account with this name exists", SOURCE))
        return findings
    reputation = profile.get("reputation") or {}
    reports = int(reputation.get("cancelled_after_flag") or 0)
    last_flag = reputation.get("last_flag_ms")
    if reports >= REPORTS_TO_BRAKE and last_flag is not None and now_ms - int(last_flag) <= REPORT_WINDOW_MS:
        detail = f"{reports} earlier senders cancelled after a warning; latest warning {_ago(now_ms - int(last_flag))}"
        findings.append(Finding(Reason.PAYEE_REPORTED, detail, SOURCE))
    age = profile.get("age_hours")
    if age is not None and not profile.get("age_is_lower_bound") and age < NEW_ACCOUNT_HOURS:
        findings.append(Finding(Reason.NEW_ACCOUNT, f"created {_ago(int(age * 3_600_000))}", SOURCE))
    senders = profile.get("distinct_inbound_senders_24h")
    if senders is not None and senders >= MULE_MIN_SENDERS:
        findings.append(Finding(Reason.MULE_PATTERN, f"{senders} different senders paid it in the last 24 h", SOURCE))
    return findings


# ----------------------------------------------------------------------
# the RiskPort
# ----------------------------------------------------------------------


class RulesRisk:
    """The deterministic checks as a ``RiskPort``: payee facts, the sender's trust list, the message."""

    def __init__(self, intel: PayeeIntel, vault: VaultPort) -> None:
        self.intel = intel
        self.vault = vault

    async def assess(self, hold: Hold, ctx: PaymentContext) -> Verdict:
        started = time.monotonic()
        profile = await self._profile(hold.payee)
        now = await self._now_ms()
        findings = payee_findings(profile, now)
        lookalike = lookalike_of(hold.payee, await self._trusted(hold.sender, ctx))
        if lookalike is not None:
            findings.append(Finding(Reason.LOOKALIKE_PAYEE, f"looks like your trusted payee {lookalike}", SOURCE))
        findings.extend(Finding(s.reason, s.detail, SOURCE) for s in scan_message(ctx.forwarded_text))
        findings.sort(key=lambda f: int(f.reason))
        step = StepRecord(
            step="precheck",
            latency_ms=round((time.monotonic() - started) * 1000),
            tool_calls=[{"name": "payee_profile", "sources": profile.get("source", "")}],
        )
        verdict = Verdict(
            hold_id=hold.id,
            brake=bool(findings),
            hold_for_ms=rules_hold_ms(findings),
            findings=findings,
            steps=[step],
        )
        verdict.summary = note_for(verdict.reason_bits)
        return verdict

    async def _profile(self, account_id: str) -> dict[str, Any]:
        try:
            return await self.intel.profile(account_id)
        except Exception:
            log.exception("payee profile failed for %s; checking the message only", account_id)
            return {}

    async def _now_ms(self) -> int:
        try:
            return await self.vault.now_ms()
        except Exception:
            log.warning("vault clock unavailable; using local time")
            return int(time.time() * 1000)

    async def _trusted(self, sender: str, ctx: PaymentContext) -> set[str]:
        """Trusted payees from the context and the sender's on-chain list (pending ones too)."""
        trusted = set(ctx.trusted_payees)
        try:
            user = await self.vault.get_user(sender)
        except Exception:
            log.warning("could not read the trust list of %s", sender)
            return trusted
        if user is not None:
            trusted.update(entry.payee for entry in user.trusted)
        return trusted
