"""Signed export links and console identities."""

from __future__ import annotations

import re

import pytest

from holdup.web.signing import (
    console_user_key,
    export_token,
    new_console_identity,
    new_console_token,
    read_export_token,
)

SECRET = "server-secret"
ACCOUNT = "u3kq7x2m9aa.holdup.testnet"


def test_export_token_round_trip() -> None:
    token = export_token(SECRET, ACCOUNT, 2_000)
    assert re.fullmatch(r"[A-Za-z0-9_.-]+", token)
    assert read_export_token(SECRET, token, now_ms=1_999) == ACCOUNT
    assert read_export_token(SECRET, token, now_ms=2_000) == ACCOUNT


def test_export_token_expires() -> None:
    assert read_export_token(SECRET, export_token(SECRET, ACCOUNT, 2_000), now_ms=2_001) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t.replace(t.split(".")[0], export_token(SECRET, "evil.testnet", 2_000).split(".")[0]),
        lambda t: t.replace(".2000.", ".9999999999999."),
        lambda t: t[:-2] + ("AA" if not t.endswith("AA") else "BB"),
        lambda t: t + ".extra",
        lambda t: "not-a-token",
    ],
)
def test_export_token_cannot_be_forged(mutate: object) -> None:
    token = export_token(SECRET, ACCOUNT, 2_000)
    assert read_export_token(SECRET, mutate(token), now_ms=0) is None  # type: ignore[operator]


def test_export_token_is_bound_to_the_secret() -> None:
    assert read_export_token("other-secret", export_token(SECRET, ACCOUNT, 2_000), now_ms=0) is None


def test_a_new_identity_matches_its_cookie() -> None:
    token, key = new_console_identity(SECRET)
    assert console_user_key(SECRET, token) == key


def test_console_user_keys_hide_the_cookie() -> None:
    token = new_console_token(SECRET)
    key = console_user_key(SECRET, token)
    assert key is not None and re.fullmatch(r"web:[0-9a-f]{24}", key)
    assert token not in key and token.split(".")[0] not in key
    assert console_user_key(SECRET, token) == key
    assert console_user_key(SECRET, new_console_token(SECRET)) != key


def test_only_this_server_can_mint_identities() -> None:
    """Any well-formed token used to be an identity, so a client could invent unlimited accounts."""
    token = new_console_token(SECRET)
    nonce, mac = token.split(".")
    assert console_user_key("other-secret", token) is None
    assert console_user_key(SECRET, nonce) is None  # an unsigned nonce, the old cookie format
    tampered = mac[:-1] + ("B" if mac.endswith("A") else "A")
    assert console_user_key(SECRET, f"{nonce}.{tampered}") is None
    other_nonce = new_console_token(SECRET).split(".")[0]
    assert console_user_key(SECRET, f"{other_nonce}.{mac}") is None


@pytest.mark.parametrize(
    "token",
    [None, "", "short", "has spaces in it, which is wrong!!", "x" * 65, "x" * 32 + ".", ".abc", "x" * 32 + ".\u00e9"],
)
def test_malformed_cookies_have_no_identity(token: str | None) -> None:
    assert console_user_key(SECRET, token) is None
