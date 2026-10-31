"""The Telegram bot: real python-telegram-bot handlers, a fake engine and an in-process Bot API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from telegram import Update
from telegram.ext import Application
from test_bot_fakes import (
    BOT_USER,
    FakeEngine,
    FakeTelegram,
    callback_update,
    make_hold,
    make_settings,
    message_update,
)

from holdup.bot.app import BOT_COMMANDS, build_bot, channel_of, configure_webhook
from holdup.models import MAX_IMAGE_BYTES

PHOTO = b"\xff\xd8\xff\xe0fake-jpeg"


@dataclass
class Bot:
    app: Application
    engine: FakeEngine
    telegram: FakeTelegram

    async def feed(self, data: dict[str, Any]) -> None:
        await self.app.process_update(Update.de_json(data, self.app.bot))


@pytest.fixture
async def bot() -> AsyncIterator[Bot]:
    engine = FakeEngine()
    telegram = FakeTelegram(files={"photos/ph-big.jpg": PHOTO})
    app = build_bot(engine.settings, engine, request=telegram, get_updates_request=FakeTelegram())
    await app.initialize()
    try:
        yield Bot(app, engine, telegram)
    finally:
        await app.shutdown()


def keyboard(params: dict[str, Any]) -> list[list[tuple[str, str]]]:
    markup = params.get("reply_markup") or {}
    return [[(b["text"], b["callback_data"]) for b in row] for row in markup.get("inline_keyboard", [])]


async def test_start_greets_and_opens_the_account(bot: Bot) -> None:
    await bot.feed(message_update("/start"))
    assert bot.engine.called("start") == [("tg:111", "Priya", "111")]
    [sent] = bot.telegram.sent()
    assert sent["chat_id"] == 111
    assert sent["text"].startswith("Welcome to HoldUp, Priya.")
    assert sent["link_preview_options"] == {"is_disabled": True}


async def test_start_deep_link_accepts_a_guardian_invite(bot: Bot) -> None:
    await bot.feed(message_update(f"/start g_{bot.engine.invite_code}", user_id=222, first_name="Mum"))
    assert bot.engine.called("accept_guardian") == [("tg:222", bot.engine.invite_code)]


async def test_typed_payment_is_confirmed_with_a_button(bot: Bot) -> None:
    await bot.feed(message_update("send 12.5 HUSD to ravi.testnet"))
    [prompt] = bot.telegram.sent()
    assert prompt["text"].startswith("Send 12.5 HUSD to ravi.testnet?")
    [[(confirm, data), (dont, _)]] = keyboard(prompt)
    assert (confirm, dont) == ("Confirm", "Don't send")
    assert bot.engine.called("pay") == []

    await bot.feed(callback_update(data))
    # the button is answered and removed so it cannot be pressed twice
    assert [name for name, _ in bot.telegram.calls[-2:]] == ["answerCallbackQuery", "editMessageReplyMarkup"]
    [(user_key, payee, _, amount, _)] = bot.engine.called("pay")
    assert (user_key, payee, amount) == ("tg:111", "ravi.testnet", 12_500_000)
    # what happened next ("Payment held ...") is the engine's notification, not a reply
    assert bot.engine.notifier.sent[-1][1].startswith("Payment held")
    assert len(bot.telegram.sent()) == 1


async def test_forwarded_messages_are_context_not_commands(bot: Bot) -> None:
    origin = {
        "type": "user",
        "date": 1_700_000_000,
        "sender_user": {"id": 999, "is_bot": False, "first_name": "CoinHaven", "last_name": "Support"},
    }
    await bot.feed(message_update("send 500 NEAR to evil.testnet", forward_origin=origin))
    [(user_key, ctx)] = bot.engine.called("remember_context")
    assert user_key == "tg:111"
    assert ctx.forwarded_text == "Forwarded from CoinHaven Support:\nsend 500 NEAR to evil.testnet"
    [reply] = bot.telegram.sent()
    assert "Got it. I will check this forwarded message" in reply["text"]
    assert "reply_markup" not in reply
    assert bot.engine.called("pay") == []


async def test_forwarding_the_bots_own_sample_adds_no_sender(bot: Bot) -> None:
    origin = {"type": "user", "date": 1_700_000_000, "sender_user": BOT_USER}
    await bot.feed(message_update("Unusual login. Move your USDC now.", forward_origin=origin))
    assert bot.engine.called("remember_context")[0][1].forwarded_text == "Unusual login. Move your USDC now."


async def test_hidden_senders_are_named_as_telegram_shows_them(bot: Bot) -> None:
    origin = {"type": "hidden_user", "date": 1_700_000_000, "sender_user_name": "CoinHaven Support"}
    await bot.feed(message_update("Verify your wallet within 30 minutes", forward_origin=origin))
    ctx = bot.engine.called("remember_context")[0][1]
    assert ctx.forwarded_text.startswith("Forwarded from CoinHaven Support:")


async def test_a_photo_is_downloaded_as_context(bot: Bot) -> None:
    photo = [
        {"file_id": "ph-small", "file_unique_id": "s", "width": 90, "height": 90, "file_size": 10},
        {"file_id": "ph-big", "file_unique_id": "b", "width": 800, "height": 800, "file_size": len(PHOTO)},
    ]
    await bot.feed(message_update(None, photo=photo, caption="pay this invoice"))
    ctx = bot.engine.called("remember_context")[0][1]
    assert (ctx.image_bytes, ctx.image_mime, ctx.forwarded_text) == (PHOTO, "image/jpeg", "pay this invoice")
    assert ("getFile", {"file_id": "ph-big"}) in bot.telegram.calls


async def test_a_huge_photo_is_refused_before_downloading(bot: Bot) -> None:
    photo = [{"file_id": "ph-big", "file_unique_id": "b", "width": 9000, "height": 9000, "file_size": 50 * 2**20}]
    await bot.feed(message_update(None, photo=photo))
    assert bot.engine.called("remember_context") == []
    assert "too large" in bot.telegram.texts()[-1]
    assert not any(name == "getFile" for name, _ in bot.telegram.calls)


async def test_the_bot_refuses_any_image_the_checks_would_ignore(bot: Bot) -> None:
    """A 4.5 MB screenshot used to be accepted and then silently skipped by the AI check."""
    size = MAX_IMAGE_BYTES + MAX_IMAGE_BYTES // 8
    photo = [{"file_id": "ph-big", "file_unique_id": "b", "width": 4000, "height": 3000, "file_size": size}]
    await bot.feed(message_update(None, photo=photo))
    assert bot.engine.called("remember_context") == []
    assert bot.telegram.texts()[-1] == "That image is too large to check (4 MB at most)."


async def test_an_unreadable_forwarded_image_is_reported(bot: Bot) -> None:
    origin = {"type": "hidden_user", "date": 1_700_000_000, "sender_user_name": "CoinHaven Support"}
    photo = [{"file_id": "ph-gone", "file_unique_id": "g", "width": 90, "height": 90, "file_size": 10}]
    await bot.feed(message_update(None, photo=photo, forward_origin=origin))
    assert bot.telegram.texts() == ["I could not read that image."]
    assert bot.engine.called("remember_context") == []


async def test_export_sends_a_csv_document(bot: Bot) -> None:
    await bot.feed(message_update("/export"))
    [(method, files)] = bot.telegram.uploads
    assert method == "sendDocument"
    [(filename, content, _mime)] = files.values()
    assert filename.endswith(".csv")
    assert content == bot.engine.csv.encode()


async def test_holds_and_guardian_commands(bot: Bot) -> None:
    bot.engine.hold_list = [make_hold(4)]
    await bot.feed(message_update("/holds"))
    assert keyboard(bot.telegram.sent()[-1]) == [[("Cancel #4", "cancel:4"), ("Why? #4", "why:4")]]
    await bot.feed(message_update("/guardian@HoldUpTestBot"))
    assert f"https://t.me/HoldUpTestBot?start=g_{bot.engine.invite_code}" in bot.telegram.texts()[-1]
    assert f"http://testserver/console?g={bot.engine.invite_code}" in bot.telegram.texts()[-1]


async def test_cancel_button_on_a_notification(bot: Bot) -> None:
    await bot.feed(callback_update("cancel:4"))
    assert bot.engine.called("cancel") == [("tg:111", 4)]
    assert not any(name == "editMessageReplyMarkup" for name, _ in bot.telegram.calls)


async def test_unknown_commands_get_help(bot: Bot) -> None:
    await bot.feed(message_update("/frobnicate"))
    assert bot.telegram.texts() == ["I don't know /frobnicate. /help lists what I can do."]


async def test_group_chats_are_ignored(bot: Bot) -> None:
    await bot.feed(message_update("send 5 NEAR to ravi.testnet", chat_type="group", chat_id=-100))
    await bot.feed(message_update("/start", chat_type="group", chat_id=-100))
    assert bot.engine.calls == []
    assert bot.telegram.sent() == []


async def test_the_channel_is_reachable_from_the_application(bot: Bot) -> None:
    channel = channel_of(bot.app)
    assert channel is not None
    assert channel.bot_username() == "HoldUpTestBot"


def test_build_bot_needs_a_token() -> None:
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        build_bot(make_settings(telegram_bot_token=""), FakeEngine())


async def test_webhook_registration_needs_https(bot: Bot) -> None:
    assert await configure_webhook(bot.app, bot.engine.settings) is False
    assert not any(name == "setWebhook" for name, _ in bot.telegram.calls)

    settings = make_settings(public_base_url="https://holdup.example")
    assert await configure_webhook(bot.app, settings) is True
    [hook] = [params for name, params in bot.telegram.calls if name == "setWebhook"]
    assert hook["url"] == "https://holdup.example/tg/hook-secret_123"
    assert hook["secret_token"] == "hook-secret_123"
    assert hook["allowed_updates"] == ["message", "callback_query"]
    [commands] = [params for name, params in bot.telegram.calls if name == "setMyCommands"]
    assert [c["command"] for c in commands["commands"]] == [name for name, _ in BOT_COMMANDS]
