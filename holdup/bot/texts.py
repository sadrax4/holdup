"""Texts for replies to the person's own commands (Telegram and the web console).

Notifications the engine sends on its own (held, brakes on, released, ...)
live in ``holdup.messages``; these helpers reuse its formatting so both read
the same: short and plain, one fact per line, a link where there is one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..config import Settings
from ..messages import fmt_amount, fmt_utc, reason_lines, short_model
from ..models import Hold, HoldStatus, StepRecord, WhyView, reasons_from_bits
from .samples import Sample

CURRENCY_HINT = "send 10 {symbol} to friend.testnet"


def money(settings: Settings, amounts: Mapping[str, int]) -> str:
    """``2 NEAR, 2000 HUSD``: the non-zero amounts, in the settings' asset order."""
    parts = [fmt_amount(settings, asset_id, amounts[asset_id]) for asset_id in settings.assets if amounts.get(asset_id)]
    return ", ".join(parts) or "nothing yet"


def asset_notes(settings: Settings) -> list[str]:
    """One line per token that is not NEAR, saying what it really is (``HUSD: HoldUp demo dollar (test token).``)."""
    return [
        f"{info.symbol}: {info.label}." for info in settings.assets.values() if info.symbol != "NEAR" and info.label
    ]


# ----------------------------------------------------------------------
# hold status
# ----------------------------------------------------------------------


def hold_status(hold: Hold, now_ms: int) -> tuple[str, str]:
    """A short status phrase and a tone (``brake``, ``held``, ``ok``, ``muted``, ``bad``)."""
    if hold.status == HoldStatus.HELD:
        until = fmt_utc(hold.release_at_ms, now_ms)
        if hold.flagged or hold.extensions:
            return f"brakes on, held until {until}", "brake"
        return f"held until {until}", "held"
    if hold.status == HoldStatus.RELEASING:
        return "releasing now", "held"
    if hold.status == HoldStatus.RELEASED:
        return ("paid instantly" if hold.instant else "released to the payee"), "ok"
    if hold.status == HoldStatus.CANCELLED:
        by = "by the guardian" if hold.cancelled_by and hold.cancelled_by != hold.sender else "by the sender"
        return f"cancelled {by}, money returned", "muted"
    return "payout failed, money returned", "bad"


def steps_summary(steps: Sequence[StepRecord]) -> str:
    """``triage gpt-oss-20b, investigator gpt-oss-120b (fallback), ... ($0.0031)``"""
    parts: list[str] = []
    cost = 0.0
    for step in steps:
        label = step.step if not step.model else f"{step.step} {short_model(step.model)}"
        if step.fallback:
            label += " (fallback)"
        if step.error:
            label += " (failed)"
        if label not in parts:
            parts.append(label)
        cost += step.cost_usd or 0.0
    if not parts:
        return ""
    text = ", ".join(parts)
    return f"{text} (${cost:.4f})" if cost else text


# ----------------------------------------------------------------------
# replies
# ----------------------------------------------------------------------


def welcome(
    settings: Settings,
    account_id: str,
    *,
    name: str,
    created: bool,
    balances: Mapping[str, int],
    symbol: str,
) -> str:
    hello = f"Welcome to HoldUp, {name}." if name else "Welcome to HoldUp."
    if created:
        funded = money(settings, settings.demo_funds)
        account_line = f"Your account is {account_id}, funded with demo money: {funded}."
    else:
        hello = f"Welcome back, {name}." if name else "Welcome back."
        account_line = f"Your account is {account_id}. Balance: {money(settings, balances)}."
    return "\n".join(
        [
            hello,
            account_line,
            *asset_notes(settings),
            "",
            "How it works:",
            f"- Pay with a message: {CURRENCY_HINT.format(symbol=symbol)}",
            (
                "- Payments to someone you have not trusted yet wait before they release. "
                "Checks run during the wait and can only add time."
            ),
            "- Forward me the message that asked you to pay, and it gets checked too.",
            "- Only you, or a guardian you choose, can cancel a held payment. Nobody can release it early.",
            "",
            "Practice with /demo. All commands: /help",
        ]
    )


