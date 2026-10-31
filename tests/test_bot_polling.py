"""The bot in polling mode: the real python-telegram-bot stack against a fake Bot API server.

``FakeBotApi`` is a small HTTP server on 127.0.0.1 that answers like Telegram's
Bot API for one token. The app reaches it through ``TELEGRAM_API_BASE_URL``
(``settings.telegram_api_base_url``), so these tests run the same HTTP client,
updater and lifecycle as production, and nothing leaves the machine.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self
from urllib.parse import parse_qs

import pytest
from fastapi.testclient import TestClient
from telegram.error import Conflict, NetworkError
from telegram.ext import Application
from test_bot_fakes import (
    BOT_TOKEN,
    BOT_USER,
    WEBHOOK_SECRET,
    FakeEngine,
    FakeTelegram,
    make_settings,
    message_update,
)

from holdup.bot import app as bot_module
from holdup.bot.app import (
    BOT_COMMANDS,
    TOKEN_REJECTED,
    BotStatus,
    Poller,
    build_bot,
    describe_error,
)
from holdup.chain.fake import FakeVault
from holdup.store import Store
from holdup.web.app import create_app

CONFLICT = "Conflict: terminated by other getUpdates request; make sure that only one bot instance is running"
WEBHOOK_CONFLICT = (
    "Conflict: can't use getUpdates method while webhook is active; use deleteWebhook to delete the webhook first"
)
CONFLICT_LINE = "Telegram: another process is polling this bot token; stop it"


# ----------------------------------------------------------------------
# a fake Bot API server
# ----------------------------------------------------------------------


class FakeBotApi:
    """Enough of the Telegram Bot API for long polling, on a free 127.0.0.1 port.

    ``push`` queues an update for getUpdates; ``fail`` makes a method answer
    with an error the next ``times`` calls. While ``webhook`` is set,
    getUpdates answers 409 the way Telegram does.
    """

    def __init__(self, token: str = BOT_TOKEN, *, max_wait_s: float = 0.3) -> None:
        self.token = token
        self.max_wait_s = max_wait_s  # longest a getUpdates is held open here, whatever its timeout
        self.webhook = ""
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._updates: list[dict[str, Any]] = []
        self._failures: dict[str, list[tuple[int, str]]] = {}
        self._lock = threading.Condition()
        self._message_id = 500
        self.server = _QuietServer(("127.0.0.1", 0), _handler_for(self))
        # A short poll interval keeps shutdown() quick (the default waits up to 0.5 s).
        self._thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, name="fake-bot-api", daemon=True
        )

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()

    # ------------------------------------------------------------------
    # for tests
    # ------------------------------------------------------------------

    def push(self, update: dict[str, Any]) -> None:
        with self._lock:
            self._updates.append(update)
            self._lock.notify_all()

    def fail(self, method: str, status: int, description: str, *, times: int = 1) -> None:
        with self._lock:
            self._failures.setdefault(method, []).extend([(status, description)] * times)

    def recover(self) -> None:
        with self._lock:
            self._failures.clear()

    def names(self) -> list[str]:
        with self._lock:
            return [name for name, _ in self.calls]

    def params(self, method: str) -> list[dict[str, Any]]:
        with self._lock:
            return [params for name, params in self.calls if name == method]

    def sent(self) -> list[dict[str, Any]]:
        return self.params("sendMessage")

    # ------------------------------------------------------------------
    # for the request handler
    # ------------------------------------------------------------------

    def answer(self, path: str, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        method = path.rsplit("/", 1)[-1]
        with self._lock:
            self.calls.append((method, params))
            if not path.startswith(f"/bot{self.token}/"):
                return 401, {"ok": False, "error_code": 401, "description": "Unauthorized"}
            if failures := self._failures.get(method):
                status, description = failures.pop(0)
                return status, {"ok": False, "error_code": status, "description": description}
            if method == "getUpdates":
                return self._get_updates(params)
            if method == "deleteWebhook":
                self.webhook = ""
            elif method == "setWebhook":
                self.webhook = str(params.get("url", ""))
            return 200, {"ok": True, "result": self._result(method, params)}

    def _get_updates(self, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Long poll: wait (with the lock released) until an update is queued or the time is up."""
        if self.webhook:
            return 409, {"ok": False, "error_code": 409, "description": WEBHOOK_CONFLICT}
        offset = int(params.get("offset") or 0)
        deadline = time.monotonic() + min(float(params.get("timeout") or 0), self.max_wait_s)
        while True:
            self._updates = [u for u in self._updates if u["update_id"] >= offset]
            remaining = deadline - time.monotonic()
            if self._updates or remaining <= 0:
                return 200, {"ok": True, "result": list(self._updates)}
            self._lock.wait(remaining)

    def _result(self, method: str, params: dict[str, Any]) -> Any:
        if method == "getMe":
            return BOT_USER
        if method == "sendMessage":
            self._message_id += 1
            chat = {"id": int(params["chat_id"]), "type": "private"}
            return {"message_id": self._message_id, "date": 0, "chat": chat, "text": params.get("text", "")}
        return True


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A client that hangs up during a long poll (the bot stopping) is normal here.
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


