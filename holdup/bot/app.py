"""The HoldUp Telegram bot: python-telegram-bot in webhook mode, a plain chat with inline keyboards.

The web app receives Telegram's webhook calls (``POST /tg/<secret>``) and
hands each update to the ``Application`` built here. Every handler turns the
update into a :class:`~holdup.bot.dialog.Dialog` call and sends the replies
back; the engine's own notifications arrive through ``TelegramNotifier``.
Only private chats are served.
"""

from __future__ import annotations

import contextlib
import logging
import re
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
    Update,
)
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import BaseRequest

from ..config import Settings
from .dialog import Dialog, Reply
from .notify import keyboard, send_text, telegram_user_key

log = logging.getLogger(__name__)

CONCURRENT_UPDATES = 16
MAX_IMAGE_BYTES = 5 * 1024 * 1024
ONE_SHOT_BUTTONS = ("confirm:", "drop:", "sample:")
_SECRET_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,256}")

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


def build_bot(
    settings: Settings,
    engine: Any,
    *,
    request: BaseRequest | None = None,
    get_updates_request: BaseRequest | None = None,
) -> Application:
    """The bot ``Application`` without an updater: updates come from the web app's webhook.

    ``request`` replaces the HTTP client used for Bot API calls (tests pass an
    in-process fake).
    """
    if not settings.telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set")
    builder = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .updater(None)
        .concurrent_updates(CONCURRENT_UPDATES)
    )
    if request is not None:
        builder = builder.request(request).get_updates_request(get_updates_request or request)
    application = builder.build()
    TelegramChannel(settings, engine, application).install()
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
        allowed_updates=["message", "callback_query"],
    )
    await application.bot.set_my_commands([BotCommand(name, text) for name, text in BOT_COMMANDS])
    return True


def channel_of(application: Application) -> TelegramChannel | None:
    channel = application.bot_data.get("holdup")
    return channel if isinstance(channel, TelegramChannel) else None


class TelegramChannel:
    """Telegram handlers around one :class:`Dialog`."""

    def __init__(self, settings: Settings, engine: Any, application: Application) -> None:
        self.settings = settings
        self.application = application
        self.dialog = Dialog(settings, engine, invite_links=self.invite_links)

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
        app.add_handler(MessageHandler(own & filters.COMMAND, self.on_unknown_command))
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
        if base := self.settings.public_base_url:
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
        command = _command_name(update.effective_message.text or "")
        replies = await self.dialog.command(key, command, context.args or [], display_name=name, chat_ref=chat)
        await self.send(update, replies)

    async def on_unknown_command(self, update: Update, context: Context) -> None:
        command = _command_name(update.effective_message.text or "")
        await self.send(update, [Reply(f"I don't know /{command}. /help lists what I can do.")])

    async def on_text(self, update: Update, context: Context) -> None:
        key, name, chat = _who(update)
        text = update.effective_message.text or ""
        await self.send(update, await self.dialog.text(key, text, display_name=name, chat_ref=chat))

    async def on_forward(self, update: Update, context: Context) -> None:
        """A forwarded message is evidence about a payment, never a payment instruction."""
        key, name, chat = _who(update)
        message = update.effective_message
        text = message.text or message.caption
        image, mime, too_large = await self._download_image(message)
        if too_large:
            await self.send(update, [Reply("That image is too large to check (5 MB at most).")])
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
        message = update.effective_message
        image, mime, too_large = await self._download_image(message)
        if image is None:
            text = "That image is too large to check (5 MB at most)." if too_large else "I could not read that image."
            await self.send(update, [Reply(text)])
            return
        replies = await self.dialog.context(
            key, text=message.caption, image=image, image_mime=mime, forwarded=False, display_name=name, chat_ref=chat
        )
        await self.send(update, replies)

    async def on_button(self, update: Update, context: Context) -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        data = query.data or ""
        if data.startswith(ONE_SHOT_BUTTONS) and query.message is not None:
            with contextlib.suppress(TelegramError):
                await query.edit_message_reply_markup(reply_markup=None)
        user = query.from_user
        chat_id = query.message.chat.id if query.message is not None else user.id
        replies = await self.dialog.button(
            telegram_user_key(user.id), data, display_name=user.first_name or "", chat_ref=str(chat_id)
        )
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
            if reply.document is not None:
                document = InputFile(reply.document.content, filename=reply.document.filename)
                await bot.send_document(
                    chat_id, document, caption=reply.text[:1024], reply_markup=keyboard(reply.buttons)
                )
            else:
                await send_text(bot, chat_id, reply.text, reply.buttons)

    async def _download_image(self, message: Message) -> tuple[bytes | None, str | None, bool]:
        """(bytes, mime type, too_large) of the message's photo or image document."""
        if message.photo:
            item, mime = message.photo[-1], "image/jpeg"
        elif message.document is not None and (message.document.mime_type or "").startswith("image/"):
            item, mime = message.document, message.document.mime_type
        else:
            return None, None, False
        if item.file_size and item.file_size > MAX_IMAGE_BYTES:
            return None, None, True
        try:
            file = await item.get_file()
            data = bytes(await file.download_as_bytearray())
        except TelegramError as exc:
            log.warning("could not download an image: %s", exc)
            return None, None, False
        if len(data) > MAX_IMAGE_BYTES:
            return None, None, True
        return data, mime, False

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
