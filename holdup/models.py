"""Shared data types used across chain, risk, engine, bot and web code."""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

NEAR = "near"
"""Asset id for native NEAR. Any other asset id is a NEP-141 token contract account."""


class HoldStatus(str, enum.Enum):
    HELD = "held"
    RELEASING = "releasing"
    RELEASED = "released"
    CANCELLED = "cancelled"
    FAILED = "failed"


class Reason(enum.IntFlag):
    """Reason bits recorded on-chain by extend_hold (u32)."""

    NEW_ACCOUNT = 1 << 0
    MULE_PATTERN = 1 << 1
    IMPERSONATION = 1 << 2
    URGENCY = 1 << 3
    IMPOSSIBLE_RETURNS = 1 << 4
    CHECKER_INJECTION = 1 << 5
    DETAILS_CHANGED = 1 << 6
    PAYEE_REPORTED = 1 << 7
    PHISHING_LINK = 1 << 8
    PUBLIC_WARNING = 1 << 9
    LOOKALIKE_PAYEE = 1 << 10
    AMOUNT_MISMATCH = 1 << 11
    SECRET_REQUEST = 1 << 12
    AI_UNAVAILABLE = 1 << 13
    BUDGET_EXHAUSTED = 1 << 14
    SECOND_OPINION = 1 << 15
    UNKNOWN_PAYEE = 1 << 16


REASON_LABELS: dict[Reason, str] = {
    Reason.NEW_ACCOUNT: "Payee account is brand new",
    Reason.MULE_PATTERN: "Payee is collecting from many unrelated senders",
    Reason.IMPERSONATION: "Message impersonates support, a bank or a known business",
    Reason.URGENCY: "Pressure or an artificial deadline",
    Reason.IMPOSSIBLE_RETURNS: "Promised returns that cannot be real",
    Reason.CHECKER_INJECTION: "Hidden instruction aimed at the AI checker",
    Reason.DETAILS_CHANGED: "Payment details were changed by message",
    Reason.PAYEE_REPORTED: "Other senders cancelled payments to this payee",
    Reason.PHISHING_LINK: "Linked page asks for secrets or imitates a login",
    Reason.PUBLIC_WARNING: "Matches public fraud warnings",
    Reason.LOOKALIKE_PAYEE: "Payee name looks like one of your trusted payees",
    Reason.AMOUNT_MISMATCH: "The numbers in the message do not add up",
    Reason.SECRET_REQUEST: "Asks for a recovery phrase, key or code",
    Reason.AI_UNAVAILABLE: "AI check could not run, so the brake stays on",
    Reason.BUDGET_EXHAUSTED: "AI budget used up, so the brake stays on",
    Reason.SECOND_OPINION: "Independent reviewer model also flagged it",
    Reason.UNKNOWN_PAYEE: "Payee account does not exist",
}


def reasons_from_bits(bits: int) -> list[Reason]:
    return [r for r in Reason if bits & r.value]


# ---------------------------------------------------------------------------
# Assets and amounts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssetInfo:
    asset_id: str
    symbol: str
    decimals: int
    label: str = ""


def format_amount(raw: int, decimals: int, max_fraction: int = 6) -> str:
    """Format a raw integer amount for people, trimming trailing zeros."""
    q = Decimal(raw) / (Decimal(10) ** decimals)
    q = q.quantize(Decimal(1) / (Decimal(10) ** max_fraction), rounding=ROUND_DOWN)
    text = format(q, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def parse_amount(text: str, decimals: int) -> int:
    """Parse a decimal string such as '50' or '12.5' into raw integer units."""
    try:
        value = Decimal(text.strip().replace(",", ""))
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"not a number: {text!r}") from exc
    if value <= 0:
        raise ValueError("amount must be positive")
    raw = value * (Decimal(10) ** decimals)
    if raw != raw.to_integral_value():
        raise ValueError(f"too many decimal places (max {decimals})")
    return int(raw)


# ---------------------------------------------------------------------------
# Chain views (mirror the HoldVault JSON views; U64/U128 arrive as strings)
# ---------------------------------------------------------------------------


def _int(v: Any) -> int | None:
    return None if v is None else int(v)


