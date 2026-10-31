"""App wiring: the Telegram webhook, the bot's lifecycle, and the optional MCP and guardrail mounts.

Polling mode, the default, is tested against a fake Bot API in ``test_bot_polling.py``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse
from test_bot_fakes import (
    BOT_TOKEN,
    WEBHOOK_SECRET,
    FakeEngine,
    FakeTelegram,
    make_settings,
    message_update,
)

from holdup.bot.app import build_bot
from holdup.chain.fake import FakeVault
from holdup.config import Settings
from holdup.store import Store
from holdup.web.app import create_app


@dataclass
class Hook:
    client: TestClient
    engine: FakeEngine
    telegram: FakeTelegram

    def wait_for(self, condition: Callable[[], bool], timeout: float = 3.0) -> bool:
        """Let the app's event loop work until ``condition`` holds."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            self.client.portal.call(asyncio.sleep, 0.02)
        return condition()


def build(settings: Settings, telegram: FakeTelegram | None = None, **kwargs: Any) -> tuple[TestClient, FakeEngine]:
    engine = FakeEngine(settings)
    bot_app = None
    if telegram is not None:
        bot_app = build_bot(settings, engine, request=telegram, get_updates_request=FakeTelegram())
    app = create_app(settings, engine, FakeVault.from_settings(settings), Store(":memory:"), bot_app=bot_app, **kwargs)
    return TestClient(app), engine


def webhook_settings(**overrides: Any) -> Settings:
    return make_settings(telegram_mode="webhook", **overrides)


@pytest.fixture
def hook() -> Iterator[Hook]:
    telegram = FakeTelegram()
    client, engine = build(webhook_settings(), telegram)
    with client:
        yield Hook(client, engine, telegram)


# ----------------------------------------------------------------------
# the webhook
# ----------------------------------------------------------------------


def test_updates_with_the_right_secret_reach_the_bot(hook: Hook) -> None:
    response = hook.client.post(f"/tg/{WEBHOOK_SECRET}", json=message_update("/start"))
    assert response.status_code == 200
    assert hook.wait_for(lambda: bool(hook.telegram.sent()))
    assert hook.engine.called("start") == [("tg:111", "Priya", "111")]
    assert hook.telegram.texts()[0].startswith("Welcome to HoldUp, Priya.")


@pytest.mark.parametrize("path", ["/tg/wrong-secret", "/tg/hook-secret_12", f"/tg/{WEBHOOK_SECRET}x"])
def test_a_wrong_secret_is_not_found(hook: Hook, path: str) -> None:
    response = hook.client.post(path, json=message_update("/start"))
    assert response.status_code == 404
    hook.client.portal.call(asyncio.sleep, 0.1)
    assert hook.engine.calls == []


def test_the_secret_token_header_must_match_when_sent(hook: Hook) -> None:
    update = message_update("/help")
    wrong = hook.client.post(f"/tg/{WEBHOOK_SECRET}", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "nope"})
    assert wrong.status_code == 403
    right = hook.client.post(
        f"/tg/{WEBHOOK_SECRET}", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}
    )
    assert right.status_code == 200
    assert hook.wait_for(lambda: any("HoldUp commands" in t for t in hook.telegram.texts()))


def test_garbage_is_refused(hook: Hook) -> None:
    assert hook.client.post(f"/tg/{WEBHOOK_SECRET}", content=b"{not json").status_code == 400
    assert hook.client.post(f"/tg/{WEBHOOK_SECRET}", json=[1, 2, 3]).status_code == 400


def test_health_reports_the_running_bot(hook: Hook) -> None:
    telegram = hook.client.get("/health").json()["telegram"]
    assert telegram == {
        "mode": "webhook",
        "running": True,
        "username": "HoldUpTestBot",
        "last_error": None,
        "conflict": False,
        "last_poll_s": None,
    }


def test_without_a_bot_there_is_no_webhook() -> None:
    client, _ = build(make_settings())
    with client:
        assert client.post(f"/tg/{WEBHOOK_SECRET}", json=message_update("/start")).status_code == 404


def test_without_a_configured_secret_there_is_no_webhook() -> None:
    client, _ = build(webhook_settings(telegram_webhook_secret=""), FakeTelegram())
    with client:
        assert client.post("/tg/", json=message_update("/start")).status_code == 404
        assert client.post("/tg/anything", json=message_update("/start")).status_code == 404


