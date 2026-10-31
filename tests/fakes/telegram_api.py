"""A small fake of the Telegram Bot API on 127.0.0.1, for tests that run whole processes.

It answers ``/bot<token>/<method>`` the way api.telegram.org does for the calls a
polling bot makes (getMe, deleteWebhook, setMyCommands, getUpdates, sendMessage)
and returns 401 for any other token. ``getUpdates`` waits a little, like a long
poll, and returns the messages queued with :meth:`FakeBotAPI.send_text`. The
methods called are kept in :attr:`FakeBotAPI.calls`, and what the bot sent in
:attr:`FakeBotAPI.sent`.

    api = FakeBotAPI(token="123456:...")
    base_url = api.start()          # http://127.0.0.1:<port>, for TELEGRAM_API_BASE_URL
    api.send_text("/start")         # a user writes to the bot
    ...
    api.stop()
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs

USER_ID = 4242  # the one user who writes to the bot (a private chat has the user's id)


class FakeBotAPI:
    def __init__(self, token: str, username: str = "holdup_test_bot", poll_wait_s: float = 0.5) -> None:
        self.token = token
        self.username = username
        self.poll_wait_s = poll_wait_s
        self.calls: list[str] = []
        self.sent: list[dict[str, Any]] = []  # sendMessage parameters, in order
        self.rejected = 0
        self._updates: list[dict[str, Any]] = []
        self._next_update = 1
        self._next_message = 100
        self._lock = threading.Condition()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.answer({})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                self.answer(api.parse_body(self.headers.get("Content-Type", ""), raw))

            def answer(self, params: dict[str, Any]) -> None:
                status, payload = api.handle(self.path, params)
                body = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the bot stopped polling while a long poll was open

            def log_message(self, format: str, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # ------------------------------------------------------------------
    # what a user does, and what the bot answered
    # ------------------------------------------------------------------

    def send_text(self, text: str, *, user_id: int = USER_ID, first_name: str = "Test") -> int:
        """Queue a private message from a user, as getUpdates delivers it; returns its update_id."""
        with self._lock:
            update_id, self._next_update = self._next_update, self._next_update + 1
            self._next_message += 1
            person = {"id": user_id, "is_bot": False, "first_name": first_name}
            message: dict[str, Any] = {
                "message_id": self._next_message, "date": int(time.time()), "text": text,
                "chat": {"id": user_id, "type": "private", "first_name": first_name}, "from": person,
            }
            if text.startswith("/"):
                message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
            self._updates.append({"update_id": update_id, "message": message})
            self._lock.notify_all()
        return update_id

    def press(self, data: str, *, user_id: int = USER_ID, first_name: str = "Test") -> int:
        """Queue a press on an inline button whose callback data is ``data``; returns its update_id."""
        with self._lock:
            update_id, self._next_update = self._next_update, self._next_update + 1
            person = {"id": user_id, "is_bot": False, "first_name": first_name}
            message = {"message_id": self._next_message, "date": int(time.time()), "text": "",
                       "chat": {"id": user_id, "type": "private", "first_name": first_name}}
            query = {"id": f"query-{update_id}", "from": person, "chat_instance": str(user_id), "data": data,
                     "message": message}
            self._updates.append({"update_id": update_id, "callback_query": query})
            self._lock.notify_all()
        return update_id

    def replies(self, user_id: int = USER_ID) -> list[str]:
        """The texts the bot has sent to ``user_id`` so far."""
        with self._lock:
            return [str(p.get("text", "")) for p in self.sent if str(p.get("chat_id")) == str(user_id)]

    def buttons(self, user_id: int = USER_ID) -> list[tuple[str, str]]:
        """(label, callback data) of every inline button the bot has sent to ``user_id``, oldest first."""
        found = []
        with self._lock:
            markups = [p.get("reply_markup") for p in self.sent if str(p.get("chat_id")) == str(user_id)]
        for markup in markups:
            if isinstance(markup, str):
                try:
                    markup = json.loads(markup)
                except ValueError:
                    continue
            for row in (markup or {}).get("inline_keyboard", []) if isinstance(markup, dict) else []:
                found += [(b.get("text", ""), b["callback_data"]) for b in row if "callback_data" in b]
        return found

    # ------------------------------------------------------------------
    # the Bot API
    # ------------------------------------------------------------------

    @staticmethod
    def parse_body(content_type: str, raw: bytes) -> dict[str, Any]:
        if not raw:
            return {}
        if "application/json" in content_type:
            try:
                data = json.loads(raw)
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}
        if "application/x-www-form-urlencoded" in content_type:
            return {key: values[-1] for key, values in parse_qs(raw.decode("utf-8", "replace")).items()}
        return {}

    def handle(self, path: str, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        prefix = f"/bot{self.token}/"
        if not path.startswith(prefix):
            self.rejected += 1
            return 401 if path.startswith("/bot") else 404, {
                "ok": False, "error_code": 401, "description": "Unauthorized"}
        method = path[len(prefix):].split("?", 1)[0]
        self.calls.append(method)
        if method == "getMe":
            return 200, {"ok": True, "result": {
                "id": int(self.token.split(":", 1)[0]), "is_bot": True, "first_name": "HoldUp test",
                "username": self.username, "can_join_groups": False, "can_read_all_group_messages": False,
                "supports_inline_queries": False}}
        if method == "getUpdates":
            return 200, {"ok": True, "result": self._get_updates(params)}
        if method == "sendMessage":
            with self._lock:
                self.sent.append(params)
                self._next_message += 1
                chat = {"id": int(params.get("chat_id") or 0), "type": "private"}
                result = {"message_id": self._next_message, "date": int(time.time()), "chat": chat,
                          "text": str(params.get("text", ""))}
            return 200, {"ok": True, "result": result}
        return 200, {"ok": True, "result": True}

    def _get_updates(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """A long poll: the queued updates from ``offset`` on, waiting a little while there are none."""
        try:
            offset = int(params.get("offset") or 0)
            timeout = float(params.get("timeout") or 0)
        except (TypeError, ValueError):
            offset, timeout = 0, 0.0
        deadline = time.monotonic() + min(timeout, self.poll_wait_s)
        with self._lock:
            while True:
                self._updates = [u for u in self._updates if u["update_id"] >= offset]
                remaining = deadline - time.monotonic()
                if self._updates or remaining <= 0:
                    return list(self._updates)
                self._lock.wait(remaining)
