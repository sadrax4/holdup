"""What the web pages show, built from the engine's views and the store.

Pure functions from data to small view objects, so the templates stay
simple and the logic is easy to test.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..chain import links
from ..config import Settings
from ..messages import finding_tag, fmt_amount, fmt_duration, fmt_utc, short_model, split_context
from ..models import (
    REASON_LABELS,
    Finding,
    Hold,
    HoldStatus,
    Reason,
    StepRecord,
    VaultConfig,
    WhyView,
)

log = logging.getLogger(__name__)

FAILURE_REASONS = (Reason.AI_UNAVAILABLE, Reason.BUDGET_EXHAUSTED)
RULE_SOURCES = ("rules", "precheck")


@dataclass(frozen=True)
class TxLink:
    label: str
    tx_hash: str
    url: str
    simulated: bool

    @property
    def short(self) -> str:
        return links.short_hash(self.tx_hash)


@dataclass(frozen=True)
class ReasonRow:
    label: str
    detail: str
    tags: tuple[str, ...]
    citations: tuple[str, ...] = ()


@dataclass
class TimelineEntry:
    title: str
    detail: str
    tone: str  # "neutral", "brake", "ok", "muted", "bad" or "pending"
    when_ms: int | None = None
    reasons: list[ReasonRow] = field(default_factory=list)  # reasons this step added
    confirmed: int = 0  # earlier reasons this step found again
    tx: TxLink | None = None
    evidence_hash: str | None = None


@dataclass
class HoldView:
    hold: Hold
    amount: str
    status: str
    tone: str
    headline: str
    release_text: str
    hold_length: str
    base_wait: str
    cap: str
    progress: int  # hold length as a share of the cap, in percent
    reasons: list[ReasonRow]
    timeline: list[TimelineEntry]
    evidence_onchain: str | None
    evidence_published: str | None
    context_note: str
    account_urls: dict[str, str]
    context: list[ReasonRow] = field(default_factory=list)  # noted by the checks, not a reason to hold

    @property
    def evidence_match(self) -> bool | None:
        if not self.evidence_onchain or not self.evidence_published:
            return None
        return self.evidence_onchain == self.evidence_published


@dataclass(frozen=True)
class StepRow:
    step: str
    model: str
    provider: str
    fallback: str
    attempts: str
    latency: str
    cost: str
    tools: tuple[str, ...]
    guardrail: str
    request_id: str
    prompt: str
    error: str
    blocked: bool = False  # a block-mode guardrail refused the text: the check worked, nothing failed
    served_model: str = ""  # the provider's own name for the model, when the gateway reported it


# ----------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def usd(value: float) -> str:
    """``$0.0004``: up to six decimals, trailing zeros dropped."""
    return f"${value:.6f}".rstrip("0").rstrip(".")


def tx_link(settings: Settings, label: str, tx_hash: str, *, simulated: bool) -> TxLink:
    return TxLink(label, tx_hash, "" if simulated else links.tx_url(settings, tx_hash), simulated)


def reason_rows(findings: Iterable[Finding]) -> list[ReasonRow]:
    """One row per reason the hold rests on, with the tag of every step that found it (as the bot groups them).

    Context findings are left out; ``context_rows`` lists them.
    """
    reasons, _ = split_context(findings)
    return _rows(reasons)


def context_rows(findings: Iterable[Finding]) -> list[ReasonRow]:
    """One row per reason that was only passed on as context: noted, but no reason to hold on its own."""
    _, context = split_context(findings)
    return _rows(context)


def _rows(findings: Iterable[Finding]) -> list[ReasonRow]:
    grouped: dict[Reason, list[Finding]] = {}
    for finding in findings:
        grouped.setdefault(finding.reason, []).append(finding)
    rows = []
    for reason, items in grouped.items():
        tags: list[str] = []
        citations: list[str] = []
        for item in items:
            tag = finding_tag(item)
            if tag not in tags:
                tags.append(tag)
            citations += [c for c in item.citations if c not in citations]
        label = REASON_LABELS.get(reason, reason.name or str(int(reason)))
        detail = next((i.detail.strip() for i in items if i.detail.strip()), "")
        rows.append(
            ReasonRow(label, "" if detail.lower() == label.lower() else detail, tuple(tags), tuple(citations[:4]))
        )
    return rows


def findings_from_evidence(document: Mapping[str, Any]) -> list[Finding]:
    """The reasons inside a canonical evidence JSON, as findings."""
    findings = []
    for entry in document.get("reasons") or []:
        try:
            reason = Reason(int(entry["bit"]))
        except (KeyError, TypeError, ValueError):
            continue
        findings.append(
            Finding(
                reason=reason,
                detail=str(entry.get("detail") or ""),
                source=str(entry.get("source") or ""),
                model=entry.get("model"),
                citations=[str(c) for c in entry.get("citations") or []],
            )
        )
    return findings


def _load_json(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _extension_title(findings: Sequence[Finding], document: Mapping[str, Any]) -> str:
    """What kind of check led to one extension, from its evidence."""
    if any(f.reason in FAILURE_REASONS for f in findings):
        return "AI check failed, so the brake stays on"
    steps = [s for s in document.get("steps") or [] if isinstance(s, Mapping)]
    if findings and all(f.source in RULE_SOURCES for f in findings) and not any(s.get("model") for s in steps):
        return "Quick rules check: brakes on"
    return "Full check: brakes on"


# ----------------------------------------------------------------------
# the hold page
# ----------------------------------------------------------------------


def hold_headline(hold: Hold) -> tuple[str, str]:
    """Status label and tone for the page's main badge."""
    if hold.status == HoldStatus.HELD:
        return ("Brakes on", "brake") if hold.extensions or hold.flagged else ("Waiting", "held")
    if hold.status == HoldStatus.RELEASING:
        return "Releasing", "held"
    if hold.status == HoldStatus.RELEASED:
        return ("Paid instantly", "ok") if hold.instant else ("Released", "ok")
    if hold.status == HoldStatus.CANCELLED:
        return "Cancelled", "muted"
    return "Payout failed", "bad"