def help_text(symbol: str) -> str:
    return "\n".join(
        [
            "HoldUp commands",
            f"{CURRENCY_HINT.format(symbol=symbol)} - pay someone (or: pay friend.testnet 2.5 NEAR)",
            "Forward or paste a message first to have it checked with your next payment.",
            "/balance - your balances",
            "/holds - recent payments, with Cancel and Why? buttons",
            "/cancel <id> - cancel a held payment; the money comes back to you",
            "/why <id> - what the checks found",
            "/trust <account> - pay this account instantly, after a short delay",
            "/untrust <account> - stop trusting it",
            "/guardian - invite someone who can cancel payments for you",
            "/export - your payments as CSV",
            "/prove - why HoldUp cannot move your money",
            "/demo - practice messages with fictional brands",
            "/stats - what HoldUp has caught so far",
            "/budget - what the AI checks cost",
        ]
    )


def balance_text(settings: Settings, account_id: str, balances: Mapping[str, int]) -> str:
    lines = [f"Balance of {account_id}:"]
    lines += [fmt_amount(settings, asset_id, balances.get(asset_id, 0)) for asset_id in settings.assets]
    lines.append("Payments on hold are not included until they are released or cancelled.")
    return "\n".join(lines)


def holds_text(settings: Settings, holds: Sequence[Hold], now_ms: int, symbol: str) -> str:
    if not holds:
        return "No payments yet. Try: " + CURRENCY_HINT.format(symbol=symbol)
    lines = ["Your recent payments:"]
    for hold in holds:
        status, _ = hold_status(hold, now_ms)
        reasons = ", ".join(
            (r.name or str(int(r))).replace("_", " ").lower() for r in reasons_from_bits(hold.reason_bits)
        )
        suffix = f" ({reasons})" if reasons else ""
        lines.append(f"#{hold.id} {fmt_amount(settings, hold.asset, hold.amount)} to {hold.payee} - {status}{suffix}")
    return "\n".join(lines)


def why_text(settings: Settings, view: WhyView, now_ms: int, *, page: str = "", steps_page: str = "") -> str:
    hold = view.hold
    status, _ = hold_status(hold, now_ms)
    lines = [
        f"Payment #{hold.id}: {fmt_amount(settings, hold.asset, hold.amount)} to {hold.payee}",
        status.capitalize() + ".",
    ]
    findings = [f for f in view.findings if f.reason]
    if findings:
        lines.append("What the checks found:")
        lines += reason_lines(findings)
    elif hold.status == HoldStatus.HELD:
        lines.append("No warning signs so far. New payees always wait; checks can only add time.")
    else:
        lines.append("No warning signs were recorded.")
    summary = steps_summary(view.steps)
    if summary:
        lines.append(f"Checked by: {summary}")
    if view.evidence_hash:
        lines.append(f"Evidence sha256 (on-chain): {view.evidence_hash}")
    if page:
        lines.append(f"Timeline: {page}")
    if steps_page and view.steps:
        lines.append(f"Every AI step: {steps_page}")
    return "\n".join(lines)


def confirm_text(
    settings: Settings,
    asset_id: str,
    amount: int,
    payee: str,
    *,
    context_chars: int | None,
    has_image: bool,
    substituted: str | None = None,
) -> str:
    lines = []
    if substituted:
        lines.append(substituted)
    lines.append(f"Send {fmt_amount(settings, asset_id, amount)} to {payee}?")
    if context_chars is not None or has_image:
        what = "the message" if context_chars is not None else "the image"
        if context_chars is not None and has_image:
            what = "the message and image"
        lines.append(f"I will check it together with {what} you sent me.")
    lines.append("New payees wait before release while checks run; trusted payees are paid at once up to a daily cap.")
    return "\n".join(lines)


def context_saved_text(*, chars: int, has_image: bool, forwarded: bool, symbol: str, next_command: str = "") -> str:
    if has_image and chars:
        what = "this image and its caption"
    elif has_image:
        what = "this image"
    else:
        what = "this forwarded message" if forwarded else "this message"
    lines = [f"Got it. I will check {what} with your next payment in the next 15 minutes."]
    if next_command:
        lines += ["Now type the payment it asks for:", next_command]
    else:
        lines += [
            "Nothing is sent until you type a payment and confirm it, for example:",
            CURRENCY_HINT.format(symbol=symbol),
        ]
    return "\n".join(lines)


