"""Texts the engine sends to people, shared by the Telegram bot and the web console.

Style: short and plain, one fact per line, every chain action with an
explorer link (or a simulated id offline). Brake messages start with
"Brakes on - held until <time UTC>" and tag each reason with what found it.
Accounts are described by "reports" from other senders; no message labels
an account as a scam.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from .chain import errors as E
from .chain.treasury import TREASURY_EMPTY
from .config import Settings
from .models import NEAR, REASON_LABELS, STILL_CHECKING, STILL_CHECKING_LABEL, Finding, Hold, Reason, format_amount

Buttons = list[list[tuple[str, str]]]

HOUR_MS = 3_600_000
DETAIL_MAX = 120
CONTEXT_HEADING = "Context, not a reason to hold on its own:"

_PLAIN_TAGS = {
    "rules": "rules",
    "precheck": "rules",
    "mcp": "MCP near-intel",
    "web_fetch": "web fetch",
    "code_execution": "code execution",
    "guardrail": "guardrail",
    "fail_closed": "fail-closed",
    "still_checking": "still checking",
}


# ----------------------------------------------------------------------
# formatting helpers
# ----------------------------------------------------------------------


def fmt_amount(settings: Settings, asset_id: str, amount: int) -> str:
    info = settings.assets.get(asset_id)
    if info is None:
        return f"{amount} {asset_id}"
    return f"{format_amount(amount, info.decimals)} {info.symbol}"


def fmt_utc(ms: int, now_ms: int | None = None) -> str:
    """``09:00:20 UTC`` when soon, ``09:00 UTC`` later today, ``3 Nov 09:00 UTC`` otherwise."""
    when = datetime.fromtimestamp(ms / 1000, UTC)
    if now_ms is not None:
        now = datetime.fromtimestamp(now_ms / 1000, UTC)
        if when.date() == now.date():
            return when.strftime("%H:%M:%S UTC" if ms - now_ms < HOUR_MS else "%H:%M UTC")
    return f"{when.day} {when:%b %H:%M} UTC"


def fmt_duration(ms: int) -> str:
    seconds = max(0, round(ms / 1000))
    if seconds < 60:
        return f"{seconds} s"
    minutes, _ = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h" if not minutes else f"{hours} h {minutes} min"
    return f"{hours // 24} days"


def short_model(model: str | None) -> str:
    return (model or "").rsplit("/", 1)[-1]


def finding_tag(finding: Finding) -> str:
    """What found a reason, e.g. ``[triage gpt-oss-20b]`` or ``[web search, 2 sources]``."""
    source = finding.source
    if source == "web_search":
        count = len(finding.citations)
        if count:
            return f"[web search, {count} source{'s' if count != 1 else ''}]"
        return "[web search]"
    if source in _PLAIN_TAGS:
        return f"[{_PLAIN_TAGS[source]}]"
    model = short_model(finding.model)
    label = source.replace("_", " ")
    return f"[{label} {model}]" if model else f"[{label}]"


def split_context(findings: Iterable[Finding]) -> tuple[list[Finding], list[Finding]]:
    """The findings a hold rests on, and those passed on only as context.

    A reason that any step found as a reason is one, and its context findings go with it
    (their tags show that the rules saw it too).
    """
    findings = list(findings)
    held = {f.reason for f in findings if not f.context}
    return [f for f in findings if f.reason in held], [f for f in findings if f.reason not in held]


def reason_lines(findings: Iterable[Finding]) -> list[str]:
    """One line per reason, merging the tags of every step that found it.

    Context findings follow under ``CONTEXT_HEADING``, so they are never read as reasons.
    """
    reasons, context = split_context(findings)
    lines = _grouped_lines(reasons)
    if context:
        lines += [CONTEXT_HEADING, *_grouped_lines(context)]
    return lines


def _grouped_lines(findings: Iterable[Finding]) -> list[str]:
    grouped: dict[Reason, list[Finding]] = {}
    for finding in findings:
        grouped.setdefault(finding.reason, []).append(finding)
    lines = []
    for reason, items in grouped.items():
        label = REASON_LABELS.get(reason, reason.name)
        if all(item.source == STILL_CHECKING for item in items):
            label = STILL_CHECKING_LABEL
        tags: list[str] = []
        for item in items:
            tag = finding_tag(item)
            if tag not in tags:
                tags.append(tag)
        detail = next((i.detail.strip() for i in items if i.detail.strip()), "")
        if detail and detail.lower() != label.lower():
            if len(detail) > DETAIL_MAX:
                detail = detail[: DETAIL_MAX - 3].rstrip() + "..."
            lines.append(f"- {label}: {detail} {' '.join(tags)}")
        else:
            lines.append(f"- {label} {' '.join(tags)}")
    return lines


def _with_footer(lines: list[str], *, tx_text: str = "", page: str = "") -> str:
    if page:
        lines.append(f"Details: {page}")
    if tx_text:
        lines.append(tx_text)
    return "\n".join(lines)


# ----------------------------------------------------------------------
# buttons (callback data: cancel:<id>, gcancel:<id>, why:<id>)
# ----------------------------------------------------------------------


def sender_buttons(hold_id: int) -> Buttons:
    return [[("Cancel & refund", f"cancel:{hold_id}"), ("Why?", f"why:{hold_id}")]]


def guardian_buttons(hold_id: int, owner_name: str) -> Buttons:
    return [[(f"Cancel for {owner_name}", f"gcancel:{hold_id}"), ("Why?", f"why:{hold_id}")]]


# ----------------------------------------------------------------------
# payments
# ----------------------------------------------------------------------


def held(settings: Settings, hold: Hold, *, now_ms: int, tx_text: str = "", page: str = "") -> str:
    wait = fmt_duration(hold.release_at_ms - hold.created_ms)
    lines = [
        f"Payment held: {fmt_amount(settings, hold.asset, hold.amount)} to {hold.payee}.",
        f"New payees wait {wait}: it releases at {fmt_utc(hold.release_at_ms, now_ms)} unless you cancel.",
        "Checks are running now. They can add time, never release early.",
    ]
    return _with_footer(lines, tx_text=tx_text, page=page)


def instant_paid(settings: Settings, hold: Hold, *, tx_text: str = "") -> str:
    lines = [
        f"Paid instantly: {fmt_amount(settings, hold.asset, hold.amount)} to {hold.payee} (trusted payee)."
    ]
    return _with_footer(lines, tx_text=tx_text)


def provisional_brake(
    settings: Settings,
    hold: Hold,
    findings: Iterable[Finding],
    until_ms: int,
    *,
    now_ms: int,
    tx_text: str = "",
    page: str = "",
) -> str:
    lines = [
        f"Brakes on - held until {fmt_utc(until_ms, now_ms)}",
        f"{fmt_amount(settings, hold.asset, hold.amount)} to {hold.payee}. Quick checks found:",
        *reason_lines(findings),
        "The full check is still running. Cancel any time to get your money back.",
    ]
    return _with_footer(lines, tx_text=tx_text, page=page)


def brake(
    settings: Settings,
    hold: Hold,
    findings: Iterable[Finding],
    until_ms: int,
    *,
    now_ms: int,
    tx_text: str = "",
    page: str = "",
) -> str:
    lines = [
        f"Brakes on - held until {fmt_utc(until_ms, now_ms)}",
        f"{fmt_amount(settings, hold.asset, hold.amount)} to {hold.payee}:",
        *reason_lines(findings),
        "Only you or your guardian can cancel. Nobody can release it early.",
    ]
    return _with_footer(lines, tx_text=tx_text, page=page)


def fail_closed(
    settings: Settings,
    hold: Hold,
    detail: str,
    until_ms: int,
    *,
    now_ms: int,
    budget: bool = False,
    final: bool = False,
    tx_text: str = "",
) -> str:
    cause = "The AI check budget is used up" if budget else f"The AI check could not run ({detail})"
    lines = [f"Brakes on - held until {fmt_utc(until_ms, now_ms)}"]
    if final:
        lines += [
            f"{cause}, and it is still failing after the extra waiting time.",
            f"{fmt_amount(settings, hold.asset, hold.amount)} to {hold.payee} will release then unless you cancel.",
        ]
    else:
        lines += [
            f"{cause}, so the brake stays on instead of letting the payment through.",
            "We will try again. You can cancel any time.",
        ]
    return _with_footer(lines, tx_text=tx_text)


def check_slow(settings: Settings, hold: Hold, until_ms: int, *, now_ms: int, tx_text: str = "") -> str:
    lines = [
        f"Brakes on - held until {fmt_utc(until_ms, now_ms)}",
        "The AI check is taking longer than the hold, so we kept the brake on. You will hear when it finishes.",
    ]
    return _with_footer(lines, tx_text=tx_text)


def checks_clear(
    settings: Settings,
    hold: Hold,
    *,
    now_ms: int,
    after_brake: bool = False,
    context: Iterable[Finding] = (),
) -> str:
    when = fmt_utc(hold.release_at_ms, now_ms)
    if after_brake:
        return (
            "Full check finished: no further warning signs.\n"
            f"The earlier brake stays until {when}; holds can only get longer."
        )
    payment = f"{fmt_amount(settings, hold.asset, hold.amount)} to {hold.payee} releases at {when}."
    lines = reason_lines(context)
    if not lines:
        return f"Checks finished: nothing unusual found. {payment}"
    return "\n".join([f"Checks finished: no reason to hold it longer. {payment}", *lines])


def brake_not_recorded(settings: Settings, hold: Hold, findings: Iterable[Finding]) -> str:
    lines = [
        f"Warning signs on your payment of {fmt_amount(settings, hold.asset, hold.amount)} to {hold.payee}:",
        *reason_lines(findings),
        "We could not add time on-chain yet. We keep trying, and HoldUp will not release it meanwhile,",
        f"but anyone can release it once its time comes at {fmt_utc(hold.release_at_ms)}.",
        "Cancel now if you are not sure.",
    ]
    return "\n".join(lines)


def guardian_alert(
    settings: Settings,
    hold: Hold,
    findings: Iterable[Finding],
    until_ms: int,
    owner_name: str,
    *,
    now_ms: int,
    page: str = "",
) -> str:
    lines = [
        f"Guardian alert: {owner_name}'s payment of {fmt_amount(settings, hold.asset, hold.amount)} "
        f"to {hold.payee} is held until {fmt_utc(until_ms, now_ms)}.",
        *reason_lines(findings),
        f"You can cancel it for them; the money goes back to {owner_name}'s HoldUp balance.",
    ]
    return _with_footer(lines, page=page)


def cancelled(settings: Settings, hold: Hold, *, tx_text: str = "") -> str:
    lines = [
        f"Cancelled. {fmt_amount(settings, hold.asset, hold.amount)} is back in your HoldUp balance "
        f"(payment to {hold.payee})."
    ]
    return _with_footer(lines, tx_text=tx_text)


def cancelled_by_guardian(settings: Settings, hold: Hold, guardian_name: str, *, tx_text: str = "") -> str:
    lines = [
        f"Your guardian {guardian_name} cancelled your payment of "
        f"{fmt_amount(settings, hold.asset, hold.amount)} to {hold.payee}.",
        "The money is back in your HoldUp balance.",
    ]
    return _with_footer(lines, tx_text=tx_text)


def guardian_cancelled(settings: Settings, hold: Hold, owner_name: str, *, tx_text: str = "") -> str:
    lines = [
        f"Done: you cancelled {owner_name}'s payment of {fmt_amount(settings, hold.asset, hold.amount)} "
        f"to {hold.payee}. The money went back to {owner_name}."
    ]
    return _with_footer(lines, tx_text=tx_text)


def owner_cancelled_flagged(settings: Settings, hold: Hold, owner_name: str) -> str:
    return (
        f"{owner_name} cancelled the payment of {fmt_amount(settings, hold.asset, hold.amount)} "
        f"to {hold.payee} you were alerted about. Nothing else to do."
    )


def released(settings: Settings, hold: Hold, *, tx_text: str = "") -> str:
    lines = [f"Released: {fmt_amount(settings, hold.asset, hold.amount)} paid to {hold.payee}."]
    return _with_footer(lines, tx_text=tx_text)


def payout_failed(settings: Settings, hold: Hold, *, tx_text: str = "") -> str:
    lines = [
        f"The payout to {hold.payee} failed, so {fmt_amount(settings, hold.asset, hold.amount)} "
        "went back to your HoldUp balance."
    ]
    return _with_footer(lines, tx_text=tx_text)


def received(settings: Settings, hold: Hold, sender_name: str) -> str:
    return f"You received {fmt_amount(settings, hold.asset, hold.amount)} from {sender_name}."


# ----------------------------------------------------------------------
# trust and guardians
# ----------------------------------------------------------------------


def trust_added(payee: str, effective_ms: int, *, now_ms: int, tx_text: str = "") -> str:
    if effective_ms <= now_ms:
        first = f"{payee} is a trusted payee: payments are instant up to the daily cap."
    else:
        first = (
            f"{payee} will be a trusted payee from {fmt_utc(effective_ms, now_ms)}. "
            "Trust waits a little so a thief cannot add a payee and pay it at once."
        )
    return _with_footer([first], tx_text=tx_text)


def trust_removed(payee: str, *, tx_text: str = "") -> str:
    return _with_footer([f"{payee} is no longer trusted. New payments to it will wait."], tx_text=tx_text)


def guardian_set(guardian_name: str, effective_ms: int, *, now_ms: int, tx_text: str = "") -> str:
    if effective_ms <= now_ms:
        first = f"{guardian_name} is now your guardian and can cancel suspicious payments for you."
    else:
        first = (
            f"{guardian_name} becomes your guardian at {fmt_utc(effective_ms, now_ms)}. "
            "Guardian changes wait so nobody can swap your guardian in a hurry."
        )
    return _with_footer([first], tx_text=tx_text)


def guardian_trust_notice(owner_name: str, payee: str, effective_ms: int, *, now_ms: int) -> str:
    return (
        f"{owner_name} added {payee} as a trusted payee; payments to it become instant from "
        f"{fmt_utc(effective_ms, now_ms)}. If that looks wrong, check with {owner_name}."
    )


def guardian_change_notice(owner_name: str, new_guardian: str, effective_ms: int, *, now_ms: int) -> str:
    return (
        f"{owner_name} asked to make {new_guardian} their guardian instead of you, from "
        f"{fmt_utc(effective_ms, now_ms)}. Until then you stay in charge. If that looks wrong, check with "
        f"{owner_name}; you can also keep the change from happening."
    )


def guardian_change_buttons(owner_account: str) -> Buttons:
    """A veto button for the guardian in charge (Telegram allows 64 bytes of callback data)."""
    data = f"gveto:{owner_account}"
    return [[("Keep me as guardian", data)]] if len(data.encode()) <= 64 else []


def guardian_change_stopped(owner_name: str, *, tx_text: str = "") -> str:
    line = f"Done: you stay {owner_name}'s guardian; the change they asked for was dropped."
    return _with_footer([line], tx_text=tx_text)


def guardian_change_vetoed(guardian_name: str) -> str:
    return (
        f"Your guardian {guardian_name} stopped the guardian change on your account and stays your guardian. "
        "If you did ask for the change, talk to them."
    )


def guardian_accepted(owner_name: str, effective_ms: int, *, now_ms: int) -> str:
    when = "now" if effective_ms <= now_ms else f"from {fmt_utc(effective_ms, now_ms)}"
    return (
        f"You are {owner_name}'s guardian {when}. If one of their payments shows warning signs, "
        "you will get an alert and can cancel it."
    )


# ----------------------------------------------------------------------
# refusals and errors
# ----------------------------------------------------------------------

_ERRORS = {
    E.E_NOT_REGISTERED: "Your HoldUp account is not set up yet. Send /start first.",
    E.E_NOT_SENDER: "Only the person who sent this payment can cancel it.",
    E.E_NOT_GUARDIAN: "Only this person's guardian can cancel it for them.",
    E.E_HOLD_NOT_FOUND: "There is no payment with that number.",
    E.E_HOLD_NOT_ACTIVE: "That payment is no longer on hold (already released or cancelled).",
    E.E_NOT_DUE: "That payment is still on hold.",
    E.E_INSUFFICIENT_BALANCE: (
        "Not enough balance. Payments, new trusted payees and guardian changes also pay a small storage "
        "fee from your NEAR balance. Nothing was sent."
    ),
    E.E_UNKNOWN_ASSET: "That token is not supported. Nothing was sent.",
    E.E_ZERO_AMOUNT: "The amount must be more than zero.",
    E.E_SELF_PAYMENT: "You cannot pay or trust your own account.",
    E.E_TOO_MANY_HOLDS: "You have too many payments on hold. Wait for one to finish or cancel one.",
    E.E_TOO_MANY_TRUSTED: "You already trust 50 payees. Remove one first.",
    E.E_BAD_GUARDIAN: "You cannot be your own guardian.",
    E.E_MEMO_TOO_LONG: "The payment note is too long.",
    E.E_INSOLVENT: "The vault is temporarily not accepting new records. Please try again later.",
    E.E_BAD_ARGS: "That account name is not valid on NEAR.",
    E.E_RPC: "The NEAR network did not answer. Nothing changed; please try again.",
    E.E_TX_REJECTED: "The network refused the transaction. Nothing changed.",
    E.E_TX_FAILED: "The transaction failed. Nothing changed.",
    E.E_NOT_CONFIGURED: "HoldUp is not fully configured on this server.",
    E.UNKNOWN_PAYEE: "That account does not exist on NEAR. Check the name; nothing was sent.",
    E.INVALID_PAYEE: "That is not a valid NEAR account name. Nothing was sent.",
    E.INVALID_INVITE: "This guardian link is invalid, already used or expired. Ask for a new one.",
    TREASURY_EMPTY: (
        "HoldUp's supply of test money is used up for the moment, so it cannot set up new accounts or pay "
        "network fees right now. It refills itself from the NEAR testnet faucet; please try again in about "
        "10 minutes. Nothing changed."
    ),
    E.ONBOARDING_UNFINISHED: (
        "Your HoldUp account is only partly set up: {reason}. Nothing is lost; send /start again in a few "
        "minutes to finish it."
    ),
}
_UNFINISHED_REASONS = {
    TREASURY_EMPTY: "HoldUp's test money ran out for a moment (it refills itself from the NEAR faucet)",
    E.E_RPC: "the NEAR network did not answer in time",
}


def not_enough(settings: Settings, asset_id: str, *, have: int, need: int, fee: int = 0) -> str:
    """Why a payment was refused for its balance: which asset, what the sender has, what it takes.

    ``fee`` is the hold's storage fee, which every payment pays in NEAR.
    """
    if asset_id == NEAR and fee and need <= fee:
        return (
            f"Not enough NEAR for the storage fee: each payment pays {fmt_amount(settings, NEAR, fee)} from your "
            f"NEAR balance, and you have {fmt_amount(settings, NEAR, have)}. Nothing was sent."
        )
    if asset_id == NEAR and fee:
        return (
            f"Not enough NEAR: you have {fmt_amount(settings, NEAR, have)}, and this payment needs "
            f"{fmt_amount(settings, NEAR, need)} with the {fmt_amount(settings, NEAR, fee)} storage fee. "
            "Nothing was sent."
        )
    return (
        f"Not enough {_symbol(settings, asset_id)}: you have {fmt_amount(settings, asset_id, have)} and this payment "
        f"is {fmt_amount(settings, asset_id, need)}. Nothing was sent. /balance shows what you hold."
    )


def _symbol(settings: Settings, asset_id: str) -> str:
    info = settings.assets.get(asset_id)
    return info.symbol if info is not None else asset_id


def error_text(code: str, detail: str = "") -> str:
    """A friendly sentence for a refusal or ``VaultError`` code."""
    text = _ERRORS.get(code)
    if code == E.ONBOARDING_UNFINISHED:  # ``detail`` is the code of the step that failed
        return _ERRORS[code].format(reason=_UNFINISHED_REASONS.get(detail, "a step did not go through"))
    if text:
        return text
    return f"That did not work ({code}{': ' + detail if detail else ''})."


def refusal(code: str, *, payee: str = "", detail: str = "") -> str:
    if code == E.UNKNOWN_PAYEE and payee:
        return f"{payee} does not exist on NEAR. Check the name; nothing was sent."
    return error_text(code, detail)
