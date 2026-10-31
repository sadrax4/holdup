"""The web console: a chat page that drives the same engine as the Telegram bot.

Each browser gets a random identity in an HttpOnly cookie, and the engine
sees it as ``web:<keyed hash>``. Everything said in a console conversation
(the person's messages, replies and the engine's notifications) is kept in
memory by :class:`ConsoleNotifier`; the page posts actions and polls for new
messages. Creating identities, which funds a demo account, is rate-limited
per client address and globally.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from ..bot.dialog import Dialog, Reply
from ..bot.notify import RoutingNotifier
from ..bot.ratelimit import RateLimiter
from ..bot.samples import Sample, find_sample
from ..config import Settings
from ..messages import fmt_amount
from ..models import UserAccount
from .signing import (
    CONSOLE_COOKIE,
    CONSOLE_PREFIX,
    EXPORT_TTL_MS,
    console_user_key,
    export_token,
    new_console_identity,
)

log = logging.getLogger(__name__)

Buttons = list[list[tuple[str, str]]]

COOKIE_MAX_AGE_S = 30 * 24 * 3600
NAME_MAX = 32
_BRAKE_PREFIXES = ("Brakes on", "Guardian alert", "Warning signs")
_OK_PREFIXES = (
    "Released",
    "Paid instantly",
    "Cancelled",
    "Done:",
    "You received",
    "Checks finished",
    "Full check finished",
)


def _wall_ms() -> int:
    return int(time.time() * 1000)


def tone_for(text: str) -> str:
    """A styling hint from the first line of a message."""
    first = text.lstrip()
    if first.startswith(_BRAKE_PREFIXES):
        return "brake"
    if first.startswith(_OK_PREFIXES):
        return "ok"
    if first.startswith("Payment held"):
        return "held"
    return ""


@dataclass
class ConsoleMessage:
    seq: int
    role: str  # "bot", "me" or "sample" (a training sample shown as a forwarded message)
    text: str
    buttons: Buttons = field(default_factory=list)
    ts_ms: int = 0
    title: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "role": self.role,
            "text": self.text,
            "buttons": [[{"label": label, "data": data} for label, data in row] for row in self.buttons],
            "ts": self.ts_ms,
            "title": self.title,
            "tone": tone_for(self.text) if self.role == "bot" else "",
        }


class ConsoleNotifier:
    """``Notifier`` for ``web:`` users: keeps each conversation in memory for the page to poll.

    Sequence numbers increase across all conversations, so a page asks for
    everything after the last number it has seen.
    """

    def __init__(
        self, *, max_messages: int = 200, max_users: int = 5_000, clock: Callable[[], int] | None = None
    ) -> None:
        self.max_messages = max_messages
        self.max_users = max_users
        self.clock = clock or _wall_ms
        self._conversations: OrderedDict[str, deque[ConsoleMessage]] = OrderedDict()
        self._seq = 0

    async def notify(self, user_key: str, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
        self.post(user_key, "bot", text, buttons)

    def post(
        self,
        user_key: str,
        role: str,
        text: str,
        buttons: Sequence[Sequence[tuple[str, str]]] | None = None,
        *,
        title: str = "",
    ) -> ConsoleMessage:
        conversation = self._conversations.pop(user_key, None)
        if conversation is None:
            conversation = deque(maxlen=self.max_messages)
        self._conversations[user_key] = conversation
        while len(self._conversations) > self.max_users:
            self._conversations.popitem(last=False)
        self._seq += 1
        rows = [[(str(label), str(data)) for label, data in row] for row in buttons or [] if row]
        message = ConsoleMessage(self._seq, role, text, rows, self.clock(), title)
        conversation.append(message)
        return message

    def since(self, user_key: str, after: int = 0) -> list[ConsoleMessage]:
        return [m for m in self._conversations.get(user_key, ()) if m.seq > after]


def find_console(notifier: Any) -> ConsoleNotifier | None:
    """The console notifier behind an engine's notifier, if it has one."""
    if isinstance(notifier, ConsoleNotifier):
        return notifier
    if isinstance(notifier, RoutingNotifier):
        route = notifier.route(CONSOLE_PREFIX)
        return route if isinstance(route, ConsoleNotifier) else None
    return None


def attach_console(engine: Any) -> ConsoleNotifier:
    """Make sure ``web:`` notifications reach a console notifier; returns it.

    ``holdup serve`` already wires one in; otherwise the engine's notifier is
    wrapped so console users are routed here and everyone else as before.
    """
    console = find_console(engine.notifier)
    if console is None:
        console = ConsoleNotifier()
        engine.notifier = RoutingNotifier({CONSOLE_PREFIX: console}, default=engine.notifier)
    return console