def _context_note(summary: Mapping[str, Any] | None) -> str:
    """What the sender shared, without revealing any of it."""
    if not summary:
        return ""
    parts = []
    if summary.get("has_text"):
        parts.append(f"a message ({summary.get('text_chars', 0)} characters)")
    if summary.get("has_image"):
        parts.append("an image")
    if not parts:
        return ""
    return (
        f"The sender shared {' and '.join(parts)} with this payment. "
        "It was checked, and it stays private: it is not on-chain or on this page."
    )


def build_hold_view(
    settings: Settings,
    why: WhyView,
    *,
    config: VaultConfig,
    now_ms: int,
    txs: Sequence[tuple[str, str]],
    evidence_docs: Sequence[tuple[str, str]],
    context: Mapping[str, Any] | None,
    simulated: bool,
) -> HoldView:
    """``evidence_docs`` are ``(json, sha256)`` in the order they were recorded, one per extension."""
    hold = why.hold
    status, tone = hold_headline(hold)
    amount = fmt_amount(settings, hold.asset, hold.amount)
    length_ms = max(hold.release_at_ms - hold.created_ms, 0)
    progress = min(100, round(100 * length_ms / config.max_hold_ms)) if config.max_hold_ms else 0
    if hold.active:
        release_text = f"Releases at {fmt_utc(hold.release_at_ms, now_ms)} unless the sender or their guardian cancels."
    elif hold.status == HoldStatus.RELEASING:
        release_text = "The release is being paid out."
    elif hold.settled_ms:
        release_text = f"Settled at {fmt_utc(hold.settled_ms, now_ms)}."
    else:
        release_text = ""
    findings = why.findings or [f for _, doc in evidence_docs for f in findings_from_evidence(_load_json(doc))]
    published = sha256_hex(why.evidence_json) if why.evidence_json else None
    return HoldView(
        hold=hold,
        amount=amount,
        status=status,
        tone=tone,
        headline=f"{amount} to {hold.payee}",
        release_text=release_text,
        hold_length=fmt_duration(length_ms),
        base_wait=fmt_duration(config.new_payee_hold_ms),
        cap=fmt_duration(config.max_hold_ms),
        progress=progress,
        reasons=reason_rows(f for f in findings if f.reason),
        context=context_rows(f for f in findings if f.reason),
        timeline=build_timeline(
            settings, hold, config=config, now_ms=now_ms, txs=txs, evidence_docs=evidence_docs, simulated=simulated
        ),
        evidence_onchain=hold.evidence_hash,
        evidence_published=published,
        context_note=_context_note(context),
        account_urls={a: links.account_url(settings, a) for a in (hold.sender, hold.payee)},
    )


def build_timeline(
    settings: Settings,
    hold: Hold,
    *,
    config: VaultConfig,
    now_ms: int,
    txs: Sequence[tuple[str, str]],
    evidence_docs: Sequence[tuple[str, str]],
    simulated: bool,
) -> list[TimelineEntry]:
    """Created, each on-chain extension with its reasons, then the outcome (or what happens next)."""
    by_label: dict[str, list[TxLink]] = {}
    for label, tx_hash in txs:
        by_label.setdefault(label, []).append(tx_link(settings, label, tx_hash, simulated=simulated))
    send = by_label.get("send", [None])[0]
    amount = fmt_amount(settings, hold.asset, hold.amount)
    if hold.instant:
        detail = f"{amount} from {hold.sender} to {hold.payee}, a trusted payee within the daily cap."
        return [TimelineEntry("Paid instantly", detail, "ok", hold.created_ms, tx=send)]

    wait = fmt_duration(config.new_payee_hold_ms)
    release = fmt_utc(hold.created_ms + config.new_payee_hold_ms, now_ms)
    detail = f"{amount} from {hold.sender} to {hold.payee}. New payees wait {wait}: release at {release}."
    return [
        TimelineEntry("Payment held", detail, "neutral", hold.created_ms, tx=send),
        *_extension_entries(by_label.get("extend", []), evidence_docs),
        _outcome_entry(settings, hold, config=config, now_ms=now_ms, txs=by_label),
    ]


