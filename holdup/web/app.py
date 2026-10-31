"""The HoldUp web app: public pages, the web console, the Telegram webhook and optional mounts.

``create_app`` serves:

- ``/`` landing page, ``/h/<id>`` hold timeline, ``/why/<id>`` every AI step,
  ``/v/<id>.json`` evidence exactly as hashed on-chain, ``/prove`` live proof
  of the deployment, ``/stats``, ``/export/<token>.csv`` signed CSV links,
  ``/health``;
- ``/console``: the web console, a chat that drives the same engine as the bot;
- ``POST /tg/<secret>``: the Telegram webhook, when a bot application in
  webhook mode is given (a polling bot runs inside the app's lifespan instead);
- ``/fixtures/...``: fictional training pages for the demo story, on their own
  host when ``FIXTURE_BASE_URL`` names one (see :class:`FixtureHost`);
- ``/mcp`` (near-intel MCP server) and ``/validate`` (guardrail endpoint), only
  when the caller passes them in.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application

from .. import __version__
from ..bot.app import BotStatus, Poller, channel_of, configure_webhook, describe_error
from ..config import Settings
from ..interfaces import VaultPort
from ..messages import fmt_duration, fmt_utc
from .console import Console, attach_console
from .fixtures import fixtures_router
from .pages import Site, json_error, network_label

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
]
JSON_PREFIXES = ("/console/api/", "/tg/", "/mcp", "/validate", "/profiles", "/health")


class SecurityHeaders:
    """Adds a strict CSP and friends to every HTTP response (pure ASGI, so streaming is untouched)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                present = {name for name, _ in message.get("headers", [])}
                extra = [(name, value) for name, value in SECURITY_HEADERS if name not in present]
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        await self.app(scope, receive, send_with_headers)


class FixtureHost:
    """Keeps the training pages on their own host when ``FIXTURE_BASE_URL`` names one.

    Each host reaches the app through its own tunnel (scripts/server.sh runs a
    quick tunnel per host), and the app tells them apart by the Host header: the
    fixture host serves only the training pages and their stylesheet, and every
    other host redirects ``/fixtures`` there. A phishing report about those
    pages then names the fixture host, not the host that people and the Otari
    callbacks (``/mcp``, ``/validate``) depend on.
    """

    STYLESHEET = "/static/fixtures.css"

    def __init__(self, app: ASGIApp, fixture_base_url: str) -> None:
        self.app = app
        self.base = fixture_base_url.rstrip("/")
        self.host = _hostname(self.base)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        training = path == "/fixtures" or path.startswith("/fixtures/")
        if _hostname("//" + _header(scope, b"host")) == self.host:
            if (training or path == self.STYLESHEET) and scope["method"] in ("GET", "HEAD"):
                await self.app(scope, receive, send)
            else:
                await PlainTextResponse("Not found", status_code=404)(scope, receive, send)
            return
        if training:
            query = scope.get("query_string", b"").decode("latin-1")
            target = self.base + path + (f"?{query}" if query else "")
            await RedirectResponse(target, status_code=302)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _hostname(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:  # a malformed Host header
        return ""


def _header(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers", []):
        if key == name:
            return bytes(value).decode("latin-1")
    return ""


def _asset_version() -> str:
    digest = hashlib.sha256()
    for path in sorted(STATIC_DIR.glob("*")):
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def make_templates(settings: Settings) -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    env = templates.env
    env.globals.update(
        asset_version=_asset_version(),
        network_label=network_label(settings),
        network=settings.network,
        ai_replay=settings.otari_mode == "replay",
        base_url=settings.public_base_url,
        version=__version__,
    )
    env.filters["utc"] = lambda ms, now=None: fmt_utc(int(ms), int(now) if now is not None else None) if ms else ""
    env.filters["duration"] = lambda ms: fmt_duration(int(ms))
    env.filters["iso"] = _iso
    return templates


def _iso(ms: int | None) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, UTC).isoformat(timespec="seconds") if ms else ""


def _bot_username(bot_app: Application | None) -> str:
    if bot_app is None:
        return ""
    channel = channel_of(bot_app)
    return channel.bot_username() if channel is not None else ""


def _bot_status(bot_app: Application | None) -> BotStatus | None:
    channel = channel_of(bot_app) if bot_app is not None else None
    return channel.status if channel is not None else None


async def _start_bot(
    bot_app: Application, settings: Settings, stack: AsyncExitStack, app: FastAPI, status: BotStatus
) -> None:
    """Run the bot for the app's lifetime; the web app keeps running without it.

    A polling bot comes up in the background (and retries while Telegram is
    unreachable); a webhook bot is started here and registers its webhook.
    """
    if status.mode == "polling":
        poller = Poller(bot_app, status)
        poller.start()
        stack.push_async_callback(poller.stop)
        return
    secret_values = (settings.telegram_bot_token, settings.telegram_webhook_secret)
    try:
        await bot_app.initialize()
    except TelegramError as exc:  # InvalidToken's own message carries the token
        status.last_error = describe_error(exc, *secret_values)
        log.warning("Telegram bot unavailable; the webhook is disabled (%s)", status.last_error)
        return
    stack.push_async_callback(bot_app.shutdown)
    await bot_app.start()
    stack.push_async_callback(bot_app.stop)
    app.state.telegram_ready = status.running = True
    status.username = bot_app.bot.username or ""
    try:
        if await configure_webhook(bot_app, settings):
            log.info("Telegram webhook registered at %s/tg/...", settings.public_base_url)
    except TelegramError as exc:
        status.last_error = describe_error(exc, *secret_values)
        log.warning("could not register the Telegram webhook: %s", status.last_error)


def install_error_pages(app: FastAPI, templates: Jinja2Templates) -> None:
    """Friendly HTML error pages for people, JSON errors for API and data routes."""

    def error_response(request: Request, status: int, message: str) -> Response:
        path = request.url.path
        if path.startswith(JSON_PREFIXES) or path.endswith((".json", ".csv")):
            return json_error(status, message)
        context = {"status": status, "message": message}
        return templates.TemplateResponse(request, "error.html", context, status_code=status)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        message = str(exc.detail) if exc.status_code < 500 else "Something went wrong on our side."
        if exc.status_code == 404 and exc.detail == "Not Found" and not request.url.path.endswith(".json"):
            message = "There is nothing at this address."
        return error_response(request, exc.status_code, message)

    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception) -> Response:
        log.error("%s %s failed", request.method, request.url.path, exc_info=exc)
        return error_response(request, 500, "Something went wrong on our side. Please try again in a moment.")


