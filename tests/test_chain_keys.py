"""Derived user accounts are stable, valid and reveal nothing about the channel id."""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest
from near import KeyType, PublicKey, is_valid_account_id, parse_key

from holdup.chain.keys import derive_user

SECRET = "test-secret-please-ignore"
ROOT = "holdup.testnet"


def test_account_name_is_hmac_derived_base32() -> None:
    user = derive_user(SECRET, "tg:123456", ROOT)
    digest = hmac.new(SECRET.encode(), b"holdup:name:tg:123456", hashlib.sha256).digest()
    expected = "u" + base64.b32encode(digest).decode().lower()[:10]
    assert user.account_id == f"{expected}.{ROOT}"
    assert is_valid_account_id(user.account_id)
    assert len(user.account_id.split(".")[0]) == 11


def test_derivation_is_stable_and_distinct() -> None:
    first = derive_user(SECRET, "tg:42", ROOT)
    assert derive_user(SECRET, "tg:42", ROOT) == first
    assert derive_user(SECRET, "tg:43", ROOT).account_id != first.account_id
    assert derive_user("another-secret", "tg:42", ROOT).account_id != first.account_id
    assert derive_user(SECRET, "web:42", ROOT).account_id != first.account_id


def test_key_comes_from_the_hmac_seed_and_matches_the_public_key() -> None:
    user = derive_user(SECRET, "tg:42", ROOT)
    pair = parse_key(user.private_key)
    assert str(pair.public_key) == user.public_key
    assert PublicKey.parse(user.public_key).key_type == KeyType.ED25519
    seed = hmac.new(SECRET.encode(), b"holdup:key:tg:42", hashlib.sha256).digest()[:32]
    message = b"holdup"
    assert pair.sign(message) == type(pair)(seed).sign(message)


@pytest.mark.parametrize("telegram_id", ["123456789", "100000000001", "987654321098"])
def test_telegram_ids_never_appear_in_the_account(telegram_id: str) -> None:
    user = derive_user(SECRET, f"tg:{telegram_id}", ROOT)
    assert telegram_id not in user.account_id
    assert telegram_id not in user.public_key
    assert "tg" not in user.account_id.split(".")[0]


def test_derivation_refuses_bad_inputs() -> None:
    with pytest.raises(ValueError):
        derive_user("", "tg:1", ROOT)
    with pytest.raises(ValueError):
        derive_user(SECRET, "", ROOT)
    with pytest.raises(ValueError):
        derive_user(SECRET, "tg:1", "Not_A_Valid.Root")