def _extension_entries(extend_txs: Sequence[TxLink], evidence_docs: Sequence[tuple[str, str]]) -> list[TimelineEntry]:
    """One entry per extension, listing only the reasons it added to the earlier ones."""
    entries = []
    seen: set[Reason] = set()
    for index in range(max(len(extend_txs), len(evidence_docs))):
        document_text, digest = evidence_docs[index] if index < len(evidence_docs) else ("", None)
        document = _load_json(document_text)
        findings = findings_from_evidence(document)
        found = {f.reason for f in findings}
        decided = document.get("decided_ms")
        entries.append(
            TimelineEntry(
                _extension_title(findings, document),
                "The oracle extended the hold and recorded the reasons and the evidence hash on-chain.",
                "brake",
                decided if isinstance(decided, int) else None,
                reasons=reason_rows(f for f in findings if f.reason not in seen),
                confirmed=len(found & seen),
                tx=extend_txs[index] if index < len(extend_txs) else None,
                evidence_hash=digest,
            )
        )
        seen |= found
    return entries


def _outcome_entry(
    settings: Settings, hold: Hold, *, config: VaultConfig, now_ms: int, txs: Mapping[str, list[TxLink]]
) -> TimelineEntry:
    """How the hold ended, or what happens next while it is still held."""

    def tx(*labels: str) -> TxLink | None:
        return next((txs[label][0] for label in labels if txs.get(label)), None)

    amount = fmt_amount(settings, hold.asset, hold.amount)
    if hold.status == HoldStatus.CANCELLED:
        by_guardian = bool(hold.cancelled_by and hold.cancelled_by != hold.sender)
        who = f"the guardian {hold.cancelled_by}" if by_guardian else "the sender"
        detail = f"Cancelled by {who}. {amount} went back to the sender's vault balance."
        labels = ("guardian_cancel", "cancel") if by_guardian else ("cancel", "guardian_cancel")
        return TimelineEntry("Cancelled", detail, "muted", hold.settled_ms, tx=tx(*labels))
    if hold.status == HoldStatus.RELEASED:
        return TimelineEntry("Released", f"{amount} paid to {hold.payee}.", "ok", hold.settled_ms, tx=tx("release"))
    if hold.status == HoldStatus.FAILED:
        detail = f"The transfer to {hold.payee} failed, so {amount} went back to the sender."
        return TimelineEntry("Payout failed", detail, "bad", hold.settled_ms, tx=tx("release"))
    if hold.status == HoldStatus.RELEASING:
        return TimelineEntry("Releasing", "The payout is in progress.", "pending", None, tx=tx("release"))
    detail = (
        f"Releases at {fmt_utc(hold.release_at_ms, now_ms)} unless the sender or their guardian cancels. "
        "Nobody can release it early, and checks can only add time "
        f"(at most {fmt_duration(config.max_hold_ms)} after the payment)."
    )
    return TimelineEntry("Waiting", detail, "pending", hold.release_at_ms)


# ----------------------------------------------------------------------
# the AI steps page
# ----------------------------------------------------------------------


