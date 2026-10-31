"""The web console: identity, limits, the chat flow and the notifier behind it."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_bot_fakes import FakeEngine, RecordingNotifier, make_settings

from holdup.bot.notify import RoutingNotifier
from holdup.bot.samples import load_samples
from holdup.chain.fake import FakeVault
from holdup.store import Store
from holdup.web.app import create_app
from holdup.web.console import (
    ConsoleNotifier,
    attach_console,
    find_console,
    new_accounts_per_hour,
    story_chips,
    tone_for,
)
from holdup.web.signing import CONSOLE_COOKIE, new_console_identity


@dataclass
class Console:
    client: TestClient
    engine: FakeEngine
    store: Store

    def start(self, **body: Any) -> dict[str, Any]:
        response = self.client.post("/console/api/start", json=body)
        assert response.status_code == 200, response.text
        return response.json()

    def send(self, text: str, after: int = 0) -> dict[str, Any]:
        response = self.client.post("/console/api/send", json={"text": text, "after": after})
        assert response.status_code == 200, response.text
        return response.json()

    def act(self, data: str, label: str = "", after: int = 0) -> dict[str, Any]:
        response = self.client.post("/console/api/action", json={"data": data, "label": label, "after": after})
        assert response.status_code == 200, response.text
        return response.json()

    @property
    def user_key(self) -> str:
        return self.engine.called("start")[0][0]


@pytest.fixture
def console() -> Iterator[Console]:
    settings = make_settings()
    store = Store(":memory:")
    engine = FakeEngine(settings, store=store)
    app = create_app(settings, engine, FakeVault.from_settings(settings), store)
    with TestClient(app) as client:
        yield Console(client, engine, store)


def last(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["messages"][-1]


# ----------------------------------------------------------------------
# the notifier
# ----------------------------------------------------------------------


async def test_console_notifier_keeps_each_conversation() -> None:
    notifier = ConsoleNotifier(max_messages=3, clock=lambda: 42)
    await notifier.notify("web:a", "Brakes on - held until 10:00 UTC", [[("Why?", "why:1")]])
    notifier.post("web:b", "me", "hello")
    notifier.post("web:a", "me", "/balance")
    [first, second] = notifier.since("web:a")
    assert (first.seq, second.seq) == (1, 3)
    assert first.to_json() == {
        "seq": 1,
        "role": "bot",
        "text": "Brakes on - held until 10:00 UTC",
        "buttons": [[{"label": "Why?", "data": "why:1"}]],
        "ts": 42,
        "title": "",
        "tone": "brake",
    }
    assert [m.text for m in notifier.since("web:a", after=1)] == ["/balance"]
    for n in range(5):
        notifier.post("web:a", "me", str(n))
    assert [m.text for m in notifier.since("web:a")] == ["2", "3", "4"]


def test_console_notifier_forgets_the_oldest_conversations() -> None:
    notifier = ConsoleNotifier(max_users=2)
    for key in ("web:a", "web:b", "web:c"):
        notifier.post(key, "me", "hi")
    assert notifier.since("web:a") == []
    assert len(notifier.since("web:c")) == 1


@pytest.mark.parametrize(
    ("text", "tone"),
    [
        ("Brakes on - held until 10:00 UTC", "brake"),
        ("Guardian alert: Priya's payment", "brake"),
        ("Released: 5 NEAR paid to ravi.testnet.", "ok"),
        ("Payment held: 5 NEAR to ravi.testnet.", "held"),
        ("Welcome to HoldUp.", ""),
    ],
)
def test_tone_for(text: str, tone: str) -> None:
    assert tone_for(text) == tone


async def test_attach_console_routes_web_users_and_keeps_the_rest() -> None:
    engine = FakeEngine()
    original = engine.notifier
    console = attach_console(engine)
    assert isinstance(engine.notifier, RoutingNotifier)
    await engine.notifier.notify("web:x", "for the console")
    await engine.notifier.notify("tg:1", "for telegram")
    assert [m.text for m in console.since("web:x")] == ["for the console"]
    assert original.sent == [("tg:1", "for telegram", None)]
    assert attach_console(engine) is console  # idempotent


def test_find_console_through_a_router() -> None:
    console = ConsoleNotifier()
    assert find_console(RoutingNotifier({"web": console, "tg": RecordingNotifier()})) is console
    assert find_console(console) is console
    assert find_console(RecordingNotifier()) is None


# ----------------------------------------------------------------------
# identity and limits
# ----------------------------------------------------------------------


def test_the_page_is_a_labelled_web_console(console: Console) -> None:
    page = console.client.get("/console")
    assert page.status_code == 200
    assert "Web console" in page.text
    assert "Drives the same HoldUp engine as the Telegram bot" in page.text
    assert 'data-action="sample:hero"' in page.text
    assert 'data-send="send 50 HUSD to safe-wallet-77.testnet"' in page.text
    assert page.headers["x-robots-tag"] == "noindex"


def test_state_before_starting(console: Console) -> None:
    assert console.client.get("/console/api/state").json() == {"started": False, "messages": [], "account": None}
    assert console.client.post("/console/api/send", json={"text": "/balance"}).status_code == 401
    assert console.engine.calls == []


def test_start_sets_a_private_cookie_identity(console: Console) -> None:
    response = console.client.post("/console/api/start", json={"name": "Priya\u202e\x07"})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{CONSOLE_COOKIE}=")
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie
    token = response.cookies[CONSOLE_COOKIE]
    [(user_key, name, chat_ref)] = console.engine.called("start")
    assert user_key.startswith("web:") and token not in user_key
    assert (name, chat_ref) == ("Priya", None)
    body = response.json()
    assert body["started"] is True
    assert last(body)["text"].startswith("Welcome to HoldUp, Priya.")
    assert body["account"] == {
        "id": console.engine.account_for(user_key).account_id,
        "balances": ["2 NEAR", "2000 HUSD"],
    }

    again = console.client.post("/console/api/start", json={})
    assert "set-cookie" not in again.headers  # the same identity is reused
    assert {args[0] for args in console.engine.called("start")} == {user_key}


def test_new_identities_are_rate_limited_per_client(console: Console) -> None:
    for _ in range(5):
        console.client.cookies.clear()
        assert console.client.post("/console/api/start", json={}).status_code == 200
    console.client.cookies.clear()
    refused = console.client.post("/console/api/start", json={})
    assert refused.status_code == 429
    assert "too many new console accounts" in refused.json()["error"]


def test_the_dinner_payment_chip_lets_the_dialog_fill_in_the_persons_friend() -> None:
    chips = story_chips(load_samples(make_settings(network="testnet", root_account="holdup.testnet")))
    [dinner] = [chip for chip in chips if chip.action == "pay:benign"]
    assert dinner.label == "send 12.5 HUSD to Ravi" and not dinner.send
    assert any(chip.send == "send 50 HUSD to safe-wallet-77.holdup.testnet" for chip in chips)


def test_new_accounts_overall_stay_within_what_the_faucet_refills() -> None:
    assert new_accounts_per_hour(make_settings(network="testnet")) == 20  # about 5.2 test NEAR an hour
    assert new_accounts_per_hour(make_settings(network="sandbox")) == 20
    assert new_accounts_per_hour(make_settings()) == 200  # offline: free


def test_made_up_cookies_cannot_open_accounts(console: Console) -> None:
    """The review's attack: a fresh self-made cookie per request skipped the limits and funded a new account."""
    for _ in range(5):
        console.client.cookies.clear()
        console.start()
    started = len(console.engine.called("start"))
    for n in range(12):
        console.client.cookies.clear()
        console.client.cookies.set(CONSOLE_COOKIE, secrets.token_urlsafe(24 + n))
        assert console.client.post("/console/api/start", json={}).status_code == 429
        assert console.client.post("/console/api/send", json={"text": "/balance"}).status_code == 401
        assert console.client.post("/console/api/action", json={"data": "why:1"}).status_code == 401
        assert console.client.get("/console/api/state").json()["started"] is False
    assert len(console.engine.called("start")) == started