def chatter_text(symbol: str) -> str:
    return "\n".join(
        [
            "I send payments with brakes. Try:",
            CURRENCY_HINT.format(symbol=symbol),
            "Forward or paste the message that asked you to pay, and it gets checked too. /help lists everything.",
        ]
    )


def invite_text(code: str, links: Iterable[tuple[str, str]]) -> str:
    links = list(links)
    lines = ["Send this link to someone you trust, like a family member:"]
    if links:
        lines += [url if not label else f"{label}: {url}" for label, url in links]
    else:
        lines.append(f"Invite code: {code}")
    lines += [
        (
            "When they open it, they become your guardian: if a payment shows warning signs, "
            "they get an alert and can cancel it for you."
        ),
        "The link works once and expires in 24 hours.",
    ]
    return "\n".join(lines)


def prove_text(settings: Settings) -> str:
    return "\n".join(
        [
            "HoldUp cannot move your money, and neither can its AI:",
            (
                f"- Your money sits in the vault contract {settings.vault_account}, which has no access keys: "
                "nobody can upgrade it or take funds out."
            ),
            (
                "- The AI's oracle key can call one method, extend_hold, which can only make a hold longer "
                "(72 h after the payment at most)."
            ),
            "- The keeper's key can only call release, and only once a hold's time is up.",
            f"Checked live against the chain: {settings.public_base_url}/prove",
        ]
    )


_STAT_LINES: tuple[tuple[str, ...], ...] = (
    ("holds", "payments created"),
    ("instant", "paid instantly to trusted payees"),
    ("brakes", "brakes from the full check"),
    ("provisional_brakes", "quick brakes from the rules"),
    ("cancels_after_flag", "cancelled after a warning"),
    ("guardian_cancels", "cancelled by a guardian"),
    ("releases", "released"),
    ("fail_closed", "kept on hold because a check failed"),
)


def stats_text(settings: Settings, stats: Mapping[str, Any]) -> str:
    lines = ["HoldUp so far:"]
    for key, label in _STAT_LINES:
        if key in stats:
            lines.append(f"{stats[key]} {label}")
    if "ai_checks" in stats:
        cost = float(stats.get("ai_cost_usd") or 0.0)
        lines.append(
            f"AI: {stats['ai_checks']} checks, {stats.get('ai_steps', 0)} model steps, "
            f"{stats.get('fallbacks', 0)} fallbacks, ${cost:.4f}"
        )
    lines.append(f"More: {settings.public_base_url}/stats")
    return "\n".join(lines)


_MODES = {
    "live": "live, through the Otari gateway",
    "replay": "replaying recorded Otari responses (no live AI calls)",
    "off": "off: deterministic rules only",
}


def budget_text(settings: Settings, stats: Mapping[str, Any]) -> str:
    cost = float(stats.get("ai_cost_usd") or 0.0)
    return "\n".join(
        [
            f"AI checks: {_MODES.get(settings.otari_mode, settings.otari_mode)}.",
            f"Spent so far on this server: ${cost:.4f} over {stats.get('ai_checks', 0)} checks.",
            (
                "The AI budget has a hard ceiling. If it runs out, payments stay on hold (fail-closed) "
                "instead of going through without a check."
            ),
        ]
    )


def demo_intro(samples: Sequence[Sample]) -> str:
    return "\n".join(
        [
            f"{len(samples)} practice messages follow. Every brand and account in them is made up (training samples).",
            "Forward one back to me (or tap its button), then type the payment it asks for and watch the brakes.",
        ]
    )


def sample_intro(sample: Sample, number: int) -> str:
    lines = [f"Training sample {number}: {sample.label}."]
    if sample.trusted:
        lines.append(f"First trust the real supplier: /trust {' '.join(sample.trusted)}")
    lines.append(f"Then forward the next message to me and type: {sample.command}")
    return "\n".join(lines)
