"""Notifiers: deliver the engine's messages to Telegram chats and route by channel.

User keys name their channel before the colon: ``tg:<telegram user id>`` for
the bot (a private chat has the user's id as its chat id) and
``web:<id>`` for the web console.
"""

from __future__ import annotations

import asyncio
import logging
import warnings
from collections.abc import Mapping, Sequence
from datetime import timedelta

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.warnings import PTBDeprecationWarning

from ..interfaces import Notifier

log = logging.getLogger(__name__)

Buttons = Sequence[Sequence[tuple[str, str]]]

TELEGRAM_PREFIX = "tg"
MESSAGE_LIMIT = 4096
MAX_RETRY_WAIT_S = 30.0
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


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
    """``Notifier`` for ``tg:`` users: sends the text with inline buttons."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def notify(self, user_key: str, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
        chat_id = telegram_chat_id(user_key)
        if chat_id is None:
            log.warning("TelegramNotifier got a non-Telegram user key %r", user_key)
            return
        await send_text(self.bot, chat_id, text, buttons)


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
