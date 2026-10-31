"""Notifiers: deliver the engine's messages to Telegram chats and route by channel.

User keys name their channel before the colon: ``tg:<telegram user id>`` for
the bot (a private chat has the user's id as its chat id) and
``web:<id>`` for the web console.

When the web pages are not public (``PUBLIC_BASE_URL`` is a loopback or LAN
address), links to them would be dead on the person's phone, so Telegram
messages leave them out and say so instead.
"""

from __future__ import annotations

import asyncio
import logging
import re
import warnings
from collections.abc import Mapping, Sequence
from datetime import timedelta

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.warnings import PTBDeprecationWarning

from ..config import Settings, is_public_url
from ..interfaces import Notifier

log = logging.getLogger(__name__)

Buttons = Sequence[Sequence[tuple[str, str]]]

TELEGRAM_PREFIX = "tg"
MESSAGE_LIMIT = 4096
MAX_RETRY_WAIT_S = 30.0
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
NOT_PUBLIC_NOTE = "Links to this server's web pages are left out: they are not public."
PRACTICE_PAGE_OFFLINE = "(the practice page is offline for a moment)"


def channel_of(user_key: str) -> str:
    return user_key.split(":", 1)[0] if ":" in user_key else ""


def telegram_user_key(user_id: int) -> str:
    return f"{TELEGRAM_PREFIX}:{user_id}"


def telegram_chat_id(user_key: str) -> int | None:
    """The private chat of a ``tg:<id>`` user key, or None for other channels."""
    channel, _, ident = user_key.partition(":")
    if channel != TELEGRAM_PREFIX or not ident.lstrip("-").isdigit():
        return None
    return int(ident)


def keyboard(buttons: Buttons | None) -> InlineKeyboardMarkup | None:
    """Rows of ``(label, callback_data)`` as an inline keyboard."""
    rows = [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in buttons or [] if row]
    return InlineKeyboardMarkup(rows) if rows else None


def split_text(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split a long message at line breaks so each part fits in one Telegram message."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            parts.append(current)
            current = ""
        current += line
    if current:
        parts.append(current)
    return [part.rstrip("\n") for part in parts if part.strip()]


def private_base_url(settings: Settings) -> str:
    """``public_base_url`` when nobody else can open it (loopback or LAN), else ""."""
    return "" if settings.web_is_public() else settings.public_base_url


def private_fixture_base(settings: Settings) -> str:
    """The training pages' base URL when nobody else can open it (no public address yet), else ""."""
    base = settings.fixture_base()
    return "" if is_public_url(base) else base


def replace_links_to(text: str, base_url: str, replacement: str) -> str:
    """Put ``replacement`` where ``text`` links to ``base_url``, keeping the rest of the sentence."""
    if not base_url:
        return text
    link = re.compile(re.escape(base_url) + r"(?:[/?#]\S*?)?(?=[.,;:!?)]*(?:\s|$))")
    return link.sub(replacement, text)


def drop_links_to(text: str, base_url: str) -> str:
    """Leave out the lines that link to ``base_url`` and add one line saying why.

    HoldUp's texts put each link to its own pages on a line of its own
    (``Details: <url>``), so nothing else is lost. An empty ``base_url`` keeps
    the text as it is.
    """
    if not base_url:
        return text
    link = re.compile(re.escape(base_url) + r"(?=[/?#\s]|$)")
    lines = text.split("\n")
    kept = [line for line in lines if not link.search(line)]
    if len(kept) == len(lines):
        return text
    return "\n".join([*kept, NOT_PUBLIC_NOTE])


def retry_seconds(exc: RetryAfter) -> float:
    """How long Telegram asked us to wait (an int today, a timedelta in a future PTB release)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PTBDeprecationWarning)
        value = exc.retry_after
    return value.total_seconds() if isinstance(value, timedelta) else float(value)


async def send_text(bot: Bot, chat_id: int, text: str, buttons: Buttons | None = None) -> None:
    """Send ``text`` (split if needed) with the buttons under its last part.

    Waits once when Telegram asks to slow down. A chat that blocked the bot is
    logged and skipped; other errors propagate to the caller.
    """
    parts = split_text(text)
    for index, part in enumerate(parts):
        markup = keyboard(buttons) if index == len(parts) - 1 else None
        for attempt in (1, 2):
            try:
                await bot.send_message(chat_id, part, reply_markup=markup, link_preview_options=NO_PREVIEW)
                break
            except RetryAfter as exc:
                if attempt == 2:
                    raise
                await asyncio.sleep(min(retry_seconds(exc), MAX_RETRY_WAIT_S))
            except Forbidden:
                log.info("chat %s blocked the bot; message dropped", chat_id)
                return
            except BadRequest as exc:
                log.warning("Telegram refused a message to chat %s: %s", chat_id, exc.message)
                return


class TelegramNotifier:
    """``Notifier`` for ``tg:`` users: sends the text with inline buttons.

    Links under ``private_base_url`` (see :func:`private_base_url`) are left out.
    """

    def __init__(self, bot: Bot, *, private_base_url: str = "") -> None:
        self.bot = bot
        self.private_base_url = private_base_url

    async def notify(self, user_key: str, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
        chat_id = telegram_chat_id(user_key)
        if chat_id is None:
            log.warning("TelegramNotifier got a non-Telegram user key %r", user_key)
            return
        await send_text(self.bot, chat_id, drop_links_to(text, self.private_base_url), buttons)


class RoutingNotifier:
    """Sends each notification to the notifier of the user key's channel (the part before ':').

    Keys of other channels go to ``default`` when one is given, else they are
    logged and dropped.
    """

    def __init__(self, routes: Mapping[str, Notifier], default: Notifier | None = None) -> None:
        self.routes = dict(routes)
        self.default = default

    def route(self, channel: str) -> Notifier | None:
        return self.routes.get(channel)

    async def notify(self, user_key: str, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
        target = self.routes.get(channel_of(user_key), self.default)
        if target is None:
            log.warning("no notifier for %r; message dropped", channel_of(user_key) or user_key)
            return
        await target.notify(user_key, text, buttons)
