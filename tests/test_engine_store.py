"""The local store: users, one-time invites, evidence, verdicts, logs and encrypted key secrets."""

from __future__ import annotations

from pathlib import Path

import pytest

from holdup.chain.fake import SimClock
from holdup.store import INVITE_TTL_MS, Store


@pytest.fixture
def clock() -> SimClock:
    return SimClock()


@pytest.fixture
def store(clock: SimClock) -> Store:
    return Store(":memory:", secret="store-secret", clock=clock)


def test_users_are_upserted_without_losing_details(store: Store) -> None:
    store.upsert_user("tg:1", "u1.holdup.testnet", "chat-1", "Priya")
    store.upsert_user("tg:1", "u1.holdup.testnet", None, "")
    user = store.get_user("tg:1")
    assert user is not None and (user["chat_ref"], user["display_name"]) == ("chat-1", "Priya")
    store.upsert_user("tg:1", "u1.holdup.testnet", "chat-2", "Priya K")
    assert store.user_by_account("u1.holdup.testnet")["display_name"] == "Priya K"  # type: ignore[index]
    assert store.get_user("tg:404") is None and store.count_users() == 1


def test_invites_are_single_use_and_expire(store: Store, clock: SimClock) -> None:
    code = store.create_invite("tg:1")
    assert len(code) >= 16
    assert store.consume_invite(code) == "tg:1"
    assert store.consume_invite(code) is None
    late = store.create_invite("tg:1")
    clock.advance(INVITE_TTL_MS)
    assert store.consume_invite(late) is None
    assert store.consume_invite("made-up") is None


def test_evidence_and_verdict_history(store: Store) -> None:
    store.save_evidence(7, '{"v":1}', "aa")
    store.save_evidence(7, '{"v":1,"n":2}', "bb")
    latest = store.get_evidence(7)
    assert latest == ('{"v":1,"n":2}', "bb")
    json_text, digest = latest  # type: ignore[misc]
    assert latest.evidence_hash == digest == "bb"  # type: ignore[union-attr]
    assert [e.evidence_hash for e in store.list_evidence(7)] == ["aa", "bb"]
    assert store.evidence_by_hash("aa") == (7, '{"v":1}')
    store.save_verdict(7, {"kind": "provisional"})
    store.save_verdict(7, {"kind": "final", "steps": [{"cost_usd": 0.1}]})
    assert store.get_verdict(7) == {"kind": "final", "steps": [{"cost_usd": 0.1}]}
    assert [v["kind"] for v in store.list_verdicts(7)] == ["provisional", "final"]
    assert len(store.list_verdicts()) == 2 and store.get_verdict(8) is None


def test_transactions_mcp_calls_contexts_and_events(store: Store, clock: SimClock) -> None:
    store.record_tx(7, "send", "h1")
    store.record_tx(7, "extend", "h2")
    store.record_tx(None, "trust", "h3")
    assert store.get_txs(7) == [("send", "h1"), ("extend", "h2")]
    store.record_mcp_call(7, "payee_profile", {"account_id": "x.testnet"})
    assert store.get_mcp_calls(7) == [{"tool": "payee_profile", "args": {"account_id": "x.testnet"}, "created_ms": clock()}]
    store.save_context(7, {"has_text": True, "preview": "hi"})
    assert store.get_context(7) == {"has_text": True, "preview": "hi"}
    store.log_event("brake", {"hold_id": 7})
    store.log_event("brake", {"hold_id": 8})
    store.log_event("cancel", {"hold_id": 7})
    assert store.count_events("brake") == 2
    assert [e["data"]["hold_id"] for e in store.events("brake")] == [8, 7]
    assert len(store.events(limit=1)) == 1


def test_otari_key_secrets_are_encrypted_at_rest(tmp_path: Path) -> None:
    path = tmp_path / "holdup.sqlite3"
    store = Store(str(path), secret="store-secret")
    store.save_otari_key("tg:1", "key_123", "otk_v1_super_secret_value")
    assert store.get_otari_key("tg:1") == ("key_123", "otk_v1_super_secret_value")
    store.close()
    assert b"otk_v1_super_secret_value" not in path.read_bytes()

    reopened = Store(str(path), secret="store-secret")
    assert reopened.get_otari_key("tg:1") == ("key_123", "otk_v1_super_secret_value")
    assert Store(str(path), secret="wrong-secret").get_otari_key("tg:1") == ("key_123", None)
    assert Store(str(path)).get_otari_key("tg:1") == ("key_123", None)


def test_without_a_secret_only_key_ids_are_kept() -> None:
    store = Store(":memory:")
    store.save_otari_key("tg:1", "key_123")
    assert store.get_otari_key("tg:1") == ("key_123", None)
    with pytest.raises(RuntimeError):
        store.save_otari_key("tg:1", "key_123", "secret")


def test_data_survives_a_restart(tmp_path: Path) -> None:
    path = str(tmp_path / "nested" / "holdup.sqlite3")
    store = Store(path)
    store.upsert_user("tg:1", "u1.holdup.testnet", None, "Priya")
    store.save_evidence(1, "{}", "aa")
    store.close()
    again = Store(path)
    assert again.get_user("tg:1")["display_name"] == "Priya"  # type: ignore[index]
    assert again.get_evidence(1) == ("{}", "aa")
