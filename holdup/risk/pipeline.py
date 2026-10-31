"""The Otari branch of the risk check.

``OtariRisk`` runs the deterministic rules first and then open-weight models
through the Otari gateway:

1. triage (gpt-oss-20b) classifies the forwarded message, with an injection
   guardrail in monitor mode on the request;
2. vision (glm-5.3-flash) reads an image, when the sender shared one;
3. when injection is suspected, a guardrail gate in block mode decides whether
   the raw text may reach a model that has tools. If it is blocked, the later
   steps only get the facts that HoldUp extracted from it;
4. the investigator (gpt-oss-120b) checks the payee and the links with the
   near-intel MCP server, web search and web fetch, and computes every number
   with code execution;
5. a reviewer from another model family gives a blind second opinion on large
   payments and when triage and the investigator disagree.

Findings merge with an OR rule: any step that flags keeps the brake on. Model
claims are checked before they count: a web search citation must be a page the
gateway reports the search returned, a fetched page one it reports fetching,
and an amount mismatch must hold when HoldUp redoes the arithmetic on numbers
that appear in the message. A gateway failure that leaves no brake fails
closed: the payment waits another hour and the engine tries again.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from ..config import Settings
from ..models import NEAR, Finding, Hold, PaymentContext, Reason, StepRecord, Verdict, format_amount
from . import prompts
from .budgets import KeyPool
from .otari import (
    CODE_EXECUTION,
    FALLBACK_CHAIN,
    INVESTIGATOR_MODEL,
    REVIEWER_MODEL,
    TRIAGE_MODEL,
    VISION_MODEL,
    WEB_FETCH,
    WEB_SEARCH,
    AllCandidatesFailed,
    BadModelOutput,
    BudgetExceeded,
    GuardrailUnavailable,
    GuardrailViolation,
    InsufficientFunds,
    OtariClient,
    OtariError,
    OtariRequestError,
    ToolNotEnabled,
    fallbacks_for,
    family,
)
from .prompts.contracts import (
    AmountCheck,
    InvestigatorResult,
    MoneyClaim,
    ReviewerResult,
    TriageResult,
    VisionResult,
)

log = logging.getLogger(__name__)

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
MAX_HOLD_MS = 72 * HOUR_MS
FAIL_CLOSED_MS = HOUR_MS

INJECTION_PROFILE = "checker-injection"
MCP_SERVER = "near-intel"
MCP_TOOLS = ("payee_profile", "payee_reputation", "sender_history", "hold_status", "fetch_page")
MCP_PURPOSE = (
    "HoldUp facts about NEAR accounts and this payment: the age and activity of an account, holds and "
    "cancellations recorded on chain, earlier payments between the sender and the payee, the status of "
    "this hold, and a bounded reader for web pages."
)
REPLAY_BASE_URL = "https://replay.holdup.invalid"
"""Where the recorded exchanges say the MCP server and guardrail lived; replayed requests never leave the process."""

MAX_TEXT_CHARS = 6_000
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_LINKS = 5
REVIEW_STABLE_UNITS = 100
REVIEW_NEAR = 10
STABLECOINS = frozenset({"USDC", "HUSD", "USDT"})
STRONG = frozenset({Reason.SECRET_REQUEST, Reason.CHECKER_INJECTION})

_SIGNALS = (
    ("urgency", Reason.URGENCY, "sets a deadline or threatens a loss"),
    ("impersonation", Reason.IMPERSONATION, "claims to come from support, a bank or a known business"),
    ("secret_request", Reason.SECRET_REQUEST, "asks for a recovery phrase, key or code"),
    ("details_changed", Reason.DETAILS_CHANGED, "says the payment details changed"),
    ("injection_suspected", Reason.CHECKER_INJECTION, "contains text aimed at the AI checker"),
    ("impossible_returns", Reason.IMPOSSIBLE_RETURNS, "promises returns that cannot be real"),
)

DEFAULT_DETAIL = {
    Reason.NEW_ACCOUNT: "the payee account is new",
    Reason.MULE_PATTERN: "the payee receives money from many unrelated senders",
    Reason.IMPERSONATION: "the message impersonates a business or a support team",
    Reason.URGENCY: "the message pushes for a fast payment",
    Reason.IMPOSSIBLE_RETURNS: "the message promises returns that cannot be real",
    Reason.CHECKER_INJECTION: "the message contains text aimed at the AI checker",
    Reason.DETAILS_CHANGED: "the payment details differ from earlier payments",
    Reason.PAYEE_REPORTED: "other senders cancelled payments to this payee",
    Reason.PHISHING_LINK: "a linked page asks for secrets or imitates a login",
    Reason.PUBLIC_WARNING: "public warnings describe this pattern",
    Reason.LOOKALIKE_PAYEE: "the payee name imitates a trusted payee",
    Reason.AMOUNT_MISMATCH: "the amounts in the message do not add up",
    Reason.SECRET_REQUEST: "a recovery phrase, key or code is requested",
}

_REASON_ORDER = (
    Reason.SECRET_REQUEST,
    Reason.CHECKER_INJECTION,
    Reason.PHISHING_LINK,
    Reason.PAYEE_REPORTED,
    Reason.IMPERSONATION,
    Reason.DETAILS_CHANGED,
    Reason.AMOUNT_MISMATCH,
    Reason.LOOKALIKE_PAYEE,
    Reason.PUBLIC_WARNING,
    Reason.MULE_PATTERN,
    Reason.NEW_ACCOUNT,
    Reason.URGENCY,
    Reason.IMPOSSIBLE_RETURNS,
    Reason.UNKNOWN_PAYEE,
    Reason.SECOND_OPINION,
)
_SOURCE_ORDER = (
    "rules",
    "precheck",
    "triage",
    "vision",
    "guardrail",
    "mcp",
    "web_search",
    "web_fetch",
    "code_execution",
    "investigator",
    "reviewer",
)

_PAYEE_LABELS = {
    "exists": "account exists",
    "age_hours": "account age in hours",
    "distinct_inbound_senders_24h": "distinct senders in the last 24 hours",
    "tx_count": "transactions seen",
    "holds": "payments to it that HoldUp held",
    "released": "of those, released",
    "cancelled_after_flag": "reports: payments cancelled after a HoldUp warning",
}
_URL = re.compile(r"https?://[^\s<>()\[\]{}\"'\u0060]+", re.IGNORECASE)
_FORWARDED_FROM = re.compile(r"\A\s*forwarded from ([^\n]{1,80}?):[ \t]*(?:\n|\Z)", re.IGNORECASE)
_NOT_WORDISH = re.compile(r"[^A-Za-z0-9 .&-]+")
_QUOTE_CHARS = str.maketrans("", "", "\"'\u0060")


# ----------------------------------------------------------------------
# small pure helpers (module level so tests can reach them)
# ----------------------------------------------------------------------


def clean_text(text: str | None) -> str:
    """The forwarded text without control or invisible format characters, capped in length."""
    if not text:
        return ""
    kept = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf"))
    kept = kept.strip()
    if len(kept) > MAX_TEXT_CHARS:
        kept = kept[:MAX_TEXT_CHARS] + "\n[message truncated]"
    return kept


def find_links(text: str) -> list[str]:
    links: list[str] = []
    for match in _URL.finditer(text):
        url = match.group(0).rstrip(".,;:!?")
        if url not in links:
            links.append(url)
    return links[:MAX_LINKS]


def untrusted(title: str, text: str) -> str:
    """``text`` between markers whose id the text itself cannot know in advance."""
    tag = "m-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
    body = text.replace("<<<", "\u2039" * 3).replace(">>>", "\u203a" * 3)
    return (
        f"{title} (untrusted data between the markers)\n<<<BEGIN UNTRUSTED {tag}>>>\n{body}\n<<<END UNTRUSTED {tag}>>>"
    )


def _url_key(url: str) -> str:
    """URLs compared the way the gateway reports them: no query string, fragment or trailing slash."""
    parts = urlsplit(url.strip())
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


def _words(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def quotes_message(detail: str, message: str, window: int = 24) -> bool:
    """Whether ``detail`` repeats a run of ``window`` characters of the message."""
    d, m = _words(detail), _words(message)
    if len(d) < window or not m:
        return False
    return any(d[i : i + window] in m for i in range(len(d) - window + 1))


def safe_detail(text: str, message: str, fallback: str) -> str:
    """A model-written detail fit for on-chain evidence: short, no links or quotes, no message text."""
    detail = _URL.sub("", " ".join(text.split())).translate(_QUOTE_CHARS).strip(" .;:,-")
    if not detail or quotes_message(detail, message):
        return fallback
    return detail[:140]


def _amount(value: str) -> Decimal:
    digits = re.sub(r"[^0-9.\-]", "", value.replace(",", ""))
    if not digits:
        raise InvalidOperation(value)
    return Decimal(digits)


def _appears(amount: str, text: str) -> bool:
    plain = amount.replace(",", "").strip()
    plain = re.sub(r"[^0-9.]", "", plain)
    if not plain:
        return False
    haystack = text.replace(",", "")
    # A whole amount: not part of a longer number, and not the integer part of a decimal.
    return re.search(rf"(?<![0-9.]){re.escape(plain)}(?![0-9]|\.[0-9])", haystack) is not None


@dataclass(frozen=True)
class AmountMismatch:
    items_total: Decimal
    stated_total: Decimal

    @property
    def detail(self) -> str:
        # No currency: the message names one, the payment may be in another (HUSD standing in for USDC).
        gap = abs(self.stated_total - self.items_total)
        return f"line items add up to {self.items_total} but the stated total is {self.stated_total} (off by {gap})"


def verify_amount_check(check: AmountCheck | None, message: str) -> AmountMismatch | None:
    """Redo a model's sum: a mismatch counts only if HoldUp's own arithmetic agrees.

    Every line item and the stated total must appear in the message as
    written, the model's computed total must equal HoldUp's sum, and that sum
    must differ from the stated total.
    """
    if check is None or not check.line_items:
        return None
    try:
        items = [_amount(item) for item in check.line_items]
        stated = _amount(check.stated_total)
        computed = _amount(check.computed_total)
    except InvalidOperation:
        return None
    if not all(_appears(value, message) for value in [*check.line_items, check.stated_total]):
        return None
    total = sum(items, Decimal(0))
    if total != computed or total == stated:
        return None
    return AmountMismatch(total, stated)


def _claim_text(claim: MoneyClaim) -> str:
    """A money claim reduced to plain words and digits, fit to show a model that has tools."""
    label = _NOT_WORDISH.sub(" ", claim.label)
    amount = re.sub(r"[^0-9.,]", "", claim.amount)[:20]
    currency = _NOT_WORDISH.sub("", claim.currency)
    return " ".join(f"{label} {amount} {currency}".split())


def _gateway_can_reach(url: str, *, with_token: bool) -> bool:
    """Whether a hosted gateway could call ``url`` (public host; https when a token is sent)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        return False
    if with_token and parts.scheme != "https":
        return False  # mcp: an authorization_token requires https
    if host == "localhost" or host.endswith((".local", ".internal", ".invalid", ".localhost")):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return address.is_global