def webhook_router(settings: Settings, bot_app: Application, state: Any) -> APIRouter:
    """``POST /tg/<secret>``: Telegram's webhook, feeding the bot's update queue.

    The secret in the path is compared in constant time; Telegram's secret
    token header, when present, must match it too.
    """
    router = APIRouter(include_in_schema=False)
    expected = settings.telegram_webhook_secret.encode()

    @router.post("/tg/{secret_path}")
    async def telegram_webhook(secret_path: str, request: Request) -> Response:
        if not expected or not hmac.compare_digest(secret_path.encode(), expected):
            raise HTTPException(404, "not found")
        header = request.headers.get("x-telegram-bot-api-secret-token")
        if header is not None and not hmac.compare_digest(header.encode(), expected):
            raise HTTPException(403, "wrong secret token")
        if not state.telegram_ready:
            raise HTTPException(503, "the bot is not running")
        try:
            update = Update.de_json(await request.json(), bot_app.bot)
        except Exception as exc:
            raise HTTPException(400, "not a Telegram update") from exc
        if update is None:
            raise HTTPException(400, "not a Telegram update")
        await bot_app.update_queue.put(update)
        return Response(status_code=200)

    return router


def mount_mcp(app: FastAPI, endpoint: ASGIApp) -> None:
    """Serve the MCP endpoint at ``/mcp`` and below it: MCP clients do not follow redirects."""
    app.router.routes.append(Route("/mcp", endpoint, methods=["GET", "POST", "DELETE"]))
    app.mount("/mcp", endpoint)


def create_app(
    settings: Settings,
    engine: Any,
    vault: VaultPort,
    store: Any,
    *,
    bot_app: Application | None = None,
    mcp: tuple[ASGIApp, Any] | None = None,
    guard_router: APIRouter | None = None,
    health_extra: Callable[[], Mapping[str, Any]] | None = None,
) -> FastAPI:
    """The web app around one engine.

    ``bot_app`` comes from ``holdup.bot.app.build_bot``; it runs for the app's
    lifetime in the mode it was built for. ``mcp`` is ``(asgi_app,
    lifespan_cm)`` from ``holdup.mcp_server.build_mcp``; ``guard_router`` comes
    from ``holdup.guard.build_router``. Both are mounted only when given.
    ``health_extra`` adds fields to ``/health`` (the keeper's liveness from
    ``holdup serve``). Web console notifications are routed to the console
    (``engine.notifier`` is wrapped if it does not route ``web:`` users yet).
    """
    secret = settings.secret or secrets.token_hex(32)  # without a secret, links last until restart
    templates = make_templates(settings)

    def telegram_username() -> str:
        return _bot_username(bot_app)

    site = Site(settings, engine, vault, store, templates, secret=secret, telegram_username=telegram_username)
    console = Console(
        settings,
        engine,
        attach_console(engine),
        templates,
        secret=secret,
        store=store,
        telegram_username=telegram_username,
    )

    bot_status = _bot_status(bot_app)
    if bot_app is not None and bot_status is None:
        raise ValueError("bot_app must come from holdup.bot.app.build_bot")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            if mcp is not None:
                await stack.enter_async_context(mcp[1])
            if bot_app is not None and bot_status is not None:
                await _start_bot(bot_app, settings, stack, app, bot_status)
            yield

    app = FastAPI(
        title="HoldUp", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.telegram_ready = False
    fixture_host = _hostname(settings.fixture_base_url)
    if fixture_host and fixture_host != _hostname(settings.public_base_url):
        app.add_middleware(FixtureHost, fixture_base_url=settings.fixture_base_url)
    app.add_middleware(SecurityHeaders)  # added last, so it wraps the responses of FixtureHost too
    install_error_pages(app, templates)

    async def health() -> dict[str, Any]:
        """Liveness for people and scripts. Public, so nothing secret goes in here."""
        telegram = bot_status.as_dict() if bot_status is not None else BotStatus("off").as_dict()
        return {
            "ok": True,
            "version": __version__,
            "network": settings.network,
            "vault": settings.vault_account,
            "public_base_url": settings.public_base_url,
            "telegram": telegram,
            "mcp": mcp is not None,
            "guardrail": guard_router is not None,
            **(health_extra() if health_extra is not None else {}),
        }

    async def favicon() -> RedirectResponse:
        return RedirectResponse("/static/favicon.svg", status_code=301)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.add_api_route("/favicon.ico", favicon, methods=["GET"], include_in_schema=False)
    app.add_api_route("/health", health, methods=["GET"])
    app.include_router(site.router())
    app.include_router(console.router())
    app.include_router(fixtures_router(templates, settings))
    if bot_app is not None and bot_status is not None and bot_status.mode == "webhook":
        app.include_router(webhook_router(settings, bot_app, app.state))
    if guard_router is not None:
        app.include_router(guard_router)
    if mcp is not None:
        mount_mcp(app, mcp[0])
    return app