# ----------------------------------------------------------------------
# request bodies
# ----------------------------------------------------------------------


class StartBody(BaseModel):
    name: str = Field(default="", max_length=64)
    invite: str = Field(default="", max_length=64)
    after: int = 0


class SendBody(BaseModel):
    text: str = Field(min_length=1, max_length=8_000)
    after: int = 0


class ActionBody(BaseModel):
    data: str = Field(min_length=1, max_length=64)
    label: str = Field(default="", max_length=80)
    after: int = 0


def _clean_name(name: str) -> str:
    return " ".join("".join(ch for ch in name if ch.isprintable()).split())[:NAME_MAX]


@dataclass(frozen=True)
class Chip:
    label: str
    send: str = ""  # text to send, or
    action: str = ""  # button data to trigger


def story_chips(samples: Sequence[Sample]) -> list[Chip]:
    """Quick actions that walk through the demo story, then everyday commands."""
    chips: list[Chip] = []
    for sample_id, title in (
        ("hero", "CoinHaven DM"),
        ("invoice", "Northwind invoice"),
        ("benign", "Dinner with a friend"),
    ):
        sample = find_sample(list(samples), sample_id)
        if sample is None:
            continue
        chips += [Chip(f"/trust {account}", send=f"/trust {account}") for account in sample.trusted]
        chips.append(Chip(f"Sample: {title}", action=f"sample:{sample.id}"))
        chips.append(Chip(sample.command, send=sample.command))
    return chips


COMMAND_CHIPS = (
    Chip("Balance", send="/balance"),
    Chip("My payments", send="/holds"),
    Chip("Guardian invite", send="/guardian"),
    Chip("Practice messages", send="/demo"),
    Chip("Proof", send="/prove"),
    Chip("Export CSV", send="/export"),
    Chip("Help", send="/help"),
)


