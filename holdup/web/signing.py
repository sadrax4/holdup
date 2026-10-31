"""Signed CSV export links and web console identities.

Export links name the payer's vault account (public on-chain anyway), an
expiry and an HMAC, so they cannot be forged or used after they expire, and
they reveal no chat id or cookie. A console identity is a random nonce plus
its HMAC, kept in an HttpOnly cookie: only this server can mint one, so a
client cannot invent identities (each of which would open a funded account).
The user key the engine sees is a keyed hash of the nonce, so nothing stored
server-side can be replayed as the cookie.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets

EXPORT_TTL_MS = 15 * 60_000
CONSOLE_COOKIE = "holdup_console"
CONSOLE_PREFIX = "web"
_NONCE = re.compile(r"[A-Za-z0-9_-]{32}")  # secrets.token_urlsafe(24)
_SIGNATURE_BYTES = 16


def _key(secret: str, purpose: str) -> bytes:
    return hmac.new(secret.encode(), f"holdup:{purpose}".encode(), hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(secret: str, body: str) -> str:
    digest = hmac.new(_key(secret, "export"), body.encode(), hashlib.sha256).digest()
    return _b64(digest[:_SIGNATURE_BYTES])


def export_token(secret: str, account_id: str, expires_ms: int) -> str:
    """``<account>.<expiry>.<signature>``, URL-safe."""
    body = f"{_b64(account_id.encode())}.{int(expires_ms)}"
    return f"{body}.{_signature(secret, body)}"


def read_export_token(secret: str, token: str, now_ms: int) -> str | None:
    """The account an export token was issued for, or None if it is forged, malformed or expired."""
    parts = token.split(".")
    if len(parts) != 3 or not parts[1].isdigit():
        return None
    body = f"{parts[0]}.{parts[1]}"
    if not hmac.compare_digest(parts[2], _signature(secret, body)) or int(parts[1]) < now_ms:
        return None
    try:
        return _unb64(parts[0]).decode()
    except (binascii.Error, ValueError):
        return None


def new_console_token(secret: str) -> str:
    """``<nonce>.<mac>``: a random nonce signed by this server."""
    nonce = secrets.token_urlsafe(24)
    return f"{nonce}.{_cookie_mac(secret, nonce)}"


def new_console_identity(secret: str) -> tuple[str, str]:
    """A fresh cookie token and the user key it stands for."""
    token = new_console_token(secret)
    return token, _console_key(secret, token.rpartition(".")[0])


def console_user_key(secret: str, token: str | None) -> str | None:
    """``web:<keyed hash>`` for a cookie token this server issued, else None."""
    nonce, _, mac = (token or "").rpartition(".")
    if not _NONCE.fullmatch(nonce) or not hmac.compare_digest(mac.encode(), _cookie_mac(secret, nonce).encode()):
        return None
    return _console_key(secret, nonce)


def _console_key(secret: str, nonce: str) -> str:
    digest = hmac.new(_key(secret, "console"), nonce.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{CONSOLE_PREFIX}:{digest}"


def _cookie_mac(secret: str, nonce: str) -> str:
    digest = hmac.new(_key(secret, "console-cookie"), nonce.encode(), hashlib.sha256).digest()
    return _b64(digest[:_SIGNATURE_BYTES])
