"""Delivering engine notifications: Telegram sending, splitting, errors and channel routing."""

from __future__ import annotations

import pytest
from telegram import Bot
from test_bot_fakes import BOT_TOKEN, FakeTelegram, RecordingNotifier, make_settings

from holdup.bot import notify
from holdup.bot.notify import (
    MESSAGE_LIMIT,
    NOT_PUBLIC_NOTE,
    RoutingNotifier,
    TelegramNotifier,
    channel_of,
    drop_links_to,
    keyboard,
    private_base_url,
    private_fixture_base,
    replace_links_to,
    split_text,
    telegram_chat_id,
)

BUTTONS = [[("Cancel & refund", "cancel:7"), ("Why?", "why:7")]]


@pytest.fixture
async def telegram() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
async def bot(telegram: FakeTelegram) -> Bot:
    bot = Bot(BOT_TOKEN, request=telegram, get_updates_request=FakeTelegram())
    await bot.initialize()
    return bot


@pytest.mark.parametrize(
    ("user_key", "chat_id"),
    [("tg:111", 111), ("tg:-100123", -100123), ("web:abc", None), ("tg:abc", None), ("111", None)],
)
def test_telegram_chat_id(user_key: str, chat_id: int | None) -> None:
    assert telegram_chat_id(user_key) == chat_id


def test_channel_of() -> None:
    assert channel_of("tg:1") == "tg"
    assert channel_of("web:x:y") == "web"
    assert channel_of("nochannel") == ""


def test_keyboard_rows() -> None:
    markup = keyboard(BUTTONS)
    assert markup is not None
    assert [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard] == BUTTONS
    assert keyboard(None) is None
    assert keyboard([[]]) is None


def test_split_text_keeps_short_messages_whole() -> None:
    assert split_text("Brakes on\n- reason") == ["Brakes on\n- reason"]


def test_split_text_breaks_long_messages_at_lines() -> None:
    lines = [f"- reason {n:04d} " + "x" * 80 for n in range(120)]
    parts = split_text("\n".join(lines))
    assert len(parts) > 1
    assert all(len(part) <= MESSAGE_LIMIT for part in parts)
    assert "\n".join(parts) == "\n".join(lines)


def test_split_text_cuts_a_single_huge_line() -> None:
    parts = split_text("y" * (MESSAGE_LIMIT * 2 + 10))
    assert [len(p) for p in parts] == [MESSAGE_LIMIT, MESSAGE_LIMIT, 10]


async def test_telegram_notifier_sends_text_and_buttons(bot: Bot, telegram: FakeTelegram) -> None:
    await TelegramNotifier(bot).notify("tg:111", "Brakes on - held until 10:00 UTC", BUTTONS)
    [sent] = telegram.sent()
    assert sent["chat_id"] == 111
    assert sent["text"] == "Brakes on - held until 10:00 UTC"
    assert sent["reply_markup"]["inline_keyboard"][0][0] == {"text": "Cancel & refund", "callback_data": "cancel:7"}


async def test_buttons_go_under_the_last_part(bot: Bot, telegram: FakeTelegram) -> None:
    text = "\n".join("line " + "z" * 200 for _ in range(40))
    await TelegramNotifier(bot).notify("tg:111", text, BUTTONS)
    sent = telegram.sent()
    assert len(sent) == 3
    assert ["reply_markup" in part for part in sent] == [False, False, True]
    assert "\n".join(part["text"] for part in sent) == text


async def test_other_channels_are_not_sent_to_telegram(bot: Bot, telegram: FakeTelegram) -> None:
    await TelegramNotifier(bot).notify("web:abc", "hello")
    assert telegram.sent() == []