def _handler_for(api: FakeBotApi) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            status, payload = api.answer(self.path, _params(body, self.headers.get("Content-Type", "")))
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return Handler


def _params(body: bytes, content_type: str) -> dict[str, Any]:
    """Bot API parameters: form fields whose non-string values python-telegram-bot sends as JSON."""
    if content_type.startswith("application/json"):
        return dict(json.loads(body or b"{}"))
    params: dict[str, Any] = {}
    for key, values in parse_qs(body.decode(), keep_blank_values=True).items():
        try:
            params[key] = json.loads(values[0])
        except ValueError:
            params[key] = values[0]
    return params


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def polling_app(api: FakeBotApi, **overrides: Any) -> tuple[TestClient, FakeEngine, Application]:
    """The web app with a polling bot whose real HTTP client talks to ``api``."""
    settings = make_settings(telegram_api_base_url=api.url, **overrides)
    engine = FakeEngine(settings)
    bot_app = build_bot(settings, engine)
    app = create_app(settings, engine, FakeVault.from_settings(settings), Store(":memory:"), bot_app=bot_app)
    return TestClient(app), engine, bot_app


def telegram_health(client: TestClient) -> dict[str, Any]:
    return dict(client.get("/health").json()["telegram"])


@pytest.fixture
def quick_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot_module, "START_RETRY_DELAYS_S", (0.05,))


# ----------------------------------------------------------------------
# polling inside the web app
# ----------------------------------------------------------------------


def test_the_bot_polls_answers_start_and_stops_in_order() -> None:
    with FakeBotApi() as api:
        api.webhook = "https://old-deployment.example/tg/secret"  # left behind by a webhook setup
        client, engine, bot_app = polling_app(api)
        update = message_update("/start")
        with client:
            assert wait_until(lambda: telegram_health(client)["running"])
            api.push(update)
            assert wait_until(lambda: bool(api.sent()))
            health = client.get("/health")
        assert bot_app.updater is not None
        assert not bot_app.running and not bot_app.updater.running

    [welcome] = api.sent()
    assert welcome["chat_id"] == 111 and welcome["text"].startswith("Welcome to HoldUp, Priya.")
    assert engine.called("start") == [("tg:111", "Priya", "111")]

    telegram = health.json()["telegram"]
    assert telegram["mode"] == "polling" and telegram["running"] is True
    assert telegram["username"] == "HoldUpTestBot" and telegram["conflict"] is False
    assert telegram["last_error"] is None and isinstance(telegram["last_poll_s"], int)
    for secret in (BOT_TOKEN, WEBHOOK_SECRET, "test-secret"):
        assert secret not in health.text

    names = api.names()
    assert names[:4] == ["getMe", "setMyCommands", "deleteWebhook", "getUpdates"]  # the stale webhook goes first
    assert api.webhook == ""
    assert [c["command"] for c in api.params("setMyCommands")[0]["commands"]] == [n for n, _ in BOT_COMMANDS]
    assert not api.params("deleteWebhook")[0].get("drop_pending_updates")  # waiting updates are kept
    polls = api.params("getUpdates")
    assert polls[0]["allowed_updates"] == ["message", "callback_query"]
    assert polls[0]["timeout"] == bot_module.POLL_TIMEOUT_S
    # Stopping ends with one short poll that confirms the updates already fetched.
    assert names[-1] == "getUpdates" and polls[-1]["timeout"] == 0
    assert polls[-1]["offset"] == update["update_id"] + 1