def test_a_signed_cookie_works_only_for_an_account_that_was_opened(console: Console) -> None:
    token, key = new_console_identity("test-secret")  # make_settings() signs with this secret
    console.client.cookies.set(CONSOLE_COOKIE, token)
    assert console.client.post("/console/api/send", json={"text": "/balance"}).status_code == 401
    assert console.engine.calls == []
    # /start opens it without minting another identity.
    response = console.client.post("/console/api/start", json={})
    assert response.status_code == 200 and "set-cookie" not in response.headers
    assert console.engine.called("start")[0][0] == key
    assert console.client.post("/console/api/send", json={"text": "/balance"}).status_code == 200


def test_cross_site_posts_are_refused(console: Console) -> None:
    console.start()
    response = console.client.post(
        "/console/api/send", json={"text": "/balance"}, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403
    same_site = console.client.post(
        "/console/api/send", json={"text": "/balance"}, headers={"Origin": "http://testserver"}
    )
    assert same_site.status_code == 200


def test_forms_cannot_post_to_the_api(console: Console) -> None:
    console.start()
    response = console.client.post(
        "/console/api/send", content="text=/balance", headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    assert response.status_code == 422


# ----------------------------------------------------------------------
# the chat
# ----------------------------------------------------------------------


def test_payment_flow_with_engine_notifications(console: Console) -> None:
    started = console.start(name="Priya")
    seq = last(started)["seq"]
    prompt_payload = console.send("send 12.5 HUSD to ravi.testnet", after=seq)
    me, prompt = prompt_payload["messages"]
    assert (me["role"], me["text"]) == ("me", "send 12.5 HUSD to ravi.testnet")
    assert prompt["text"].startswith("Send 12.5 HUSD to ravi.testnet?")
    confirm = prompt["buttons"][0][0]
    assert confirm["label"] == "Confirm" and confirm["data"].startswith("confirm:")

    after = console.act(confirm["data"], "Confirm", after=prompt["seq"])
    tapped, held = after["messages"]
    assert (tapped["role"], tapped["text"]) == ("me", "Confirm")
    assert held["role"] == "bot" and held["text"].startswith("Payment held")  # routed from the engine's notifier
    assert held["tone"] == "held"
    assert held["buttons"] == [[{"label": "Cancel & refund", "data": "cancel:0"}, {"label": "Why?", "data": "why:0"}]]
    [(user_key, payee, _, amount, _)] = console.engine.called("pay")
    assert (user_key, payee, amount) == (console.user_key, "ravi.testnet", 12_500_000)

    cancelled = console.act("cancel:0", "Cancel & refund", after=held["seq"])
    assert last(cancelled)["text"].startswith("Cancelled.")
    polled = console.client.get("/console/api/messages", params={"after": held["seq"]}).json()
    assert [m["text"] for m in polled["messages"]] == ["Cancel & refund", last(cancelled)["text"]]
    assert polled["account"] is None  # polling stays cheap


def test_a_training_sample_is_shown_as_a_forwarded_message(console: Console) -> None:
    console.start()
    payload = console.act("sample:hero", "Sample: CoinHaven DM")
    sample, reply = payload["messages"][-2:]
    assert sample["role"] == "sample"
    assert sample["title"] == "Forwarded from CoinHaven Support - training sample"
    assert "safe-wallet-77.testnet" in sample["text"]
    assert reply["text"].endswith("send 50 HUSD to safe-wallet-77.testnet")
    [(_, ctx)] = console.engine.called("remember_context")
    assert ctx.forwarded_text.startswith("Forwarded from CoinHaven Support:\nUnusual login.")


def test_slash_commands_in_the_console(console: Console) -> None:
    console.start()
    assert last(console.send("/balance"))["text"].startswith("Balance of")
    assert last(console.send("/"))["text"].startswith("HoldUp commands")
    assert last(console.send("/why@HoldUpTestBot 99"))["text"] == "There is no payment #99."
    demo = console.send("/demo")["messages"]
    assert any(m["role"] == "sample" and m["title"] == "Training sample (fictional)" for m in demo)


def test_guardian_invites_from_and_into_the_console(console: Console) -> None:
    console.start()
    invite = last(console.send("/guardian"))["text"]
    assert f"Web console: http://testserver/console?g={console.engine.invite_code}" in invite

    console.client.cookies.clear()
    console.start(invite=console.engine.invite_code)
    [(guardian_key, code)] = console.engine.called("accept_guardian")
    assert code == console.engine.invite_code
    assert guardian_key != console.user_key


def test_export_in_the_console_is_a_signed_link(console: Console) -> None:
    console.start()
    account = console.engine.account_for(console.user_key).account_id
    console.store.upsert_user(console.user_key, account)  # the real engine records users when they start
    text = last(console.send("/export"))["text"]
    url = text.splitlines()[-1]
    assert url.startswith("http://testserver/export/") and url.endswith(".csv")
    assert console.user_key not in url
    csv = console.client.get(url.removeprefix("http://testserver"))
    assert csv.status_code == 200 and csv.text == console.engine.csv


def test_the_invite_page_mentions_the_invitation(console: Console) -> None:
    page = console.client.get("/console", params={"g": "abc123"})
    assert 'data-invite="abc123"' in page.text
    assert "You were invited to be someone's guardian." in page.text
