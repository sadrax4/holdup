"""The HoldUp web app: public pages, the web console, the Telegram webhook and optional mounts.

``create_app`` serves:

- ``/`` landing page, ``/h/<id>`` hold timeline, ``/why/<id>`` every AI step,
  ``/v/<id>.json`` evidence exactly as hashed on-chain, ``/prove`` live proof
  of the deployment, ``/stats``, ``/export/<token>.csv`` signed CSV links,
  ``/health``;
- ``/console``: the web console, a chat that drives the same engine as the bot;
- ``POST /tg/<secret>``: the Telegram webhook, when a bot application is given;
- ``/fixtures/...``: fictional training pages for the demo story;
- ``/mcp`` (near-intel MCP server) and ``/validate`` (guardrail endpoint), only
  when the caller passes them in.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application

from .. import __version__
from ..bot.app import channel_of, configure_webhook
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


async def _start_bot(bot_app: Application, settings: Settings, stack: AsyncExitStack, app: FastAPI) -> None:
    """Initialize and start the bot for the app's lifetime; the web app keeps running without it."""
    try:
        await bot_app.initialize()
    except Exception as exc:
        log.warning("Telegram bot unavailable, webhook disabled: %r", exc)
        return
    stack.push_async_callback(bot_app.shutdown)
    await bot_app.start()
    stack.push_async_callback(bot_app.stop)
    app.state.telegram_ready = True
    try:
        if await configure_webhook(bot_app, settings):
            log.info("Telegram webhook registered at %s/tg/...", settings.public_base_url)
    except TelegramError as exc:
        log.warning("could not register the Telegram webhook: %s", exc)


def create_app(
    settings: Settings,
    engine: Any,
    vault: VaultPort,
    store: Any,
    *,
    bot_app: Application | None = None,
    mcp: tuple[ASGIApp, Any] | None = None,
    guard_router: APIRouter | None = None,
) -> FastAPI:
    """The web app around one engine.

    ``mcp`` is ``(asgi_app, lifespan_cm)`` from ``holdup.mcp_server.build_mcp``;
    ``guard_router`` comes from ``holdup.guard.build_router``. Both are mounted
    only when given. Web console notifications are routed to the console
    (``engine.notifier`` is wrapped if it does not route ``web:`` users yet).
    """
    secret = settings.secret or secrets.token_hex(32)  # without a secret, links last until restart
    templates = make_templates(settings)

    def telegram_username() -> str:
        return _bot_username(bot_app)

    site = Site(settings, engine, vault, store, templates, secret=secret, telegram_username=telegram_username)
    console = Console(
        settings, engine, attach_console(engine), templates, secret=secret, telegram_username=telegram_username
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            if mcp is not None:
                await stack.enter_async_context(mcp[1])
            if bot_app is not None:
                await _start_bot(bot_app, settings, stack, app)
            yield

    app = FastAPI(
        title="HoldUp",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.telegram_ready = False
    app.state.site = site
    app.state.console = console
    app.add_middleware(SecurityHeaders)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        path = request.url.path
        if path.startswith(JSON_PREFIXES) or path.endswith((".json", ".csv")):
            return json_error(exc.status_code, str(exc.detail))
        message = str(exc.detail) if exc.status_code < 500 else "Something went wrong on our side."
        if exc.status_code == 404 and exc.detail == "Not Found":
            message = "There is nothing at this address."
        return templates.TemplateResponse(
            request, "error.html", {"status": exc.status_code, "message": message}, status_code=exc.status_code
        )

    async def telegram_webhook(secret_path: str, request: Request) -> Response:
        expected = settings.telegram_webhook_secret
        if bot_app is None or not expected or not hmac.compare_digest(secret_path.encode(), expected.encode()):
            raise HTTPException(404, "not found")
        header = request.headers.get("x-telegram-bot-api-secret-token")
        if header is not None and not hmac.compare_digest(header.encode(), expected.encode()):
            raise HTTPException(403, "wrong secret token")
        if not app.state.telegram_ready:
            raise HTTPException(503, "the bot is not running")
        try:
            update = Update.de_json(await request.json(), bot_app.bot)
        except Exception as exc:
            raise HTTPException(400, "not a Telegram update") from exc
        if update is None:
            raise HTTPException(400, "not a Telegram update")
        await bot_app.update_queue.put(update)
        return Response(status_code=200)

    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "network": settings.network,
            "vault": settings.vault_account,
            "telegram": bool(app.state.telegram_ready),
            "mcp": mcp is not None,
            "guardrail": guard_router is not None,
        }

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(site.router())
    app.include_router(console.router())
    app.include_router(fixtures_router(templates, settings))
    app.add_api_route("/tg/{secret_path}", telegram_webhook, methods=["POST"], include_in_schema=False)
    app.add_api_route("/health", health, methods=["GET"])
    if guard_router is not None:
        app.include_router(guard_router)
    if mcp is not None:
        endpoint = mcp[0]
        app.add_route("/mcp", endpoint, methods=["GET", "POST", "DELETE"])
        app.mount("/mcp", endpoint)
    return app