def hold_for(findings: Iterable[Finding]) -> int:
    """24 h for one or two signals; 72 h for three or more, a secret request or injection."""
    reasons = {f.reason for f in findings} - {Reason.SECOND_OPINION}
    if not reasons:
        return 0
    if reasons & STRONG or len(reasons) >= 3:
        return MAX_HOLD_MS
    return DAY_MS


def _rank(finding: Finding) -> tuple[int, int]:
    reason = _REASON_ORDER.index(finding.reason) if finding.reason in _REASON_ORDER else len(_REASON_ORDER)
    source = _SOURCE_ORDER.index(finding.source) if finding.source in _SOURCE_ORDER else len(_SOURCE_ORDER)
    return reason, source


def merge_findings(findings: Iterable[Finding]) -> list[Finding]:
    """One finding per (reason, source, model), most serious reasons first."""
    merged: dict[tuple[Reason, str, str | None], Finding] = {}
    for finding in findings:
        key = (finding.reason, finding.source, finding.model)
        if key not in merged:
            merged[key] = Finding(
                finding.reason, finding.detail, finding.source, finding.model, list(finding.citations)
            )
            continue
        kept = merged[key]
        for url in finding.citations:
            if url not in kept.citations:
                kept.citations.append(url)
    return sorted(merged.values(), key=_rank)


