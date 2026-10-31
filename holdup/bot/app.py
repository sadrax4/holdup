"""The HoldUp Telegram bot: python-telegram-bot, a plain chat with inline keyboards.

The bot gets its updates in one of two ways (``TELEGRAM_MODE``):

- ``polling`` (the default): :class:`Poller` long-polls ``getUpdates`` from
  inside the web app's lifespan, so the server needs no inbound connection
  from Telegram;
- ``webhook``: the web app receives Telegram's webhook calls
  (``POST /tg/<secret>``) and hands each update to the ``Application``.

Every handler turns the update into a :class:`~holdup.bot.dialog.Dialog` call
and sends the replies back; the engine's own notifications arrive through
``TelegramNotifier``. Only private chats are served. :class:`BotStatus` is
what ``/health`` reports about the bot; the bot token never appears in it or
in the log lines written here.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import math
import re
import time
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from telegram import (
    BotCommand,
    InputFile,
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
    PhotoSize,
    Update,
)
from telegram import Document as TelegramDocument
from telegram.error import Conflict, InvalidToken, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import BaseRequest, HTTPXRequest, RequestData

from ..config import Settings
from ..models import MAX_IMAGE_BYTES
from .dialog import Dialog, Reply
from .notify import (
    PRACTICE_PAGE_OFFLINE,
    drop_links_to,
    keyboard,
    private_base_url,
    private_fixture_base,
    replace_links_to,
    retry_seconds,
    send_text,
    telegram_user_key,
)

log = logging.getLogger(__name__)

CONCURRENT_UPDATES = 16
IMAGE_TOO_LARGE = f"That image is too large to check ({MAX_IMAGE_BYTES // (1024 * 1024)} MB at most)."
ONE_SHOT_BUTTONS = ("confirm:", "drop:", "sample:")
_SECRET_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,256}")
_URL_QUERY = re.compile(r"(https?://[^\s?#]+)\?[^\s#]*")

# The update types the handlers below use; both the webhook and polling ask for exactly these.
ALLOWED_UPDATES = ("message", "callback_query")
POLL_TIMEOUT_S = 25  # how long Telegram may hold one getUpdates request open
BOOTSTRAP_RETRIES = 3  # PTB's own quick retries of deleteWebhook before start_polling gives up
START_RETRY_DELAYS_S = (5.0, 10.0, 30.0, 60.0)  # between attempts to bring the bot up; the last repeats
LOG_INTERVAL_S = 60.0  # repeated polling errors are logged at most this often
TOKEN_REJECTED = "Telegram rejected the bot token; check TELEGRAM_BOT_TOKEN"

BOT_COMMANDS = (
    ("start", "Open your HoldUp account"),
    ("help", "How to pay and every command"),
    ("balance", "Your balances"),
    ("holds", "Recent payments with Cancel and Why? buttons"),
    ("cancel", "Cancel a held payment: /cancel <id>"),
    ("why", "What the checks found: /why <id>"),
    ("trust", "Pay an account instantly: /trust <account>"),
    ("untrust", "Stop trusting an account"),
    ("guardian", "Invite someone who can cancel for you"),
    ("export", "Your payments as CSV"),
    ("prove", "Why HoldUp cannot move your money"),
    ("demo", "Practice messages with fictional brands"),
    ("stats", "What HoldUp has caught so far"),
    ("budget", "What the AI checks cost"),
)

Context = ContextTypes.DEFAULT_TYPE


# ----------------------------------------------------------------------
# status for /health
# ----------------------------------------------------------------------


@dataclass
class BotStatus:
    """What ``/health`` says about the bot. Error texts are redacted before they are stored."""

    mode: str  # "polling" or "webhook"
    running: bool = False
    username: str = ""
    last_error: str | None = None  # what is wrong now: cleared again once a poll works
    conflict_at: float | None = None  # clock() of the last 409 Conflict from getUpdates
    polled_at: float | None = None  # clock() of the last getUpdates that Telegram answered
    clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)

    @property
    def conflict(self) -> bool:
        """Another process took this token's updates, and no poll of ours has worked since."""
        return self.conflict_at is not None and (self.polled_at is None or self.conflict_at > self.polled_at)

    def poll_answered(self, status_code: int) -> None:
        """Called with the HTTP status of every getUpdates request."""
        if 200 <= status_code < 300:
            self.polled_at = self.clock()
            self.last_error = None
        elif status_code in (401, 404):  # PTB stops polling on these (InvalidToken)
            self.running = False
            self.last_error = TOKEN_REJECTED

    def as_dict(self) -> dict[str, Any]:
        last_poll = None if self.polled_at is None else round(self.clock() - self.polled_at)
        return {
            "mode": self.mode,
            "running": self.running,
            "username": self.username,
            "last_error": self.last_error,
            "conflict": self.conflict,
            "last_poll_s": last_poll,  # seconds since Telegram last answered a poll (polling only)
        }