async def test_a_blocked_chat_is_skipped(bot: Bot, telegram: FakeTelegram) -> None:
    telegram.errors["sendMessage"] = (
        403,
        {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"},
    )
    await TelegramNotifier(bot).notify("tg:111", "Released")  # no exception


@pytest.mark.filterwarnings("ignore::telegram.warnings.PTBDeprecationWarning")  # raised inside RetryAfter itself
async def test_flood_control_waits_and_retries(
    bot: Bot, telegram: FakeTelegram, monkeypatch: pytest.MonkeyPatch
) -> None:
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)
        telegram.errors.pop("sendMessage", None)

    monkeypatch.setattr(notify.asyncio, "sleep", fake_sleep)
    telegram.errors["sendMessage"] = (
        429,
        {"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 3}},
    )
    await TelegramNotifier(bot).notify("tg:111", "Released")
    assert waits == [3.0]
    assert len(telegram.sent()) == 2  # the refused attempt and the retry


async def test_routing_by_channel_prefix() -> None:
    telegram, web, fallback = RecordingNotifier(), RecordingNotifier(), RecordingNotifier()
    router = RoutingNotifier({"tg": telegram, "web": web}, default=fallback)
    await router.notify("tg:1", "a")
    await router.notify("web:x", "b", BUTTONS)
    await router.notify("cli:me", "c")
    assert telegram.sent == [("tg:1", "a", None)]
    assert web.sent == [("web:x", "b", BUTTONS)]
    assert fallback.sent == [("cli:me", "c", None)]
    assert router.route("web") is web


async def test_routing_without_a_default_drops_unknown_channels(caplog: pytest.LogCaptureFixture) -> None:
    router = RoutingNotifier({"tg": RecordingNotifier()})
    await router.notify("sms:+1", "hello")
    assert "no notifier for 'sms'" in caplog.text


# ----------------------------------------------------------------------
# links to a web view that only this machine can open
# ----------------------------------------------------------------------

LOCAL = "http://127.0.0.1:8000"


def test_lines_with_private_links_are_left_out_and_explained() -> None:
    text = f"Brakes on - held until 10:00 UTC\n- the payee is two hours old\nDetails: {LOCAL}/h/7"
    assert drop_links_to(text, LOCAL) == (
        f"Brakes on - held until 10:00 UTC\n- the payee is two hours old\n{NOT_PUBLIC_NOTE}"
    )
    assert drop_links_to(f"Timeline: {LOCAL}/h/7\nEvery AI step: {LOCAL}/why/7", LOCAL) == NOT_PUBLIC_NOTE
    assert drop_links_to(f"Open {LOCAL}", LOCAL) == NOT_PUBLIC_NOTE


def test_other_links_are_kept() -> None:
    text = "tx: https://testnet.nearblocks.io/txns/abc\nhttp://127.0.0.1:80001/h/7"
    assert drop_links_to(text, LOCAL) == text  # another port is another address
    assert drop_links_to(f"Details: {LOCAL}/h/7", "") == f"Details: {LOCAL}/h/7"


def test_a_private_practice_link_is_replaced_in_place() -> None:
    text = f"Verify here: {LOCAL}/fixtures/coinhaven/verify. Act now!\nOr {LOCAL}/fixtures/x?ref=1, {LOCAL}"
    assert replace_links_to(text, LOCAL, "(offline)") == "Verify here: (offline). Act now!\nOr (offline), (offline)"
    assert replace_links_to(f"{LOCAL}0/fixtures/a", LOCAL, "(offline)") == f"{LOCAL}0/fixtures/a"  # another port
    assert replace_links_to(text, "", "(offline)") == text
    assert private_fixture_base(make_settings(fixture_base_url="http://localhost:47811")) == "http://localhost:47811"
    assert private_fixture_base(make_settings(fixture_base_url="https://a-b.trycloudflare.com")) == ""


def test_only_a_private_base_url_is_held_back() -> None:
    assert private_base_url(make_settings(public_base_url=LOCAL)) == LOCAL
    assert private_base_url(make_settings(public_base_url="https://holdup.example")) == ""


async def test_the_notifier_leaves_out_private_links(bot: Bot, telegram: FakeTelegram) -> None:
    await TelegramNotifier(bot, private_base_url=LOCAL).notify("tg:111", f"Released.\nDetails: {LOCAL}/h/7", BUTTONS)
    await TelegramNotifier(bot).notify("tg:111", "Released.\nDetails: https://holdup.example/h/7")
    first, second = telegram.sent()
    assert first["text"] == f"Released.\n{NOT_PUBLIC_NOTE}" and "reply_markup" in first
    assert second["text"] == "Released.\nDetails: https://holdup.example/h/7"
