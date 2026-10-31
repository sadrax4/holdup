"""Deterministic user accounts and the key permissions HoldUp hands out.

A channel identity such as ``tg:123456`` maps to a NEAR sub-account and an
ed25519 key through HMAC-SHA256 under the server secret. The account name
reveals nothing about the Telegram id, and every account can be re-derived
after a restart, so the local database never holds the only copy of a key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from near import Ed25519KeyPair, is_valid_account_id

from ..models import UserAccount

NAME_CHARS = 10

# Methods each restricted key may call on the vault (allowance: unlimited, zero deposit). A user
# who is someone's guardian uses the same key for guardian_cancel and veto_guardian_change.
USER_METHODS = (
    "send",
    "cancel",
    "guardian_cancel",
    "add_trusted",
    "remove_trusted",
    "set_guardian",
    "veto_guardian_change",
)
ORACLE_METHODS = ("extend_hold",)
KEEPER_METHODS = ("release",)


def _mac(secret: str, label: str, user_key: str) -> bytes:
    return hmac.new(secret.encode(), (label + user_key).encode(), hashlib.sha256).digest()


def account_label(secret: str, user_key: str) -> str:
    """``u`` + 10 lowercase base32 characters of HMAC(secret, "holdup:name:" + user_key)."""
    digest = _mac(secret, "holdup:name:", user_key)
    return "u" + base64.b32encode(digest).decode("ascii").lower()[:NAME_CHARS]


def derive_user(secret: str, user_key: str, root: str) -> UserAccount:
    """Derive the sub-account and signing key for one channel identity."""
    if not secret:
        raise ValueError("a server secret is required to derive user accounts")
    if not user_key:
        raise ValueError("user_key must not be empty")
    account_id = f"{account_label(secret, user_key)}.{root}"
    if not is_valid_account_id(account_id):
        raise ValueError(f"root account {root!r} does not yield valid user account ids")
    seed = _mac(secret, "holdup:key:", user_key)[:32]
    pair = Ed25519KeyPair(seed)
    return UserAccount(
        user_key=user_key,
        account_id=account_id,
        public_key=str(pair.public_key),
        private_key=pair.secret_key,
    )