def redact(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def describe_error(exc: BaseException, *secrets: str) -> str:
    """One line about ``exc`` for logs and the public /health page.

    ``secrets`` (the bot token, ...) are replaced, and so are URL query
    strings, where API keys can travel (``?apiKey=...``).
    """
    name = type(exc).__name__
    text = exc.message if isinstance(exc, TelegramError) else str(exc)
    if not text.startswith(name):  # Telegram's own texts start with it ("Conflict: ...")
        text = f"{name}: {text}" if text else name
    return _URL_QUERY.sub(r"\1?[redacted]", redact(text, *secrets))[:300]


class PollingRequest(BaseRequest):
    """The getUpdates HTTP client, wrapped to tell :class:`BotStatus` how each poll went.

    PTB only reports failed polls (to ``error_callback``); this is how /health
    learns that polling works again after a conflict or an outage.
    """

    def __init__(self, inner: BaseRequest, status: BotStatus) -> None:
        self.inner = inner
        self.status = status

    @property
    def read_timeout(self) -> float | None:
        return self.inner.read_timeout

    async def initialize(self) -> None:
        await self.inner.initialize()

    async def shutdown(self) -> None:
        await self.inner.shutdown()

    async def do_request(
        self,
        url: str,
        method: str,
        request_data: RequestData | None = None,
        read_timeout: Any = BaseRequest.DEFAULT_NONE,
        write_timeout: Any = BaseRequest.DEFAULT_NONE,
        connect_timeout: Any = BaseRequest.DEFAULT_NONE,
        pool_timeout: Any = BaseRequest.DEFAULT_NONE,
    ) -> tuple[int, bytes]:
        code, payload = await self.inner.do_request(
            url,
            method,
            request_data,
            read_timeout=read_timeout,
            write_timeout=write_timeout,
            connect_timeout=connect_timeout,
            pool_timeout=pool_timeout,
        )
        self.status.poll_answered(code)
        return code, payload


# ----------------------------------------------------------------------
# building and starting the bot
# ----------------------------------------------------------------------


def build_bot(
    settings: Settings,
    engine: Any,
    *,
    request: BaseRequest | None = None,
    get_updates_request: BaseRequest | None = None,
) -> Application:
    """The bot ``Application`` for ``settings.bot_mode()``.

    In ``webhook`` mode it has no updater (updates come from the web app's
    webhook route); in ``polling`` mode its updater is driven by
    :class:`Poller`. ``request`` and ``get_updates_request`` replace the HTTP
    clients (tests pass an in-process fake); ``settings.telegram_api_base_url``
    points the real clients at another Bot API server.
    """
    if not settings.telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set")
    mode = settings.bot_mode()
    if mode not in ("polling", "webhook"):
        raise ValueError(f"the bot needs TELEGRAM_MODE polling or webhook (got {mode!r})")
    status = BotStatus(mode)
    builder = Application.builder().token(settings.telegram_bot_token).concurrent_updates(CONCURRENT_UPDATES)
    if settings.telegram_api_base_url:
        root = settings.telegram_api_base_url.rstrip("/").removesuffix("/bot")
        builder = builder.base_url(f"{root}/bot").base_file_url(f"{root}/file/bot")
    if request is not None:
        builder = builder.request(request)
    if mode == "webhook":
        builder = builder.updater(None)
        if request is not None:
            builder = builder.get_updates_request(get_updates_request or request)
    else:
        inner = get_updates_request or request or HTTPXRequest(connection_pool_size=1)
        builder = builder.get_updates_request(PollingRequest(inner, status))
    application = builder.build()
    TelegramChannel(settings, engine, application, status).install()
    return application


async def configure_webhook(application: Application, settings: Settings) -> bool:
    """Point Telegram at ``<public url>/tg/<secret>`` and publish the command menu.

    Telegram only delivers webhooks over HTTPS, so nothing happens (False)
    without an ``https://`` public URL and a webhook secret.
    """
    base, secret = settings.public_base_url, settings.telegram_webhook_secret
    if not base.startswith("https://") or not secret:
        return False
    await application.bot.set_webhook(
        f"{base}/tg/{quote(secret, safe='')}",
        secret_token=secret if _SECRET_TOKEN.fullmatch(secret) else None,
        allowed_updates=list(ALLOWED_UPDATES),
    )
    await application.bot.set_my_commands([BotCommand(name, text) for name, text in BOT_COMMANDS])
    return True


def channel_of(application: Application) -> TelegramChannel | None:
    channel = application.bot_data.get("holdup")
    return channel if isinstance(channel, TelegramChannel) else None


class Poller:
    """Long polling inside another asyncio program (the web app), without ``run_polling``.

    ``start`` returns at once: the bot comes up in the background (initialize,
    start, then ``updater.start_polling``) and keeps retrying while Telegram
    cannot be reached, so the web app serves meanwhile. ``stop`` undoes it in
    reverse order. PTB's ``start_polling`` deletes a stale webhook first. The
    command menu is published once, as the webhook setup does.
    """

    def __init__(
        self,
        application: Application,
        status: BotStatus,
        *,
        retry_delays_s: Sequence[float] | None = None,
        poll_timeout_s: int = POLL_TIMEOUT_S,
    ) -> None:
        if application.updater is None:
            raise ValueError("a polling bot needs an updater (build it with TELEGRAM_MODE=polling)")
        self.application = application
        self.status = status
        self.retry_delays_s = tuple(retry_delays_s or START_RETRY_DELAYS_S)
        self.poll_timeout_s = poll_timeout_s
        self._task: asyncio.Task[None] | None = None
        self._initialized = False
        self._logged_at: dict[str, float] = {}

    def start(self) -> None:
        self._task = asyncio.create_task(self._bring_up(), name="holdup-telegram-start")

    async def stop(self) -> None:
        """Stop polling, then the application, then shut it down; safe at any stage of starting."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.status.running = False
        app, updater = self.application, self.application.updater
        if updater is not None and updater.running:
            await self._quietly("stop polling", updater.stop())
        if app.running:
            await self._quietly("stop the bot", app.stop())
        if self._initialized:
            await self._quietly("shut the bot down", app.shutdown())
            self._initialized = False
        else:  # initialize failed half-way: close the HTTP clients it opened
            await self._quietly("close the bot's connections", app.bot.shutdown())

    def on_error(self, exc: TelegramError) -> None:
        """``error_callback`` of the polling loop. It must not raise: that would end polling."""
        try:
            self.status.last_error = describe_error(exc, self.application.bot.token)
            if isinstance(exc, Conflict):
                self.status.conflict_at = self.status.clock()
                if "webhook" in exc.message.lower():
                    self._log_now_and_then(
                        "conflict",
                        logging.ERROR,
                        "Telegram: a webhook was set for this bot token, so polling gets nothing; "
                        "stop whatever set it (restarting HoldUp removes the webhook)",
                    )
                else:
                    self._log_now_and_then(
                        "conflict", logging.ERROR, "Telegram: another process is polling this bot token; stop it"
                    )
                return
            self._log_now_and_then("error", logging.WARNING, f"Telegram polling failed, retrying: {self.status.last_error}")
        except Exception:
            log.exception("could not record a Telegram polling error")

    # ------------------------------------------------------------------
    async def _bring_up(self) -> None:
        token = self.application.bot.token
        for attempt in itertools.count():
            delay = self.retry_delays_s[min(attempt, len(self.retry_delays_s) - 1)]
            try:
                await self._start_once()
                return
            except InvalidToken:  # its message carries the token: never log it
                self.status.last_error = TOKEN_REJECTED
                log.error("%s; the bot stays off until HoldUp restarts", TOKEN_REJECTED)
                return
            except TelegramError as exc:  # Telegram unreachable, slow or refusing for now
                self.status.last_error = describe_error(exc, token)
                if isinstance(exc, RetryAfter):
                    delay = max(delay, retry_seconds(exc))
                log.warning("Telegram bot not started (%s); trying again in %.0f s", self.status.last_error, delay)
            except Exception as exc:  # not Telegram's doing: keep the traceback
                self.status.last_error = describe_error(exc, token)
                log.exception("Telegram bot not started; trying again in %.0f s", delay)
            await asyncio.sleep(delay)

    async def _start_once(self) -> None:
        app, updater = self.application, self.application.updater
        assert updater is not None
        if not self._initialized:
            await app.initialize()  # getMe: checks the token and learns the bot's username
            self._initialized = True
            self.status.username = app.bot.username or ""
            commands = [BotCommand(name, text) for name, text in BOT_COMMANDS]
            await self._quietly("publish the command menu", app.bot.set_my_commands(commands))
        if not app.running:
            await app.start()
        await updater.start_polling(
            timeout=self.poll_timeout_s,
            bootstrap_retries=BOOTSTRAP_RETRIES,
            allowed_updates=list(ALLOWED_UPDATES),
            drop_pending_updates=False,
            error_callback=self.on_error,
        )
        self.status.running = True
        self.status.last_error = None
        log.info("Telegram bot @%s is polling for updates", self.status.username)

    def _log_now_and_then(self, kind: str, level: int, message: str) -> None:
        now = self.status.clock()
        last = self._logged_at.get(kind)
        if last is None or now - last >= LOG_INTERVAL_S:
            self._logged_at[kind] = now
            log.log(level, message)

    async def _quietly(self, what: str, step: Coroutine[Any, Any, Any]) -> None:
        """Run one step of starting or stopping whose failure must not stop the rest."""
        try:
            await step
        except TelegramError as exc:
            log.warning("could not %s: %s", what, describe_error(exc, self.application.bot.token))
        except Exception:
            log.exception("could not %s", what)


class TelegramChannel:
    """Telegram handlers around one :class:`Dialog`."""

    def __init__(
        self, settings: Settings, engine: Any, application: Application, status: BotStatus | None = None
    ) -> None:
        self.settings = settings
        self.application = application
        self.status = status or BotStatus(settings.bot_mode())
        self.dialog = Dialog(settings, engine, invite_links=self.invite_links)

    @property
    def private_base_url(self) -> str:
        """The web pages' base URL when only this machine can open it; links to it are left out."""
        return private_base_url(self.settings)

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------

    def install(self) -> None:
        private = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE
        own = private & ~filters.FORWARDED
        commands = [name for name in self.dialog.command_names if name != "start"]
        app = self.application
        app.add_handler(MessageHandler(private & filters.FORWARDED, self.on_forward))
        app.add_handler(CommandHandler("start", self.on_start, filters=own))
        app.add_handler(CommandHandler(commands, self.on_command, filters=own))
        app.add_handler(MessageHandler(own & (filters.PHOTO | filters.Document.IMAGE), self.on_image))
        app.add_handler(MessageHandler(own & filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_handler(MessageHandler(own & filters.COMMAND, self.on_command))  # unknown commands get help
        app.add_handler(CallbackQueryHandler(self.on_button))
        app.add_error_handler(self.on_error)
        app.bot_data["holdup"] = self

    def bot_username(self) -> str:
        try:
            return self.application.bot.username or ""
        except RuntimeError:  # not initialized yet: no getMe answer to read it from
            return ""

    def invite_links(self, code: str) -> list[tuple[str, str]]:
        links = []
        if username := self.bot_username():
            links.append(("Telegram", f"https://t.me/{username}?start=g_{code}"))
        if (base := self.settings.public_base_url) and not self.private_base_url:
            links.append(("Web console", f"{base}/console?g={code}"))
        return links

    # ------------------------------------------------------------------
    # handlers
    # ------------------------------------------------------------------

    async def on_start(self, update: Update, context: Context) -> None:
        key, name, chat = _who(update)
        payload = context.args[0] if context.args else ""
        await self.send(update, await self.dialog.start(key, display_name=name, chat_ref=chat, payload=payload))

    async def on_command(self, update: Update, context: Context) -> None:
        key, name, chat = _who(update)
        command = _command_name(_message(update).text or "")
        replies = await self.dialog.command(key, command, context.args or [], display_name=name, chat_ref=chat)
        await self.send(update, replies)

    async def on_text(self, update: Update, context: Context) -> None:
        key, name, chat = _who(update)
        text = _message(update).text or ""
        await self.send(update, await self.dialog.text(key, text, display_name=name, chat_ref=chat))

    async def on_forward(self, update: Update, context: Context) -> None:
        """A forwarded message is evidence about a payment, never a payment instruction."""
        key, name, chat = _who(update)
        message = _message(update)
        text = message.text or message.caption
        image, mime, problem = await self._download_image(message)
        if problem:
            await self.send(update, [Reply(problem)])
            if not text:
                return
        replies = await self.dialog.context(
            key,
            text=text,
            image=image,
            image_mime=mime,
            sender=self._origin_name(message),
            forwarded=True,
            display_name=name,
            chat_ref=chat,
        )
        await self.send(update, replies)

    async def on_image(self, update: Update, context: Context) -> None:
        key, name, chat = _who(update)
        message = _message(update)
        image, mime, problem = await self._download_image(message)
        if image is None:
            await self.send(update, [Reply(problem or "I could not read that image.")])
            return
        replies = await self.dialog.context(
            key, text=message.caption, image=image, image_mime=mime, forwarded=False, display_name=name, chat_ref=chat
        )
        await self.send(update, replies)

    async def on_button(self, update: Update, context: Context) -> None:
        query = update.callback_query
        if query is None:
            return
        user = query.from_user
        key = telegram_user_key(user.id)
        wait = self.dialog.busy_for(key)
        if wait > 0:
            # Refused before anything changes: the buttons stay, and the tap still gets an answer.
            await query.answer(f"Slow down: try again in {math.ceil(wait)} s.")
            return
        await query.answer()
        data = query.data or ""
        if data.startswith(ONE_SHOT_BUTTONS) and query.message is not None:
            with contextlib.suppress(TelegramError):
                await query.edit_message_reply_markup(reply_markup=None)
        chat_id = query.message.chat.id if query.message is not None else user.id
        replies = await self.dialog.button(key, data, display_name=user.first_name or "", chat_ref=str(chat_id))
        await self.send_to(chat_id, replies)

    async def on_error(self, update: object, context: Context) -> None:
        log.error("Telegram update failed", exc_info=context.error)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    async def send(self, update: Update, replies: list[Reply]) -> None:
        if update.effective_chat is not None:
            await self.send_to(update.effective_chat.id, replies)

    async def send_to(self, chat_id: int, replies: list[Reply]) -> None:
        bot = self.application.bot
        for reply in replies:
            if reply.kind == "sample":
                # A training sample is shown as written, to be forwarded back; only a link to a
                # practice page nobody else can open yet (no public address) would be dead on a phone.
                text = replace_links_to(reply.text, private_fixture_base(self.settings), PRACTICE_PAGE_OFFLINE)
            else:
                text = drop_links_to(reply.text, self.private_base_url)
            if reply.document is not None:
                document = InputFile(reply.document.content, filename=reply.document.filename)
                await bot.send_document(chat_id, document, caption=text[:1024], reply_markup=keyboard(reply.buttons))
            else:
                await send_text(bot, chat_id, text, reply.buttons)

    async def _download_image(self, message: Message) -> tuple[bytes | None, str | None, str]:
        """(bytes, mime type, problem) for the message's photo or image document.

        Without an image all three are empty; ``problem`` explains a download that failed.
        """
        item: PhotoSize | TelegramDocument
        if message.photo:
            item, mime = message.photo[-1], "image/jpeg"
        elif message.document is not None and (message.document.mime_type or "").startswith("image/"):
            item, mime = message.document, message.document.mime_type or "image/jpeg"
        else:
            return None, None, ""
        if item.file_size and item.file_size > MAX_IMAGE_BYTES:
            return None, None, IMAGE_TOO_LARGE
        try:
            file = await item.get_file()
            data = bytes(await file.download_as_bytearray())
        except TelegramError as exc:
            log.warning("could not download an image: %s", exc)
            return None, None, "I could not read that image."
        if len(data) > MAX_IMAGE_BYTES:
            return None, None, IMAGE_TOO_LARGE
        return data, mime, ""

    def _origin_name(self, message: Message) -> str:
        """Who the forwarded message came from, as Telegram shows it (empty for our own messages)."""
        origin = message.forward_origin
        if isinstance(origin, MessageOriginUser):
            with contextlib.suppress(RuntimeError):
                if origin.sender_user.id == self.application.bot.id:
                    return ""
            return origin.sender_user.full_name
        if isinstance(origin, MessageOriginHiddenUser):
            return origin.sender_user_name
        if isinstance(origin, MessageOriginChat):
            return origin.sender_chat.title or ""
        if isinstance(origin, MessageOriginChannel):
            return origin.chat.title or ""
        return ""


def _message(update: Update) -> Message:
    """The message of a message update (message handlers only see updates that have one)."""
    if update.effective_message is None:
        raise ValueError("update without a message")
    return update.effective_message


def _who(update: Update) -> tuple[str, str, str]:
    """(user key, first name, chat id) of the person behind an update."""
    user, chat = update.effective_user, update.effective_chat
    if user is None or chat is None:
        raise ValueError("update without a user or chat")
    return telegram_user_key(user.id), user.first_name or "", str(chat.id)


def _command_name(text: str) -> str:
    """``/why@HoldUpBot 12`` -> ``why``."""
    first = text.split(maxsplit=1)[0] if text.strip() else ""
    return first.lstrip("/").split("@", 1)[0].lower()