def test_a_polling_bot_has_no_webhook_route_while_it_runs() -> None:
    with FakeBotApi() as api:
        client, engine, _ = polling_app(api)
        with client:
            assert wait_until(lambda: telegram_health(client)["running"])
            assert client.post(f"/tg/{WEBHOOK_SECRET}", json=message_update("/start")).status_code == 404
        assert engine.calls == []


def test_another_poller_shows_as_a_conflict_until_polling_works_again(caplog: pytest.LogCaptureFixture) -> None:
    with FakeBotApi() as api:
        api.fail("getUpdates", 409, CONFLICT)
        client, _, _ = polling_app(api)
        with client:
            assert wait_until(lambda: telegram_health(client)["conflict"])
            assert telegram_health(client)["last_error"] == CONFLICT
            # PTB keeps polling after a conflict (1 s later here); the next answered poll clears it.
            assert wait_until(lambda: not telegram_health(client)["conflict"])
            telegram = telegram_health(client)
            assert telegram["running"] is True and telegram["last_error"] is None
    assert [r.getMessage() for r in caplog.records if r.name == "holdup.bot.app" and "polling this bot" in r.getMessage()] == [
        CONFLICT_LINE
    ]


def test_a_telegram_outage_at_startup_does_not_stop_the_site(quick_retries: None) -> None:
    with FakeBotApi() as api:
        api.fail("getMe", 502, "Bad Gateway", times=10_000)
        client, _, _ = polling_app(api)
        with client:
            assert client.get("/").status_code == 200
            assert wait_until(lambda: telegram_health(client)["last_error"] == "NetworkError: Bad Gateway")
            assert telegram_health(client)["running"] is False
            assert client.get("/console").status_code == 200

            api.recover()  # Telegram is back: the bot comes up on its own
            assert wait_until(lambda: telegram_health(client)["running"])
            api.push(message_update("/help"))
            assert wait_until(lambda: bool(api.sent()))
        assert api.names().count("getMe") >= 2


def test_a_rejected_token_stops_the_bot_and_stays_out_of_logs(
    quick_retries: None, caplog: pytest.LogCaptureFixture
) -> None:
    with FakeBotApi(token="999:ANOTHER-BOTS-TOKEN") as api:
        client, _, _ = polling_app(api)
        with client:
            assert wait_until(lambda: telegram_health(client)["last_error"] == TOKEN_REJECTED)
            time.sleep(0.2)  # time for retries, if there were any
            health = client.get("/health")
        assert health.json()["telegram"]["running"] is False
        assert api.names() == ["getMe"]  # no retries with a token Telegram refused
    # python-telegram-bot's InvalidToken quotes the token: it must not reach the log or /health.
    assert TOKEN_REJECTED in caplog.text
    assert BOT_TOKEN not in caplog.text and BOT_TOKEN not in health.text


def test_stopping_while_telegram_is_unreachable_does_not_wait_out_the_retry() -> None:
    with FakeBotApi() as api:
        api.fail("getMe", 502, "Bad Gateway", times=10_000)
        client, _, bot_app = polling_app(api)
        with client:
            assert wait_until(lambda: telegram_health(client)["last_error"] is not None)
            stopping = time.monotonic()
        assert time.monotonic() - stopping < 2.0  # the first retry would come after 5 s
        assert not bot_app.running


# ----------------------------------------------------------------------
# the pieces
# ----------------------------------------------------------------------