@dataclass
class Hold:
    id: int
    sender: str
    payee: str
    asset: str
    amount: int
    created_ms: int
    release_at_ms: int
    status: HoldStatus
    instant: bool = False
    flagged: bool = False
    extensions: int = 0
    reason_bits: int = 0
    evidence_hash: str | None = None
    memo_hash: str | None = None
    settled_ms: int | None = None
    cancelled_by: str | None = None

    @classmethod
    def from_view(cls, v: dict[str, Any]) -> Hold:
        return cls(
            id=int(v["id"]),
            sender=v["sender"],
            payee=v["payee"],
            asset=v["asset"],
            amount=int(v["amount"]),
            created_ms=int(v["created_ms"]),
            release_at_ms=int(v["release_at_ms"]),
            status=HoldStatus(v["status"]),
            instant=bool(v.get("instant", False)),
            flagged=bool(v.get("flagged", False)),
            extensions=int(v.get("extensions", 0)),
            reason_bits=int(v.get("reason_bits", 0)),
            evidence_hash=v.get("evidence_hash"),
            memo_hash=v.get("memo_hash"),
            settled_ms=_int(v.get("settled_ms")),
            cancelled_by=v.get("cancelled_by"),
        )

    def to_view(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        for k in ("id", "amount", "created_ms", "release_at_ms", "settled_ms"):
            if d[k] is not None:
                d[k] = str(d[k])
        return d

    @property
    def active(self) -> bool:
        return self.status == HoldStatus.HELD


@dataclass
class PayeeStats:
    holds: int = 0
    flagged: int = 0
    cancelled: int = 0
    cancelled_after_flag: int = 0
    released: int = 0
    last_flag_ms: int | None = None

    @classmethod
    def from_view(cls, v: dict[str, Any] | None) -> PayeeStats:
        if not v:
            return cls()
        return cls(
            holds=int(v.get("holds", 0)),
            flagged=int(v.get("flagged", 0)),
            cancelled=int(v.get("cancelled", 0)),
            cancelled_after_flag=int(v.get("cancelled_after_flag", 0)),
            released=int(v.get("released", 0)),
            last_flag_ms=_int(v.get("last_flag_ms")),
        )


@dataclass
class TrustedPayee:
    payee: str
    effective_ms: int


@dataclass
class UserView:
    account_id: str
    registered_ms: int
    guardian: str | None
    pending_guardian: str | None
    pending_guardian_set: bool
    pending_guardian_effective_ms: int | None
    trusted: list[TrustedPayee]
    active_holds: int

    @classmethod
    def from_view(cls, v: dict[str, Any]) -> UserView:
        pending = v.get("pending_guardian")
        return cls(
            account_id=v["account_id"],
            registered_ms=int(v["registered_ms"]),
            guardian=v.get("guardian"),
            pending_guardian=pending["guardian"] if pending else None,
            pending_guardian_set=pending is not None,
            pending_guardian_effective_ms=int(pending["effective_ms"]) if pending else None,
            trusted=[TrustedPayee(t["payee"], int(t["effective_ms"])) for t in v.get("trusted", [])],
            active_holds=int(v.get("active_holds", 0)),
        )


@dataclass
class VaultConfig:
    oracles: list[str]
    tokens: list[str]
    new_payee_hold_ms: int
    max_hold_ms: int
    trust_delay_ms: int
    guardian_delay_ms: int
    instant_caps: dict[str, int]
    max_active_holds: int
    registration_fee: int
    version: str = ""

    @classmethod
    def from_view(cls, v: dict[str, Any]) -> VaultConfig:
        return cls(
            oracles=list(v["oracles"]),
            tokens=list(v["tokens"]),
            new_payee_hold_ms=int(v["new_payee_hold_ms"]),
            max_hold_ms=int(v["max_hold_ms"]),
            trust_delay_ms=int(v["trust_delay_ms"]),
            guardian_delay_ms=int(v["guardian_delay_ms"]),
            instant_caps={k: int(a) for k, a in v["instant_caps"].items()},
            max_active_holds=int(v["max_active_holds"]),
            registration_fee=int(v["registration_fee"]),
            version=v.get("version", ""),
        )


@dataclass
class TxResult:
    tx_hash: str
    events: list[dict[str, Any]] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    return_value: Any = None
    link: str = ""


@dataclass
class SendResult:
    hold: Hold
    tx: TxResult


@dataclass
class UserAccount:
    """A HoldUp user: a channel identity mapped to a derived NEAR sub-account."""

    user_key: str  # stable channel identity, e.g. "tg:123456" or "web:abc"
    account_id: str
    public_key: str
    private_key: str = field(repr=False, default="")
    display_name: str = ""


# ---------------------------------------------------------------------------
# Risk assessment
# ---------------------------------------------------------------------------


@dataclass
class PaymentContext:
    """Whatever the sender showed us about this payment. Never stored raw on-chain."""

    forwarded_text: str | None = None
    image_bytes: bytes | None = field(default=None, repr=False)
    image_mime: str | None = None
    received_ms: int | None = None
    trusted_payees: list[str] = field(default_factory=list)


@dataclass
class Finding:
    reason: Reason
    detail: str
    # What found it: "rules", "triage", "vision", "mcp", "web_fetch", "web_search", "code_execution",
    # "guardrail", "reviewer" or "fail_closed".
    source: str
    model: str | None = None
    citations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason.name,
            "bit": int(self.reason.value),
            "detail": self.detail,
            "source": self.source,
            "model": self.model,
            "citations": list(self.citations),
        }


@dataclass
class StepRecord:
    step: str  # "precheck", "triage", "vision", "investigator", "reviewer", "guard"
    model: str | None = None  # catalog id that was asked for (after any app-level fallback)
    provider: str | None = None
    fallback: bool | None = None
    attempts: int | None = None
    request_id: str | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    guardrail: dict[str, Any] | None = None
    error: str | None = None
    prompt_sha256: str | None = None
    served_model: str | None = None  # the provider's own name for the model (Otari-Model header)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Verdict:
    hold_id: int
    brake: bool
    hold_for_ms: int  # desired total wait measured from now; 0 means no extension
    findings: list[Finding] = field(default_factory=list)
    steps: list[StepRecord] = field(default_factory=list)
    provisional: bool = False
    summary: str = ""

    @property
    def reason_bits(self) -> int:
        bits = 0
        for f in self.findings:
            bits |= int(f.reason.value)
        return bits


# ---------------------------------------------------------------------------
# Engine results shared by the bot and the web console
# ---------------------------------------------------------------------------


@dataclass
class PaymentOutcome:
    hold: Hold | None
    tx: TxResult | None
    instant: bool = False
    refused: str | None = None  # error code when nothing was sent, e.g. "E_INSUFFICIENT_BALANCE" or "UNKNOWN_PAYEE"
    message: str = ""


@dataclass
class WhyView:
    hold: Hold
    evidence_json: str | None
    evidence_hash: str | None
    findings: list[Finding] = field(default_factory=list)
    steps: list[StepRecord] = field(default_factory=list)
    tx_links: list[tuple[str, str]] = field(default_factory=list)  # (label, url)
