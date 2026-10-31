"""Error codes of the HoldVault contract and the exception that carries them.

The contract panics with ``"E_CODE: human text"``. Both vault adapters (the
NEAR one and the in-memory one) raise :class:`VaultError` with that code, so
callers branch on ``exc.code`` and never parse messages themselves.
"""

from __future__ import annotations

import re
from typing import Any

E_NOT_REGISTERED = "E_NOT_REGISTERED"
E_NOT_SENDER = "E_NOT_SENDER"
E_NOT_GUARDIAN = "E_NOT_GUARDIAN"
E_NOT_ORACLE = "E_NOT_ORACLE"
E_NOT_TOKEN = "E_NOT_TOKEN"
E_HOLD_NOT_FOUND = "E_HOLD_NOT_FOUND"
E_HOLD_NOT_ACTIVE = "E_HOLD_NOT_ACTIVE"
E_NOT_DUE = "E_NOT_DUE"
E_ONLY_LONGER = "E_ONLY_LONGER"
E_ABOVE_MAX_HOLD = "E_ABOVE_MAX_HOLD"
E_EVIDENCE_TOO_LONG = "E_EVIDENCE_TOO_LONG"
E_NO_REASON = "E_NO_REASON"
E_INSUFFICIENT_BALANCE = "E_INSUFFICIENT_BALANCE"
E_UNKNOWN_ASSET = "E_UNKNOWN_ASSET"
E_ZERO_AMOUNT = "E_ZERO_AMOUNT"
E_SELF_PAYMENT = "E_SELF_PAYMENT"
E_TOO_MANY_HOLDS = "E_TOO_MANY_HOLDS"
E_FEE_TOO_LOW = "E_FEE_TOO_LOW"
E_INSOLVENT = "E_INSOLVENT"
E_TOO_MANY_TRUSTED = "E_TOO_MANY_TRUSTED"
E_BAD_GUARDIAN = "E_BAD_GUARDIAN"
E_BAD_CONFIG = "E_BAD_CONFIG"
E_MEMO_TOO_LONG = "E_MEMO_TOO_LONG"
E_BAD_MSG = "E_BAD_MSG"  # ft_on_transfer msg is neither "" nor {"credit_to": ...}
E_OVERFLOW = "E_OVERFLOW"  # checked arithmetic failed (unreachable with real balances)

CONTRACT_CODES = frozenset(
    {
        E_NOT_REGISTERED,
        E_NOT_SENDER,
        E_NOT_GUARDIAN,
        E_NOT_ORACLE,
        E_NOT_TOKEN,
        E_HOLD_NOT_FOUND,
        E_HOLD_NOT_ACTIVE,
        E_NOT_DUE,
        E_ONLY_LONGER,
        E_ABOVE_MAX_HOLD,
        E_EVIDENCE_TOO_LONG,
        E_NO_REASON,
        E_INSUFFICIENT_BALANCE,
        E_UNKNOWN_ASSET,
        E_ZERO_AMOUNT,
        E_SELF_PAYMENT,
        E_TOO_MANY_HOLDS,
        E_FEE_TOO_LOW,
        E_INSOLVENT,
        E_TOO_MANY_TRUSTED,
        E_BAD_GUARDIAN,
        E_BAD_CONFIG,
        E_MEMO_TOO_LONG,
        E_BAD_MSG,
        E_OVERFLOW,
    }
)

# Codes raised on the Python side when there is no contract panic to map.
E_BAD_ARGS = "E_BAD_ARGS"  # arguments the contract cannot deserialize (e.g. an invalid account id)
E_TX_REJECTED = "E_TX_REJECTED"  # the node refused the transaction (access key, nonce, balance)
E_TX_FAILED = "E_TX_FAILED"  # the transaction failed without a HoldVault error code
E_RPC = "E_RPC"  # the RPC node could not be reached or returned an unexpected error
E_NOT_CONFIGURED = "E_NOT_CONFIGURED"  # a signing key or account is missing from the settings