def test_the_site_works_when_telegram_does_not(caplog: pytest.LogCaptureFixture) -> None:
    telegram = FakeTelegram()
    telegram.errors["getMe"] = (401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
    client, _ = build(webhook_settings(), telegram)
    with client:
        assert client.get("/").status_code == 200
        health = client.get("/health")
        assert health.json()["telegram"]["running"] is False
        assert "InvalidToken" in health.json()["telegram"]["last_error"]
        assert client.post(f"/tg/{WEBHOOK_SECRET}", json=message_update("/start")).status_code == 503
    # PTB's InvalidToken message quotes the token; neither the log nor /health may.
    assert BOT_TOKEN not in caplog.text and BOT_TOKEN not in health.text


def test_startup_registers_the_webhook_on_https() -> None:
    telegram = FakeTelegram()
    client, _ = build(webhook_settings(public_base_url="https://holdup.example"), telegram)
    with client:
        hooks = [params for name, params in telegram.calls if name == "setWebhook"]
    assert [h["url"] for h in hooks] == [f"https://holdup.example/tg/{WEBHOOK_SECRET}"]
    assert any(name == "setMyCommands" for name, _ in telegram.calls)


def test_startup_on_http_does_not_touch_the_webhook(hook: Hook) -> None:
    assert [name for name, _ in hook.telegram.calls] == ["getMe"]


def test_a_polling_bot_has_no_webhook_route() -> None:
    """Polling needs no inbound route, so none is mounted that could be fed forged updates."""
    client, engine = build(make_settings(), FakeTelegram())  # not started: no lifespan, no polling
    assert client.post(f"/tg/{WEBHOOK_SECRET}", json=message_update("/start")).status_code == 404
    assert engine.calls == []


# ----------------------------------------------------------------------
# optional mounts
# ----------------------------------------------------------------------


class FakeMcp:
    """An ASGI endpoint and a lifespan, shaped like ``holdup.mcp_server.build_mcp``'s result."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.paths: list[str] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.paths.append(scope["path"])
        await JSONResponse({"mcp": True})(scope, receive, send)

    @asynccontextmanager
    async def lifespan(self) -> Any:
        self.events.append("enter")
        yield
        self.events.append("exit")


def test_mcp_and_guardrail_are_mounted_when_given() -> None:
    mcp = FakeMcp()
    guard = APIRouter()

    @guard.post("/validate")
    async def validate() -> dict[str, Any]:
        return {"result": {"valid": True}}

    client, _ = build(make_settings(), mcp=(mcp, mcp.lifespan()), guard_router=guard)
    with client:
        assert mcp.events == ["enter"]
        assert client.post("/mcp", json={}).json() == {"mcp": True}
        assert client.post("/mcp/", json={}).json() == {"mcp": True}
        assert client.post("/validate", json={}).json() == {"result": {"valid": True}}
        health = client.get("/health").json()
        assert health["mcp"] is True and health["guardrail"] is True
    assert mcp.events == ["enter", "exit"]
    assert len(mcp.paths) == 2


def test_nothing_is_mounted_when_not_given() -> None:
    client, _ = build(make_settings())
    with client:
        assert client.post("/mcp", json={}).status_code == 404
        assert client.post("/validate", json={}).status_code == 404


# ----------------------------------------------------------------------
# training pages on their own host
# ----------------------------------------------------------------------

FIXTURE_HOST = "fixtures-tunnel.example"


def test_training_pages_stay_on_their_own_host() -> None:
    settings = make_settings(public_base_url="https://bot-tunnel.example", fixture_base_url=f"https://{FIXTURE_HOST}")
    client, _ = build(settings)
    on_fixture_host = {"host": FIXTURE_HOST}
    with client:
        page = client.get("/fixtures/coinhaven/verify", headers=on_fixture_host)
        assert page.status_code == 200 and "TRAINING FIXTURE" in page.text
        assert client.get("/static/fixtures.css", headers=on_fixture_host).status_code == 200
        # Nothing else there: not the bot's pages, not /mcp, not /health.
        for path in ("/", "/health", "/console", "/mcp", "/static/style.css"):
            assert client.get(path, headers=on_fixture_host).status_code == 404, path
        assert client.post("/fixtures/coinhaven/verify", headers=on_fixture_host).status_code == 404
        response = client.get("/health", headers=on_fixture_host)
        assert response.headers["x-content-type-options"] == "nosniff"  # still under the security headers

        # The bot's own host (and any other) never serves them: it points to the fixture host.
        moved = client.get("/fixtures/northwind?ref=mail", headers={"host": "bot-tunnel.example"}, follow_redirects=False)
        assert moved.status_code == 302
        assert moved.headers["location"] == f"https://{FIXTURE_HOST}/fixtures/northwind?ref=mail"
        assert client.get("/fixtures", follow_redirects=False).status_code == 302  # Host: testserver
        assert client.get("/health").status_code == 200


def test_without_public_urls_the_training_pages_are_only_served_to_localhost() -> None:
    """How scripts/server.sh starts the app while its tunnels are not both up."""
    settings = make_settings(public_base_url="http://127.0.0.1:47900", fixture_base_url="http://localhost:47900")
    client, _ = build(settings)
    with client:
        through_a_tunnel = {"host": "some-words.trycloudflare.com"}
        moved = client.get("/fixtures/coinhaven/verify", headers=through_a_tunnel, follow_redirects=False)
        assert moved.status_code == 302
        assert moved.headers["location"] == "http://localhost:47900/fixtures/coinhaven/verify"
        assert client.get("/health", headers={"host": "127.0.0.1:47900"}).status_code == 200  # the supervisor
        assert client.get("/fixtures/coinhaven/verify", headers={"host": "localhost:47900"}).status_code == 200


def test_one_host_for_everything_serves_the_training_pages_as_before() -> None:
    same = make_settings(public_base_url="https://holdup.example", fixture_base_url="https://holdup.example")
    for settings in (make_settings(), same):
        client, _ = build(settings)
        with client:
            assert client.get("/fixtures/coinhaven/verify").status_code == 200
