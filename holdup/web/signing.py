"""Signed CSV export links and web console identities.

Export links name the payer's vault account (public on-chain anyway), an
expiry and an HMAC, so they cannot be forged or used after they expire, and
they reveal no chat id or cookie. A console identity is a random token kept
in an HttpOnly cookie; the user key the engine sees is a keyed hash of it,
so nothing stored server-side can be replayed as the cookie.
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
_TOKEN = re.compile(r"[A-Za-z0-9_-]{24,64}")
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


def new_console_token() -> str:
    return secrets.token_urlsafe(24)


def new_console_identity(secret: str) -> tuple[str, str]:
    """A fresh cookie token and the user key it stands for."""
    token = new_console_token()
    return token, _console_key(secret, token)


def console_user_key(secret: str, token: str | None) -> str | None:
    """``web:<keyed hash>`` for a well-formed cookie token, else None."""
    if not token or not _TOKEN.fullmatch(token):
        return None
    return _console_key(secret, token)


def _console_key(secret: str, token: str) -> str:
    digest = hmac.new(_key(secret, "console"), token.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{CONSOLE_PREFIX}:{digest}"
