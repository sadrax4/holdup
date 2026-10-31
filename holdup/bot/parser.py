"""Deterministic parser for payment commands.

Amounts and payees are read by these rules only, never by a language model:
a forwarded message can try to talk the AI checker into something, but it
cannot change what the sender typed.

Accepted forms (case-insensitive)::

    send 50 USDC to alice.testnet
    pay alice.testnet 12.5 near
    /pay 1,250 husd to northwindpaper.testnet
    transfer 5 NEAR alice.testnet
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from near import is_valid_account_id

EXAMPLE = "send 5 NEAR to friend.testnet"
USAGE = f"Write a payment like this: {EXAMPLE} (or: pay friend.testnet 5 NEAR)."

_COMMAND = re.compile(r"^/(?:pay|send)(?:@\w+)?(?=\s|$)", re.IGNORECASE)
_VERB = re.compile(r"^(?:send|pay|transfer)(?=\s|$)", re.IGNORECASE)

_AMOUNT = r"(?P<amount>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)"
_ASSET = r"(?P<asset>(?!to\b)[a-z][a-z0-9]{1,9})"
_PAYEE = r"(?P<payee>\S+)"
_AMOUNT_FIRST = re.compile(rf"^{_AMOUNT}\s*{_ASSET}\s+(?:to\s+)?{_PAYEE}$")
_PAYEE_FIRST = re.compile(rf"^(?:to\s+)?{_PAYEE}\s+{_AMOUNT}\s*{_ASSET}$")

_HEX = re.compile(r"[0-9a-f]+")
_TRAILING = ".,;:!?)\"'"


@dataclass(frozen=True)
class PayCommand:
    """A payment the sender typed: symbol as written (upper case), plain decimal amount, payee account."""

    asset_symbol: str
    amount_text: str
    payee: str


@dataclass(frozen=True)
class ParseError:
    """The text looked like a payment but could not be read; ``message`` says how to fix it."""

    message: str


def parse_pay(text: str) -> PayCommand | ParseError | None:
    """Read a payment command.

    Returns ``None`` when the text is not a payment at all (it may be context
    for the next payment), a :class:`ParseError` when it starts like one but
    something is missing or invalid, and a :class:`PayCommand` otherwise.
    """
    body = " ".join(text.split())
    head = _COMMAND.match(body) or _VERB.match(body)
    if head is None:
        return None
    rest = body[head.end() :].strip().rstrip(_TRAILING).lower()
    if not rest:
        return ParseError(USAGE)

    match = _AMOUNT_FIRST.match(rest) or _PAYEE_FIRST.match(rest)
    if match is None:
        return ParseError(_hint(rest))

    amount = _amount(match["amount"])
    if amount is None:
        return ParseError("The amount must be a number above zero, for example 12.5.")
    payee = match["payee"].strip(_TRAILING)
    problem = payee_problem(payee)
    if problem:
        return ParseError(problem)
    return PayCommand(asset_symbol=match["asset"].upper(), amount_text=amount, payee=payee)


def payee_problem(payee: str) -> str | None:
    """Why ``payee`` cannot be a NEAR account, or None if it can."""
    if payee.startswith("@"):
        return f"{payee} looks like a Telegram username. Payees are NEAR accounts, like friend.testnet."
    if "." not in payee and _HEX.fullmatch(payee) and len(payee) != 64:
        return f"{payee} looks like an implicit account, but those are exactly 64 hex characters."
    if not is_valid_account_id(payee):
        return f"{payee} is not a valid NEAR account name. Check the spelling, for example friend.testnet."
    return None


def _amount(text: str) -> str | None:
    """Plain decimal text without grouping or trailing zeros, or None if not positive."""
    try:
        value = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None
    if value <= 0:
        return None
    return format(value.normalize(), "f")


def _hint(rest: str) -> str:
    if "@" in rest:
        return "Payees are NEAR accounts, like friend.testnet, not Telegram usernames. " + USAGE
    if not any(ch.isdigit() for ch in rest):
        return f"How much? {USAGE}"
    if re.match(r"[\d,.]+\s+to\s", rest):
        return f"Which token? {USAGE}"
    if re.search(r"\d,\d{1,2}(?!\d)", rest):
        return f"Use a dot for decimals, like 12.5. {USAGE}"
    if re.search(r"(?<![\w.])\$\s?\d", rest):
        return f"Name the token instead of $, like 50 USDC or 5 NEAR. {USAGE}"
    return USAGE