def _tool_names(calls: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    names: list[str] = []
    for call in calls:
        name = str(call.get("name") or call.get("type") or call.get("tool") or "")
        server = call.get("server_label") or call.get("server")
        if server and name and str(server) not in name:
            name = f"{name} ({server})"
        if name and name not in names:
            names.append(name)
    return tuple(names)


def guardrail_text(value: Any) -> str:
    """A short summary of a guardrail record, e.g. ``checker-injection, monitor: flagged``."""
    if not value:
        return ""
    if isinstance(value, list):
        return "; ".join(filter(None, (guardrail_text(item) for item in value)))
    if not isinstance(value, Mapping):
        return str(value)
    flat = {**value, **value["result"]} if isinstance(value.get("result"), Mapping) else dict(value)
    head = ", ".join(str(flat[k]) for k in ("profile", "mode") if flat.get(k))
    if flat.get("flagged") is True or flat.get("valid") is False:
        verdict = "flagged"
    elif flat.get("valid") is True or flat.get("flagged") is False:
        verdict = "passed"
    else:
        verdict = str(flat.get("verdict") or flat.get("status") or flat.get("action") or "")
    return f"{head}: {verdict}" if head and verdict else head or verdict


def step_rows(steps: Iterable[StepRecord]) -> list[StepRow]:
    rows = []
    for step in steps:
        rows.append(
            StepRow(
                step=step.step,
                model=step.model or ("deterministic rules" if step.step in RULE_SOURCES else ""),
                provider=step.provider or "",
                fallback="" if step.fallback is None else ("yes" if step.fallback else "no"),
                attempts="" if step.attempts is None else str(step.attempts),
                latency="" if step.latency_ms is None else f"{step.latency_ms:,} ms",
                cost="" if step.cost_usd is None else usd(step.cost_usd),
                tools=_tool_names(step.tool_calls),
                guardrail=guardrail_text(step.guardrail),
                request_id=step.request_id or "",
                prompt=(step.prompt_sha256 or "")[:16],
                error=step.error or "",
                blocked=step.error == "guardrail_violation",
                served_model=step.served_model or "",
            )
        )
    return rows


def step_totals(steps: Sequence[StepRecord]) -> dict[str, Any]:
    models = [s for s in steps if s.model]
    return {
        "model_calls": len(models),
        "models": sorted({short_model(s.model) for s in models}),
        "fallbacks": sum(1 for s in steps if s.fallback),
        "cost_usd": round(sum(s.cost_usd or 0.0 for s in steps), 6),
        "latency_ms": sum(s.latency_ms or 0 for s in steps),
    }


def mcp_call_text(call: Mapping[str, Any]) -> str:
    """``tool(arg, ...)``, and for a call the server refused, why after it."""
    args = call.get("args") or {}
    if not isinstance(args, Mapping):
        return f"{call.get('tool', '?')}({args})"
    shown = ", ".join(str(value) for key, value in args.items() if key != "refused")
    text = f"{call.get('tool', '?')}({shown})"
    return f"{text} - refused: {args['refused']}" if "refused" in args else text


# ----------------------------------------------------------------------
# the stats page
# ----------------------------------------------------------------------

STAT_TILES: tuple[tuple[str, str, str], ...] = (
    ("holds", "Payments", "created through the vault"),
    ("braked_holds", "Braked payments", "held longer at least once, each counted once"),
    ("brakes", "Brakes", "from the full check"),
    ("provisional_brakes", "Quick brakes", "rules flagged within seconds"),
    ("cancels_after_flag", "Cancelled after a warning", "counted on-chain against the payee"),
    ("cancels", "Cancels", "by senders"),
    ("guardian_cancels", "Guardian cancels", "stopped by someone the sender chose"),
    ("releases", "Released", "paid out when the wait ended"),
    ("payout_failures", "Payout failures", "refunded to the sender"),
    ("instant", "Instant", "to trusted payees within the daily cap"),
    ("fail_closed", "Fail-closed", "held longer because a check failed"),
    ("ai_checks", "AI checks", "full pipeline runs"),
    ("ai_steps", "AI steps", "model calls across all checks"),
    ("fallbacks", "Fallbacks observed", "steps answered by a fallback model"),
    ("users", "Users", "accounts opened"),
)


def stat_tiles(stats: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """(value, title, note) for the counters the page explains; other keys are only in /stats.json."""
    tiles = [(str(stats[key]), title, note) for key, title, note in STAT_TILES if key in stats]
    if "ai_cost_usd" in stats:
        tiles.append((f"${float(stats['ai_cost_usd'] or 0):.4f}", "Total AI cost", "as reported by the gateway"))
    return tiles


# ----------------------------------------------------------------------
# the proof page
# ----------------------------------------------------------------------

CHECK_TEXT: dict[str, tuple[str, str]] = {
    "code_hash": (
        "The vault runs the published code",
        (
            "Its code hash equals base58(sha256(res/holdvault.wasm)) from the repository, "
            "so anyone can rebuild and compare."
        ),
    ),
    "vault_has_no_keys": (
        "The vault has no access keys",
        (
            "Nobody, including the HoldUp team, can upgrade the contract or move its funds. "
            "It only changes through its own methods."
        ),
    ),
    "oracle_key": (
        "The AI's oracle can only add time",
        "Its only key is a function-call key for extend_hold, which can only make a hold longer, never shorter.",
    ),
    "keeper_key": (
        "The keeper can only release due holds",
        "Its only key is a function-call key for release, which works only after a hold's time has passed.",
    ),
    "config": (
        "The rules are fixed",
        "Oracles, tokens and limits are set once when the vault is created. There is no owner and no setter.",
    ),
}


def check_title(name: str) -> tuple[str, str]:
    return CHECK_TEXT.get(name, (name.replace("_", " ").capitalize(), ""))
