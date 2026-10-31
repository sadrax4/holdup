"""The NEAR testnet faucet API, for the operator's scripts and the running service.

Documented at https://docs.near.org/getting-started/faucet: ``POST`` a JSON body
``{"accountId": "<account>"}`` and the faucet sends 5 testnet NEAR to that account, which must be
an existing testnet account or a 64-character implicit account. No API key. It answers 200 with
``{"success": true, "message": ...}``, 400 for a missing or bad account id and 500 when funding
failed, both with an ``error`` field. A request that times out may still have funded the
account, so callers re-read the balance instead of trusting the answer.

The faucet is rate limited although its documentation does not say so. On 2026-10-31, after six
drops in five minutes, it answered every request with HTTP 500 and ``Failed to create
<account>-<ms>.testnet: 429 Rate limit exceeded, retry in 10 hours.`` Such an answer funded
nothing, and asking again before that time only adds requests to the count, so callers stop
asking until then (:attr:`FaucetReply.rate_limited`, :attr:`FaucetReply.retry_after_s`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from .budget import YOCTO_PER_NEAR

FAUCET_URL = "https://docs-mcp-f18b.onrender.com/api/faucet"
FAUCET_DROP = 5 * YOCTO_PER_NEAR
FAUCET_TIMEOUT_S = 120.0  # the service may first have to wake up
RATE_LIMIT_WAIT_S = 3600.0  # how long to leave a rate-limited faucet alone when it does not say

_NAMED_TESTNET = re.compile(r"^(?:[a-z\d]+[-_])*[a-z\d]+(?:\.(?:[a-z\d]+[-_])*[a-z\d]+)*\.testnet$")
_IMPLICIT = re.compile(r"^[0-9a-f]{64}$")
_RETRY_IN = re.compile(r"retry (?:in|after) (\d+(?:\.\d+)?) ?(second|minute|hour|day)s?\b", re.IGNORECASE)
_UNIT_S = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


def faucet_account_ok(account_id: str) -> bool:
    """Whether the faucet can fund ``account_id``: a ``.testnet`` account or a 64-hex implicit one."""
    return len(account_id) <= 64 and bool(_NAMED_TESTNET.match(account_id) or _IMPLICIT.match(account_id))


@dataclass(frozen=True)
class FaucetReply:
    ok: bool  # the faucet said it funded the account
    status: int | None  # HTTP status; None when there was no answer (timeout, connection error)
    message: str  # the faucet's message or error, shortened
    rate_limited: bool = False  # refused because of a rate limit (its own or that of the service behind it)
    retry_after_s: float | None = None  # how long it asked us to wait, when it said

    @property
    def maybe_funded(self) -> bool:
        """True unless the faucet clearly refused: a timeout may still have funded the account."""
        return self.ok or (not self.rate_limited and (self.status is None or self.status >= 500))

    @property
    def wait_s(self) -> float:
        """How long to leave the faucet alone after this answer: what it asked for, else an hour."""
        return self.retry_after_s if self.retry_after_s is not None else RATE_LIMIT_WAIT_S


def retry_after_s(response: httpx.Response, text: str) -> float | None:
    """The wait a refusal names: a ``Retry-After`` header in seconds, or "retry in 10 hours" in its text."""
    header = response.headers.get("retry-after", "").strip()
    if header.isdigit():
        return float(header)
    match = _RETRY_IN.search(text)
    if match is None:
        return None
    return float(match.group(1)) * _UNIT_S[match.group(2).lower()]


def retry_hint(reply: FaucetReply, now: datetime | None = None) -> str:
    """When to ask a rate-limited faucet again, for people: ``after 06:05 UTC on 2026-11-01``."""
    if reply.retry_after_s is None:
        return "later (it did not say when)"
    when = (now or datetime.now(UTC)) + timedelta(seconds=reply.retry_after_s)
    return f"after {when:%H:%M} UTC on {when:%Y-%m-%d}"


def _reply(response: httpx.Response) -> FaucetReply:
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    text = str(body.get("message") or body.get("error") or response.text or "")
    ok = response.status_code == 200 and bool(body.get("success", True))
    limited = not ok and (response.status_code == 429 or "rate limit" in text.lower())
    wait = retry_after_s(response, text) if limited else None
    return FaucetReply(ok, response.status_code, text[:160], limited, wait)


def _check(account_id: str) -> None:
    if not faucet_account_ok(account_id):
        raise ValueError(f"the faucet only funds .testnet or 64-hex implicit accounts (got {account_id!r})")


def request_drop(http: httpx.Client, account_id: str, *, url: str = FAUCET_URL) -> FaucetReply:
    """Ask the faucet for 5 NEAR for ``account_id``; never raises for HTTP trouble."""
    _check(account_id)
    try:
        response = http.post(url, json={"accountId": account_id}, timeout=FAUCET_TIMEOUT_S)
    except httpx.HTTPError as exc:
        return FaucetReply(False, None, f"no answer: {type(exc).__name__}")
    return _reply(response)


async def request_drop_async(http: httpx.AsyncClient, account_id: str, *, url: str = FAUCET_URL) -> FaucetReply:
    """The same as :func:`request_drop`, for asyncio code."""
    _check(account_id)
    try:
        response = await http.post(url, json={"accountId": account_id}, timeout=FAUCET_TIMEOUT_S)
    except httpx.HTTPError as exc:
        return FaucetReply(False, None, f"no answer: {type(exc).__name__}")
    return _reply(response)