def test_conflicts_are_logged_at_most_once_a_minute(caplog: pytest.LogCaptureFixture) -> None:
    now = [1_000.0]
    status = BotStatus("polling", clock=lambda: now[0])
    poller = Poller(build_bot(make_settings(), FakeEngine(), request=FakeTelegram()), status)

    with caplog.at_level(logging.WARNING, logger="holdup.bot.app"):
        poller.on_error(Conflict(CONFLICT))
        poller.on_error(Conflict(CONFLICT))
        now[0] += 59
        poller.on_error(Conflict(CONFLICT))
        assert status.conflict and status.last_error == CONFLICT
        now[0] += 2
        poller.on_error(Conflict(WEBHOOK_CONFLICT))
        poller.on_error(NetworkError("httpx.ConnectError: All connection attempts failed"))
        poller.on_error(NetworkError("httpx.ConnectError: All connection attempts failed"))

    lines = [r.getMessage() for r in caplog.records if r.name == "holdup.bot.app"]
    assert lines == [
        CONFLICT_LINE,
        (
            "Telegram: a webhook was set for this bot token, so polling gets nothing; stop whatever set it "
            "(restarting HoldUp removes the webhook)"
        ),
        "Telegram polling failed, retrying: NetworkError: httpx.ConnectError: All connection attempts failed",
    ]


def test_the_status_follows_each_poll() -> None:
    now = [0.0]
    status = BotStatus("polling", running=True, clock=lambda: now[0])
    assert not status.conflict and status.as_dict()["last_poll_s"] is None

    status.conflict_at, status.last_error = 5.0, CONFLICT
    now[0] = 6.0
    assert status.conflict
    status.poll_answered(200)
    assert not status.conflict and status.last_error is None and status.polled_at == 6.0
    now[0] = 16.4
    assert status.as_dict()["last_poll_s"] == 10

    status.poll_answered(502)  # a failed poll changes nothing here; error_callback reports it
    assert status.running and status.polled_at == 6.0
    status.poll_answered(401)  # PTB stops polling for good on 401 and 404
    assert not status.running and status.last_error == TOKEN_REJECTED
    assert set(status.as_dict()) == {"mode", "running", "username", "last_error", "conflict", "last_poll_s"}


def test_error_descriptions_never_carry_the_token() -> None:
    from telegram.error import InvalidToken

    rejected = InvalidToken(f"The token `{BOT_TOKEN}` was rejected by the server.")
    assert BOT_TOKEN not in describe_error(rejected, BOT_TOKEN)
    assert describe_error(Conflict(CONFLICT)) == CONFLICT
    assert describe_error(NetworkError("Bad Gateway")) == "NetworkError: Bad Gateway"
    assert describe_error(OSError("disk full")) == "OSError: disk full"
    assert describe_error(TimeoutError()) == "TimeoutError"
    # /health is public: keys that ride in a URL's query string are left out too.
    rpc = RuntimeError("Non-JSON response from https://rpc.example/v1?apiKey=k3y&x=1 (HTTP 429)")
    assert describe_error(rpc) == "RuntimeError: Non-JSON response from https://rpc.example/v1?[redacted] (HTTP 429)"


def test_build_bot_points_at_another_bot_api_server_when_asked() -> None:
    for root in ("http://127.0.0.1:8081", "http://127.0.0.1:8081/", "http://127.0.0.1:8081/bot"):
        bot = build_bot(make_settings(telegram_api_base_url=root), FakeEngine(), request=FakeTelegram()).bot
        assert bot.base_url == f"http://127.0.0.1:8081/bot{BOT_TOKEN}"
        assert bot.base_file_url == f"http://127.0.0.1:8081/file/bot{BOT_TOKEN}"
    default = build_bot(make_settings(), FakeEngine(), request=FakeTelegram()).bot
    assert default.base_url == f"https://api.telegram.org/bot{BOT_TOKEN}"


def test_the_mode_decides_how_the_bot_is_built() -> None:
    polling = build_bot(make_settings(), FakeEngine(), request=FakeTelegram())
    assert polling.updater is not None
    webhook = build_bot(make_settings(telegram_mode="webhook"), FakeEngine(), request=FakeTelegram())
    assert webhook.updater is None
    with pytest.raises(ValueError, match="updater"):
        Poller(webhook, BotStatus("webhook"))
    with pytest.raises(ValueError, match="TELEGRAM_MODE"):
        build_bot(make_settings(telegram_mode="off"), FakeEngine())