class Console:
    """State and routes of the web console."""

    def __init__(
        self,
        settings: Settings,
        engine: Any,
        notifier: ConsoleNotifier,
        templates: Jinja2Templates,
        *,
        secret: str,
        telegram_username: Callable[[], str] = lambda: "",
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.notifier = notifier
        self.templates = templates
        self.secret = secret
        self.telegram_username = telegram_username
        self.clock = clock or _wall_ms
        self.dialog = Dialog(
            settings, engine, invite_links=self.invite_links, export_link=self.export_link, clock=self.clock
        )
        self.new_per_client = RateLimiter(5, 3600.0)
        self.new_overall = RateLimiter(200, 3600.0)
        self.requests = RateLimiter(120, 60.0)

    # ------------------------------------------------------------------
    # links handed to the dialog
    # ------------------------------------------------------------------

    def invite_links(self, code: str) -> list[tuple[str, str]]:
        links = []
        if base := self.settings.public_base_url:
            links.append(("Web console", f"{base}/console?g={code}"))
        if username := self.telegram_username():
            links.append(("Telegram", f"https://t.me/{username}?start=g_{code}"))
        return links

    def export_link(self, account: UserAccount) -> tuple[str, int]:
        expires = self.clock() + EXPORT_TTL_MS
        token = export_token(self.secret, account.account_id, expires)
        return f"{self.settings.public_base_url}/export/{token}.csv", expires

    # ------------------------------------------------------------------
    # identity and limits
    # ------------------------------------------------------------------

    def user_key(self, request: Request) -> str | None:
        return console_user_key(self.secret, request.cookies.get(CONSOLE_COOKIE))

    def _require(self, request: Request) -> str:
        self._check_origin(request)
        key = self.user_key(request)
        if key is None:
            raise HTTPException(401, "open a console account first")
        self._limit(key)
        return key

    def _limit(self, key: str) -> None:
        if not self.requests.hit(key).allowed:
            raise HTTPException(429, "too many requests; slow down")

    @staticmethod
    def _check_origin(request: Request) -> None:
        """Refuse cross-site posts (the cookie is SameSite=Strict as well)."""
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
            raise HTTPException(403, "cross-site request refused")

    # ------------------------------------------------------------------
    # responses
    # ------------------------------------------------------------------

    def _post_replies(self, key: str, replies: Sequence[Reply]) -> None:
        for reply in replies:
            if reply.kind == "sample":
                self.notifier.post(key, "sample", reply.text, reply.buttons, title="Training sample (fictional)")
            else:
                self.notifier.post(key, "bot", reply.text, reply.buttons)

    async def _account(self, key: str) -> dict[str, Any]:
        account = self.engine.account_for(key)
        balances = await self.engine.balance(key)
        return {
            "id": account.account_id,
            "balances": [fmt_amount(self.settings, a, balances.get(a, 0)) for a in self.settings.assets],
        }

    async def _payload(self, key: str | None, after: int, *, account: bool) -> dict[str, Any]:
        body: dict[str, Any] = {"started": key is not None, "messages": [], "account": None}
        if key is None:
            return body
        body["messages"] = [m.to_json() for m in self.notifier.since(key, after)]
        if account:
            try:
                body["account"] = await self._account(key)
            except Exception:  # the conversation matters more than the balance strip
                log.exception("could not read the console account")
        return body

    # ------------------------------------------------------------------
    # routes
    # ------------------------------------------------------------------

    def router(self) -> APIRouter:
        router = APIRouter()
        router.add_api_route(
            "/console", self.page, methods=["GET"], response_class=HTMLResponse, include_in_schema=False
        )
        router.add_api_route("/console/api/state", self.state, methods=["GET"])
        router.add_api_route("/console/api/messages", self.messages, methods=["GET"])
        router.add_api_route("/console/api/start", self.start, methods=["POST"])
        router.add_api_route("/console/api/send", self.send, methods=["POST"])
        router.add_api_route("/console/api/action", self.action, methods=["POST"])
        return router

    async def page(self, request: Request) -> HTMLResponse:
        return self.templates.TemplateResponse(
            request,
            "console.html",
            {
                "story_chips": story_chips(self.dialog.samples),
                "command_chips": COMMAND_CHIPS,
                "telegram": self.telegram_username(),
                "invite": (request.query_params.get("g") or "")[:64],
            },
            headers={"X-Robots-Tag": "noindex"},
        )

    async def state(self, request: Request, after: int = 0) -> dict[str, Any]:
        key = self.user_key(request)
        if key is not None:
            self._limit(key)
        return await self._payload(key, after, account=True)

    async def messages(self, request: Request, after: int = 0) -> dict[str, Any]:
        key = self.user_key(request)
        if key is None:
            return await self._payload(None, after, account=False)
        self._limit(key)
        return await self._payload(key, after, account=False)

    async def start(self, request: Request, body: StartBody) -> JSONResponse:
        self._check_origin(request)
        key = self.user_key(request)
        token = None
        if key is None:
            client = request.client.host if request.client else "unknown"
            if not self.new_per_client.hit(client).allowed or not self.new_overall.hit("*").allowed:
                raise HTTPException(429, "too many new console accounts right now; please try again later")
            token, key = new_console_identity(self.secret)
        self._limit(key)
        payload = f"g_{body.invite}" if body.invite else ""
        replies = await self.dialog.start(key, display_name=_clean_name(body.name), payload=payload)
        self._post_replies(key, replies)
        response = JSONResponse(await self._payload(key, body.after, account=True))
        if token is not None:
            response.set_cookie(
                CONSOLE_COOKIE,
                token,
                max_age=COOKIE_MAX_AGE_S,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
                path="/",
            )
        return response

    async def send(self, request: Request, body: SendBody) -> dict[str, Any]:
        key = self._require(request)
        text = body.text.strip()
        if not text:
            raise HTTPException(422, "empty message")
        self.notifier.post(key, "me", text)
        if text.startswith("/"):
            name, *args = text[1:].split() or ["help"]
            name = name.split("@", 1)[0].lower()
            if name == "start":
                replies = await self.dialog.start(key, payload=args[0] if args else "")
            else:
                replies = await self.dialog.command(key, name, args)
        else:
            replies = await self.dialog.text(key, text)
        self._post_replies(key, replies)
        return await self._payload(key, body.after, account=True)

    async def action(self, request: Request, body: ActionBody) -> dict[str, Any]:
        key = self._require(request)
        action, _, arg = body.data.partition(":")
        sample = find_sample(self.dialog.samples, arg) if action == "sample" else None
        if sample is not None:
            title = f"Forwarded from {sample.sender} - training sample" if sample.sender else "Training sample"
            self.notifier.post(key, "sample", sample.text, title=title)
        elif body.label:
            self.notifier.post(key, "me", body.label)
        self._post_replies(key, await self.dialog.button(key, body.data))
        return await self._payload(key, body.after, account=True)