# Codes the engine uses for refusals that never reach the chain.
UNKNOWN_PAYEE = "UNKNOWN_PAYEE"
INVALID_PAYEE = "INVALID_PAYEE"
INVALID_INVITE = "INVALID_INVITE"

_PANIC_MARKER = "Smart contract panicked:"
_PANIC_PREFIXES = ("Contract panicked:", _PANIC_MARKER)
_LEADING_CODE = re.compile(r"(E_[A-Z][A-Z0-9_]*)\b:?\s*(.*)", re.DOTALL)
_ANY_CODE = re.compile(r"\b(E_[A-Z][A-Z0-9_]*)\b")


class VaultError(Exception):
    """The vault (or the node in front of it) refused a call."""

    def __init__(
        self, code: str, message: str = "", *, tx_hash: str | None = None, data: Any = None
    ) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.tx_hash = tx_hash
        self.data = data


def parse_panic(text: str) -> tuple[str, str] | None:
    """Return ``(code, message)`` for a HoldVault panic, or None if there is no code.

    Accepts the raw ``ExecutionError`` string (``"Smart contract panicked:
    E_ONLY_LONGER: ..."``) with or without the runtime prefixes. A code that
    does not lead the message is only accepted when it is a known contract
    code, so unrelated text cannot masquerade as one.
    """
    body = text.strip()
    stripped = True
    while stripped:
        stripped = False
        for prefix in _PANIC_PREFIXES:
            if body.startswith(prefix):
                body = body[len(prefix) :].strip()
                stripped = True
    if match := _LEADING_CODE.match(body):
        return match.group(1), match.group(2).strip()
    for match in _ANY_CODE.finditer(text):
        if match.group(1) in CONTRACT_CODES:
            return match.group(1), text[match.end() :].lstrip(": ").strip().rstrip("\")")
    return None


def panic_in(text: str) -> tuple[str, str] | None:
    """Find a HoldVault panic inside a longer node message, e.g. a view's ``vm_error``."""
    start = text.find(_PANIC_MARKER)
    if start < 0:
        return None
    body = text[start + len(_PANIC_MARKER) :]
    for end in ('\\"', '")', "\n"):
        if end in body:
            body = body[: body.index(end)]
    return parse_panic(body)


def error_path(error: Any) -> list[str]:
    """Variant names along nearcore's nested error JSON, outermost first.

    Accepts the JSON-RPC ``error`` object (``{"name", "cause", "data", ...}``)
    or its ``data`` part, e.g. ``{"TxExecutionError": {"InvalidTxError":
    {"InvalidAccessKeyError": {"MethodNameMismatch": {"method_name": "send"}}}}}``.
    """
    node = error
    if isinstance(node, dict) and "name" in node and ("cause" in node or "data" in node):
        data = node.get("data")
        info = (node.get("cause") or {}).get("info")
        node = data if isinstance(data, dict) and data else info if info else data
    path: list[str] = []
    while isinstance(node, dict):
        variants = [key for key in node if key[:1].isupper()]
        if len(variants) != 1:
            break
        path.append(variants[0])
        node = node[variants[0]]
    if isinstance(node, str) and node[:1].isupper() and node.isidentifier():
        path.append(node)
    return path


def rejection_name(error: Any) -> str:
    """The innermost nearcore error name, e.g. ``MethodNameMismatch``; "unknown" if none."""
    path = error_path(error)
    if path:
        return path[-1]
    match = _KNOWN_REJECTION.search(str(error))
    return match.group(0) if match else "unknown"


KNOWN_REJECTIONS = (
    "MethodNameMismatch",
    "RequiresFullAccess",
    "ReceiverMismatch",
    "DepositWithFunctionCall",
    "AccessKeyNotFound",
    "NotEnoughAllowance",
)
_KNOWN_REJECTION = re.compile("|".join(KNOWN_REJECTIONS))