# ----------------------------------------------------------------------
# per-payment state
# ----------------------------------------------------------------------


@dataclass
class _Case:
    hold: Hold
    ctx: PaymentContext
    client: OtariClient
    text: str
    links: list[str]
    claimed_sender: str
    symbol: str
    amount_text: str
    large: bool
    end_user: str
    image_text: str = ""
    withheld: bool = False
    money_claims: list[MoneyClaim] = field(default_factory=list)
    payee_facts: dict[str, Any] | None = None

    @property
    def message(self) -> str:
        """All untrusted text, for checks against quoting and for arithmetic."""
        return f"{self.text}\n{self.image_text}".strip()


@dataclass
class _Outcome:
    step: str
    record: StepRecord | None = None
    findings: list[Finding] = field(default_factory=list)
    failure: Exception | None = None
    result: Any = None
    blocked: bool = False  # a guardrail in block mode refused the request

    @property
    def label(self) -> str:
        if self.failure is not None:
            return f"failed ({getattr(self.failure, 'label', type(self.failure).__name__)})"
        if self.blocked:
            return "blocked by the guardrail"
        n = len(self.findings)
        return f"{n} finding{'s' if n != 1 else ''}" if n else "nothing found"


@dataclass
class _Rules:
    findings: list[Finding]
    records: list[StepRecord]
    hold_for_ms: int


def _systemic(failure: Exception | None) -> bool:
    """Failures that every later request would hit too, so later steps are skipped."""
    if isinstance(failure, (BudgetExceeded, InsufficientFunds, GuardrailUnavailable)):
        return True
    return isinstance(failure, OtariRequestError) and failure.status in (401, 403)


async def _nothing() -> None:
    return None


# ----------------------------------------------------------------------
# the pipeline
# ----------------------------------------------------------------------


