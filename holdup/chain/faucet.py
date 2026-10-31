"""The NEAR testnet faucet API, for the operator's scripts and the running service.

Documented at https://docs.near.org/getting-started/faucet: ``POST`` a JSON body
``{"accountId": "<account>"}`` and the faucet sends 5 testnet NEAR to that account, which must be
an existing testnet account or a 64-character implicit account. No API key. It answers 200 with
``{"success": true, "message": ...}``, 400 for a missing or bad account id and 500 when funding
failed, both with an ``error`` field. A request that times out may still have funded the
account, so callers re-read the balance instead of trusting the answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from .budget import YOCTO_PER_NEAR

FAUCET_URL = "https://docs-mcp-f18b.onrender.com/api/faucet"
FAUCET_DROP = 5 * YOCTO_PER_NEAR
FAUCET_TIMEOUT_S = 120.0  # the service may first have to wake up

_NAMED_TESTNET = re.compile(r"^(?:[a-z\d]+[-_])*[a-z\d]+(?:\.(?:[a-z\d]+[-_])*[a-z\d]+)*\.testnet$")
_IMPLICIT = re.compile(r"^[0-9a-f]{64}$")


def faucet_account_ok(account_id: str) -> bool:
    """Whether the faucet can fund ``account_id``: a ``.testnet`` account or a 64-hex implicit one."""
    return len(account_id) <= 64 and bool(_NAMED_TESTNET.match(account_id) or _IMPLICIT.match(account_id))


@dataclass(frozen=True)
class FaucetReply:
    ok: bool  # the faucet said it funded the account
    status: int | None  # HTTP status; None when there was no answer (timeout, connection error)
    message: str  # the faucet's message or error, shortened

    @property
    def maybe_funded(self) -> bool:
        """True unless the faucet clearly refused: a timeout may still have funded the account."""
        return self.ok or self.status is None or self.status >= 500


def _reply(response: httpx.Response) -> FaucetReply:
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    text = str(body.get("message") or body.get("error") or response.text or "")[:160]
    ok = response.status_code == 200 and bool(body.get("success", True))
    return FaucetReply(ok, response.status_code, text)


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