class OtariRisk:
    """``RiskPort`` for the Otari branch: rules, then open-weight models through Otari."""

    def __init__(
        self,
        settings: Settings,
        client: OtariClient,
        intel: Any,
        rules: Any,
        mcp_url: str | None,
        guard_url: str | None,
        guard_token_fn: Callable[[int], str] | None = None,
        mcp_token_fn: Callable[[int], str] | None = None,
        *,
        user_keys: Callable[[str], str | None] | None = None,
        mcp_log: Callable[[int], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.intel = intel
        self.rules = rules
        self.guard_token_fn = guard_token_fn
        self.mcp_token_fn = mcp_token_fn
        self.mcp_log = mcp_log
        if client.offline:
            # Replayed exchanges were recorded with the MCP server and the guardrail declared.
            mcp_url = mcp_url or f"{REPLAY_BASE_URL}/mcp"
            guard_url = guard_url or f"{REPLAY_BASE_URL}/validate"
        else:
            mcp_url = self._reachable(mcp_url, "MCP server", with_token=mcp_token_fn is not None)
            guard_url = self._reachable(guard_url, "guardrail endpoint", with_token=False)
        self.mcp_url = mcp_url
        self.guard_url = guard_url
        if user_keys is None:
            pool = KeyPool.from_env()
            user_keys = pool.key_for if len(pool) else None
        self.user_keys = user_keys

    @staticmethod
    def _reachable(url: str | None, what: str, *, with_token: bool) -> str | None:
        if url and not _gateway_can_reach(url, with_token=with_token):
            log.warning("%s at %s is not reachable from the hosted gateway; leaving it out", what, url)
            return None
        return url

    def prompt_versions(self) -> dict[str, str]:
        return prompts.versions()

    # ------------------------------------------------------------------
    # RiskPort
    # ------------------------------------------------------------------

    async def assess(self, hold: Hold, ctx: PaymentContext) -> Verdict:
        rules = await self._rules(hold, ctx)
        case = self._case(hold, ctx)
        image = self._image(ctx)
        if not case.text and image is None:
            return self._decide(case, rules, [], note="no message or image to check")

        triage, vision = await asyncio.gather(
            self._guarded("triage", self._triage(case)) if case.text else _nothing(),
            self._guarded("vision", self._vision(case, image)) if image is not None else _nothing(),
        )
        outcomes = [o for o in (triage, vision) if o is not None]
        if vision is not None and isinstance(vision.result, VisionResult):
            case.image_text = clean_text(vision.result.transcript)
            case.links = (case.links + [u for u in find_links(case.image_text) if u not in case.links])[:MAX_LINKS]
            case.money_claims += vision.result.money_claims
        if triage is not None and isinstance(triage.result, TriageResult):
            case.money_claims = triage.result.money_claims + case.money_claims
        if any(_systemic(o.failure) for o in outcomes):
            return self._decide(case, rules, outcomes, note="later steps skipped")

        if any(o.blocked for o in outcomes):
            case.withheld = True
        elif self._injection_suspected(rules, outcomes):
            if self.guard_url:
                gate = await self._guarded("gate", self._gate(case))
                outcomes.append(gate)
                # Text the blocking guardrail refused, or could not judge, never reaches a model with tools.
                case.withheld = gate.blocked or gate.failure is not None
            else:
                case.withheld = True  # suspected, and no guardrail to clear it

        reviewing = case.large
        investigator, reviewer = await asyncio.gather(
            self._guarded("investigator", self._investigate(case)),
            self._guarded("reviewer", self._review(case, avoid=family(INVESTIGATOR_MODEL)))
            if reviewing
            else _nothing(),
        )
        outcomes.append(investigator)
        served = investigator.record.model if investigator.record and investigator.record.model else INVESTIGATOR_MODEL
        if (
            reviewer is not None
            and reviewer.record is not None
            and family(reviewer.record.model or "") == family(served)
        ):
            # The investigator fell back to the family of the reviewer; ask a third family instead.
            reviewer = await self._guarded("reviewer", self._review(case, avoid=family(served)))
        if reviewer is None and self._disagree(triage, investigator, case):
            reviewer = await self._guarded("reviewer", self._review(case, avoid=family(served)))
        if reviewer is not None:
            outcomes.append(reviewer)
        return self._decide(case, rules, outcomes)

    async def gate(self, hold: Hold, ctx: PaymentContext) -> Verdict:
        """Only the blocking guardrail, on the forwarded text: a cheap request the provider never sees if flagged."""
        case = self._case(hold, ctx)
        if not case.text or not self.guard_url:
            return Verdict(hold.id, False, 0, summary="guardrail gate: nothing to check")
        outcome = await self._guarded("gate", self._gate(case))
        steps = [outcome.record] if outcome.record else []
        if outcome.blocked:
            return Verdict(hold.id, True, MAX_HOLD_MS, outcome.findings, steps, summary="guardrail gate: blocked")
        if outcome.failure is not None:
            return self._fail_closed(case, [outcome], [], steps, 0)
        return Verdict(hold.id, False, 0, [], steps, summary="guardrail gate: passed")

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------

    def _case(self, hold: Hold, ctx: PaymentContext) -> _Case:
        text = clean_text(ctx.forwarded_text)
        info = self.settings.assets.get(hold.asset)
        if info is not None:
            amount_text, symbol = format_amount(hold.amount, info.decimals), info.symbol
            units = Decimal(hold.amount) / (Decimal(10) ** info.decimals)
            large = (
                (units >= REVIEW_NEAR)
                if hold.asset == NEAR
                else (info.symbol in STABLECOINS and units >= REVIEW_STABLE_UNITS)
            )
        else:
            amount_text, symbol, large = str(hold.amount), f"raw units of {hold.asset}", False
        match = _FORWARDED_FROM.match(text)  # the header the bot adds, never a line inside the message
        sender = " ".join(_NOT_WORDISH.sub(" ", match.group(1)).split())[:48] if match else ""
        return _Case(
            hold=hold,
            ctx=ctx,
            client=self._client_for(hold.sender),
            text=text,
            links=find_links(text),
            claimed_sender=sender,
            symbol=symbol,
            amount_text=amount_text,
            large=large,
            end_user=self._end_user(hold.sender),
        )

    def _client_for(self, account_id: str) -> OtariClient:
        if self.client.offline or self.user_keys is None:
            return self.client
        key = self.user_keys(account_id)
        return self.client.with_api_key(key) if key else self.client

    def _end_user(self, account_id: str) -> str:
        secret = (self.settings.secret or "holdup").encode()
        return hmac.new(secret, f"otari-end-user:{account_id}".encode(), hashlib.sha256).hexdigest()[:16]

    @staticmethod
    def _image(ctx: PaymentContext) -> tuple[bytes, str] | None:
        mime = (ctx.image_mime or "image/jpeg").lower()
        if not ctx.image_bytes or not mime.startswith("image/") or len(ctx.image_bytes) > MAX_IMAGE_BYTES:
            return None
        return ctx.image_bytes, mime

    def _metadata(self, case: _Case, step: str) -> dict[str, str]:
        # Tags for cost attribution per step and hold; end_user is a keyed hash, never an account or chat id.
        return {"app": "holdup", "step": step, "hold": str(case.hold.id), "end_user": case.end_user}

    def _guardrails(self, case: _Case, mode: str) -> list[dict[str, Any]] | None:
        if not self.guard_url:
            return None
        # Request-level guardrail (guardrails: Using a guardrail). In monitor mode the
        # provider still runs and the verdict comes back in the Otari-Guardrails header,
        # which the hosted gateway only sends on responses that are not streamed (observed
        # 2026-10-31), so triage is where HoldUp reads it. In block mode a flagged input is
        # refused with 403 guardrail_violation.
        entry: dict[str, Any] = {"profile": INJECTION_PROFILE, "url": self.guard_url, "mode": mode, "on": ["input"]}
        if mode == "block":
            entry["on_unavailable"] = "block"
        if self.guard_token_fn is not None:
            # Forwarded to our /validate endpoint, which checks it before answering.
            entry["validate_kwargs"] = {"token": self.guard_token_fn(case.hold.id)}
        return [entry]

    def _mcp_servers(self, case: _Case) -> list[dict[str, Any]] | None:
        if not self.mcp_url:
            return None
        server: dict[str, Any] = {
            "name": MCP_SERVER,
            "url": self.mcp_url,
            "purpose_hint": MCP_PURPOSE,
            "allowed_tools": list(MCP_TOOLS),
        }
        if self.mcp_token_fn is not None:
            # A bearer token per hold: the server only answers about this hold and its parties.
            server["authorization_token"] = self.mcp_token_fn(case.hold.id)
        return [server]

    # ------------------------------------------------------------------
    # message views
    # ------------------------------------------------------------------

    def _facts(self, case: _Case, *, payee: dict[str, Any] | None = None) -> str:
        trusted = ", ".join(case.ctx.trusted_payees) or "none"
        lines = [
            "PAYMENT FACTS (looked up by HoldUp)",
            f"- payee account: {case.hold.payee}",
            f"- amount: {case.amount_text} {case.symbol}",
            f"- payees this sender already trusts: {trusted}",
            f"- links in the message: {', '.join(case.links) or 'none'}",
        ]
        if payee:
            lines.append("PAYEE FACTS (looked up by HoldUp)")
            for key, label in _PAYEE_LABELS.items():
                if key in payee:
                    value = payee[key]
                    lines.append(f"- {label}: {'yes' if value is True else 'no' if value is False else value}")
        return "\n".join(lines)

    def _message_view(self, case: _Case) -> str:
        if case.withheld:
            claims = "; ".join(_claim_text(claim) for claim in case.money_claims[:12])
            return "\n".join(
                [
                    "FORWARDED MESSAGE: withheld, because the injection guardrail flagged it. Facts HoldUp extracted:",
                    f"- claimed sender: {case.claimed_sender or 'not stated'}",
                    "- links: as listed in the payment facts",
                    f"- amounts mentioned: {claims or 'none'}",
                ]
            )
        parts = []
        if case.text:
            parts.append(untrusted("FORWARDED MESSAGE", case.text))
        if case.image_text:
            parts.append(untrusted("TEXT READ FROM THE IMAGE", case.image_text))
        return "\n\n".join(parts) or "No message was forwarded."

    async def _payee(self, case: _Case) -> dict[str, Any] | None:
        """What HoldUp itself knows about the payee, for steps without the MCP server."""
        if case.payee_facts is None and self.intel is not None:
            try:
                profile = await asyncio.wait_for(self.intel.profile(case.hold.payee), 5)
            except Exception as exc:  # intel is best effort; the models can still look
                log.warning("payee lookup for %s failed: %r", case.hold.payee, exc)
                profile = {}
            profile = profile if isinstance(profile, dict) else {}
            reputation = profile.get("reputation") if isinstance(profile.get("reputation"), dict) else {}
            merged = {**{k: reputation.get(k) for k in ("holds", "released", "cancelled_after_flag")}, **profile}
            case.payee_facts = {k: merged[k] for k in _PAYEE_LABELS if merged.get(k) is not None}
        return case.payee_facts or None

    # ------------------------------------------------------------------
    # steps
    # ------------------------------------------------------------------

    async def _guarded(self, step: str, work: Awaitable[_Outcome]) -> _Outcome:
        """Run a step; an unexpected error becomes a failed step instead of crashing the check."""
        try:
            return await work
        except Exception as exc:
            log.exception("%s step crashed", step)
            return _Outcome(step, StepRecord(step=step, error=type(exc).__name__), failure=exc)

    async def _rules(self, hold: Hold, ctx: PaymentContext) -> _Rules:
        if self.rules is None:
            return _Rules([], [], 0)
        try:
            verdict = await asyncio.wait_for(self.rules.assess(hold, ctx), 20)
        except Exception as exc:
            log.exception("rules failed for hold %s", hold.id)
            return _Rules([], [StepRecord(step="precheck", error=type(exc).__name__)], 0)
        records = list(verdict.steps) or [StepRecord(step="precheck")]
        return _Rules(list(verdict.findings), records, verdict.hold_for_ms if verdict.brake else 0)

    async def _triage(self, case: _Case) -> _Outcome:
        prompt = prompts.load("triage")
        messages = [
            {"role": "system", "content": prompt.text},
            {"role": "user", "content": f"{self._facts(case)}\n\n{untrusted('FORWARDED MESSAGE', case.text)}"},
        ]
        try:
            result, record = await case.client.chat(
                "triage",
                TRIAGE_MODEL,
                messages,
                output=TriageResult,
                guardrails=self._guardrails(case, "monitor"),
                metadata=self._metadata(case, "triage"),
                max_tokens=2_000,
                reasoning_effort="low",
                fallbacks=fallbacks_for(TRIAGE_MODEL),
                prompt_sha256=prompt.sha256,
            )
        except GuardrailViolation as exc:
            return self._blocked("triage", exc)
        except OtariError as exc:
            return _Outcome("triage", exc.record, failure=exc)
        findings = [
            Finding(reason, f"message {phrase}", "triage", record.model)
            for name, reason, phrase in _SIGNALS
            if getattr(result, name)
        ]
        return _Outcome("triage", record, findings + self._monitored(record), result=result)

    async def _vision(self, case: _Case, image: tuple[bytes, str]) -> _Outcome:
        prompt = prompts.load("vision")
        data, mime = image
        url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        messages = [
            {"role": "system", "content": prompt.text},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": self._facts(case)},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            },
        ]
        try:
            result, record = await case.client.chat(
                "vision",
                VISION_MODEL,
                messages,
                output=VisionResult,
                metadata=self._metadata(case, "vision"),
                max_tokens=3_000,
                fallbacks=(REVIEWER_MODEL,),  # the only other chain model that reads images
                prompt_sha256=prompt.sha256,
            )
        except GuardrailViolation as exc:
            return self._blocked("vision", exc)
        except OtariError as exc:
            return _Outcome("vision", exc.record, failure=exc)
        findings = [
            Finding(reason, f"image {phrase}", "vision", record.model)
            for name, reason, phrase in _SIGNALS
            if getattr(result, name, False)
        ]
        return _Outcome("vision", record, findings, result=result)

    async def _gate(self, case: _Case) -> _Outcome:
        prompt = prompts.load("gate")
        text = case.message
        messages = [
            {"role": "system", "content": prompt.text},
            {"role": "user", "content": untrusted("FORWARDED MESSAGE", text)},
        ]
        try:
            _, record = await case.client.chat(
                "gate",
                TRIAGE_MODEL,
                messages,
                guardrails=self._guardrails(case, "block"),
                metadata=self._metadata(case, "gate"),
                max_tokens=256,
                reasoning_effort="low",
                prompt_sha256=prompt.sha256,
            )
        except GuardrailViolation as exc:
            return self._blocked("gate", exc)
        except BadModelOutput as exc:  # the guardrail let it through; the answer itself does not matter
            return _Outcome("gate", exc.record)
        except OtariError as exc:
            return _Outcome("gate", exc.record, failure=exc)
        return _Outcome("gate", record)

    async def _investigate(self, case: _Case) -> _Outcome:
        prompt = prompts.load("investigator")
        tools = [{"type": WEB_SEARCH, "max_uses": 3}]  # tools: Bounding the searches one request runs
        if case.links:
            # Fetch accepts no options: the live gateway refuses max_uses on it with a 400.
            tools.append({"type": WEB_FETCH})
        tools.append({"type": CODE_EXECUTION})
        mcp = self._mcp_servers(case)
        payee = None if mcp else await self._payee(case)
        messages = [
            {"role": "system", "content": prompt.text},
            {"role": "user", "content": f"{self._facts(case, payee=payee)}\n\n{self._message_view(case)}"},
        ]
        disabled: set[str] = set()
        while True:
            offered = [t for t in tools if t["type"] not in disabled]
            servers = mcp if "mcp" not in disabled else None
            try:
                result, record = await case.client.chat(
                    "investigator",
                    INVESTIGATOR_MODEL,
                    messages,
                    output=InvestigatorResult,
                    tools=offered or None,
                    mcp_servers=servers,
                    guardrails=self._guardrails(case, "monitor"),
                    metadata=self._metadata(case, "investigator"),
                    max_tokens=4_000,
                    reasoning_effort="medium",
                    max_tool_iterations=8,
                    fallbacks=fallbacks_for(INVESTIGATOR_MODEL),
                    prompt_sha256=prompt.sha256,
                )
                break
            except ToolNotEnabled as exc:
                if not exc.tools - disabled:
                    return _Outcome("investigator", exc.record, failure=exc)
                log.warning("investigator: %s; retrying without %s", exc, ", ".join(sorted(exc.tools)))
                disabled |= exc.tools
            except GuardrailViolation as exc:
                return self._blocked("investigator", exc)
            except AllCandidatesFailed as exc:
                # The gateway reports an MCP server it cannot reach as a generic 502 for every
                # offering (observed on the hosted gateway), so try once more without it.
                if servers is None:
                    return _Outcome("investigator", exc.record, failure=exc)
                log.warning("investigator failed with near-intel attached (%s); retrying without it", exc.label)
                disabled.add("mcp")
                payee = await self._payee(case)
                messages[1] = {
                    "role": "user",
                    "content": f"{self._facts(case, payee=payee)}\n\n{self._message_view(case)}",
                }
            except OtariError as exc:
                return _Outcome("investigator", exc.record, failure=exc)
        available = {t["type"] for t in offered} | ({"mcp"} if servers else set())
        findings = self._investigator_findings(case, result, record, available)
        return _Outcome("investigator", record, findings + self._monitored(record), result=result)

    async def _review(self, case: _Case, *, avoid: str) -> _Outcome:
        model = next(m for m in (REVIEWER_MODEL, VISION_MODEL, INVESTIGATOR_MODEL) if family(m) != avoid)
        fallbacks = tuple(m for m in FALLBACK_CHAIN if m != model and family(m) != avoid)
        prompt = prompts.load("reviewer")
        payee = await self._payee(case)
        messages = [
            {"role": "system", "content": prompt.text},
            {"role": "user", "content": f"{self._facts(case, payee=payee)}\n\n{self._message_view(case)}"},
        ]
        tools: list[dict[str, Any]] | None = [{"type": CODE_EXECUTION}]
        while True:
            try:
                result, record = await case.client.chat(
                    "reviewer",
                    model,
                    messages,
                    output=ReviewerResult,
                    tools=tools,
                    metadata=self._metadata(case, "reviewer"),
                    max_tokens=3_000,
                    max_tool_iterations=4 if tools else None,
                    fallbacks=fallbacks,
                    prompt_sha256=prompt.sha256,
                )
                break
            except ToolNotEnabled as exc:
                if tools is None:
                    return _Outcome("reviewer", exc.record, failure=exc)
                log.warning("reviewer: %s; reviewing without code execution", exc)
                tools = None
            except GuardrailViolation as exc:
                return self._blocked("reviewer", exc)
            except OtariError as exc:
                return _Outcome("reviewer", exc.record, failure=exc)
        findings = []
        for item in result.reasons:
            reason = Reason[item.reason]
            if reason == Reason.AMOUNT_MISMATCH:
                mismatch = verify_amount_check(result.amount_check, case.message)
                if mismatch is None:
                    log.info("reviewer claimed an amount mismatch that HoldUp's arithmetic does not confirm")
                    continue
                detail = mismatch.detail
            else:
                detail = safe_detail(item.detail, case.message, DEFAULT_DETAIL[reason])
            findings.append(Finding(reason, detail, "reviewer", record.model))
        return _Outcome("reviewer", record, findings, result=result)

    # ------------------------------------------------------------------
    # turning answers into findings
    # ------------------------------------------------------------------

    def _blocked(self, step: str, exc: GuardrailViolation) -> _Outcome:
        detail = (
            "the injection guardrail blocked the message before a model with tools could read it"
            if step == "gate"
            else "a guardrail in block mode refused the request"
        )
        return _Outcome(step, exc.record, [Finding(Reason.CHECKER_INJECTION, detail, "guardrail")], blocked=True)

    @staticmethod
    def _monitored(record: StepRecord) -> list[Finding]:
        verdict = record.guardrail or {}
        if verdict.get("verdict") != "flagged":
            return []
        score = verdict.get("score")
        suffix = f" (score {float(score):.2f})" if isinstance(score, (int, float)) else ""
        return [Finding(Reason.CHECKER_INJECTION, f"the injection guardrail flagged the message{suffix}", "guardrail")]

    def _investigator_findings(
        self, case: _Case, result: InvestigatorResult, record: StepRecord, available: set[str]
    ) -> list[Finding]:
        searched = {
            _url_key(url)
            for call in record.tool_calls
            if call.get("name") == WEB_SEARCH and call.get("status") == "completed"
            for url in call.get("sources") or []
        }
        fetched = {
            _url_key(call["url"])
            for call in record.tool_calls
            if call.get("name") == WEB_FETCH and call.get("status") == "completed" and call.get("url")
        }
        computed = {c.label for c in result.computations if c.result}
        mcp_ran = "mcp" in available and (self.mcp_log is None or bool(self.mcp_log(case.hold.id)))
        message_links = {_url_key(u) for u in case.links}

        def attribute(tool: str, citations: list[str], computation: str) -> tuple[str, list[str]]:
            if tool == "web_search" and WEB_SEARCH in available:
                cited = [u for u in citations if _url_key(u) in searched][:2]
                if cited:
                    return "web_search", cited
            elif tool == "web_fetch":
                cited = [u for u in citations if _url_key(u) in fetched][:2]
                if cited and WEB_FETCH in available:
                    return "web_fetch", cited
                read = [u for u in citations if _url_key(u) in message_links][:2]
                if read and mcp_ran and WEB_FETCH not in available:
                    return "mcp", read  # read through the fetch_page tool of near-intel
            elif tool == "near-intel" and mcp_ran:
                return "mcp", [u for u in citations if _url_key(u) in message_links][:2]
            elif tool == "code_execution" and CODE_EXECUTION in available and computed:
                if not computation or computation in computed:
                    return "code_execution", []
            return "investigator", []

        findings: list[Finding] = []
        for item in result.findings:
            reason = Reason[item.reason]
            source, citations = attribute(item.tool, item.citations, item.computation)
            if reason == Reason.AMOUNT_MISMATCH:
                mismatch = verify_amount_check(result.amount_check, case.message)
                if mismatch is None:
                    log.info("investigator claimed an amount mismatch that HoldUp's arithmetic does not confirm")
                    continue
                detail = mismatch.detail
            else:
                detail = safe_detail(item.detail, case.message, DEFAULT_DETAIL[reason])
            findings.append(Finding(reason, detail, source, record.model, citations))
        if not any(f.reason == Reason.AMOUNT_MISMATCH for f in findings):
            # A mismatch the model computed but did not list still counts once HoldUp confirms it.
            mismatch = verify_amount_check(result.amount_check, case.message)
            if mismatch is not None:
                source = "code_execution" if CODE_EXECUTION in available and computed else "investigator"
                findings.append(Finding(Reason.AMOUNT_MISMATCH, mismatch.detail, source, record.model))
        return findings

    # ------------------------------------------------------------------
    # decision
    # ------------------------------------------------------------------

    @staticmethod
    def _injection_suspected(rules: _Rules, outcomes: list[_Outcome]) -> bool:
        found = [*rules.findings, *(f for o in outcomes for f in o.findings)]
        return any(f.reason == Reason.CHECKER_INJECTION for f in found)

    @staticmethod
    def _disagree(triage: _Outcome | None, investigator: _Outcome, case: _Case) -> bool:
        if case.withheld or triage is None or triage.result is None or investigator.result is None:
            return False  # nothing comparable: a step failed, or the investigator never saw the text
        return bool(triage.findings) != bool(investigator.findings)

    def _decide(self, case: _Case, rules: _Rules, outcomes: list[_Outcome], note: str = "") -> Verdict:
        steps = [*rules.records, *(o.record for o in outcomes if o.record is not None)]
        ai = [f for o in outcomes for f in o.findings]
        reviewer = next((o for o in outcomes if o.step == "reviewer"), None)
        others_flagged = bool(rules.findings) or any(o.findings for o in outcomes if o.step != "reviewer")
        if reviewer is not None and reviewer.findings and others_flagged:
            model = reviewer.record.model if reviewer.record else None
            ai.append(
                Finding(
                    Reason.SECOND_OPINION, "an independent reviewer model also flagged this payment", "reviewer", model
                )
            )
        findings = merge_findings([*rules.findings, *ai])
        if not ai and any(o.failure is not None for o in outcomes):
            return self._fail_closed(case, outcomes, findings, steps, rules.hold_for_ms)
        if not findings:
            wait = 0
        elif not ai:
            wait = rules.hold_for_ms  # the AI steps added nothing, so the rules decide alone
        else:
            wait = max(hold_for(findings), rules.hold_for_ms)
        return Verdict(
            hold_id=case.hold.id,
            brake=bool(findings),
            hold_for_ms=wait,
            findings=findings,
            steps=steps,
            summary=self._summary(rules, outcomes, note),
        )

    def _fail_closed(
        self, case: _Case, outcomes: list[_Outcome], findings: list[Finding], steps: list[StepRecord], rules_wait: int
    ) -> Verdict:
        failed = [o for o in outcomes if o.failure is not None]
        budget = any(isinstance(o.failure, (BudgetExceeded, InsufficientFunds)) for o in failed)
        first = failed[0]
        cause = getattr(first.failure, "label", type(first.failure).__name__)
        model = first.record.model if first.record else None
        finding = Finding(
            Reason.BUDGET_EXHAUSTED if budget else Reason.AI_UNAVAILABLE,
            f"the {first.step} step could not run ({cause})",
            first.step,
            model,
        )
        return Verdict(
            hold_id=case.hold.id,
            brake=True,
            hold_for_ms=max(FAIL_CLOSED_MS, rules_wait),
            findings=[finding, *findings],
            steps=steps,
            summary="fail-closed: " + "; ".join(f"{o.step} {o.label}" for o in outcomes),
        )

    @staticmethod
    def _summary(rules: _Rules, outcomes: list[_Outcome], note: str) -> str:
        n = len(rules.findings)
        parts = [f"rules: {n} finding{'s' if n != 1 else ''}" if n else "rules: nothing found"]
        parts += [f"{o.step}: {o.label}" for o in outcomes]
        if note:
            parts.append(note)
        return "; ".join(parts)
