"""Fakes for the bot and web tests, and checks that they keep to the real contracts.

``FakeEngine`` answers like ``holdup.engine.Engine`` (the cross-module API the
bot and the web app call) and records every call. ``FakeTelegram`` is an
in-process Bot API for python-telegram-bot: nothing leaves the process. The
other bot and web test modules import these helpers from here.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import replace
from typing import Any

from telegram.request import BaseRequest, RequestData

from holdup.chain import errors as E
from holdup.chain.errors import VaultError
from holdup.config import Settings
from holdup.engine import Engine
from holdup.models import (
    NEAR,
    Hold,
    HoldStatus,
    PaymentContext,
    PaymentOutcome,
    TxResult,
    UserAccount,
    WhyView,
)

ROOT = "holdup.testnet"
HUSD = f"husd.{ROOT}"
NOW_MS = 1_793_610_000_000  # 2 Nov 2026 09:00 UTC
BOT_TOKEN = "123456:TEST-TOKEN"
WEBHOOK_SECRET = "hook-secret_123"

ENGINE_API = (
    "start",
    "balance",
    "pay",
    "cancel",
    "guardian_cancel",
    "trust",
    "untrust",
    "guardian_invite",
    "accept_guardian",
    "veto_guardian_change",
    "holds",
    "why",
    "export_csv",
    "stats",
    "remember_context",
    "account_for",
    "wait_idle",
)


def make_settings(**overrides: Any) -> Settings:
    settings = Settings(
        network="offline",
        root_account=ROOT,
        vault_account=f"vault.{ROOT}",
        oracle_accounts=[f"oracle.{ROOT}"],
        keeper_account=f"keeper.{ROOT}",
        secret="test-secret",
        husd_token=HUSD,
        telegram_bot_token=BOT_TOKEN,
        telegram_webhook_secret=WEBHOOK_SECRET,
        public_base_url="http://testserver",
        otari_mode="replay",
        database_path=":memory:",
    )
    settings.demo_funds = {NEAR: 2 * 10**24, HUSD: 2_000 * 10**6}
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, Any]] = []

    async def notify(self, user_key: str, text: str, buttons: Any = None) -> None:
        self.sent.append((user_key, text, buttons))


def make_hold(hold_id: int = 7, *, sender: str = "", status: HoldStatus = HoldStatus.HELD, **fields: Any) -> Hold:
    values: dict[str, Any] = {
        "id": hold_id,
        "sender": sender or f"usender.{ROOT}",
        "payee": "safe-wallet-77.testnet",
        "asset": HUSD,
        "amount": 50 * 10**6,
        "created_ms": NOW_MS,
        "release_at_ms": NOW_MS + 20_000,
        "status": status,
    }
    values.update(fields)
    return Hold(**values)


class FakeEngine:
    """Answers like ``holdup.engine.Engine`` and records each call as ``(name, args)``.

    Payments notify through ``self.notifier`` like the real engine does, so a
    notifier installed by the web app receives them. ``fail`` maps a method
    name to an exception that method raises.
    """

    def __init__(self, settings: Settings | None = None, store: Any = None) -> None:
        self.settings = settings or make_settings()
        self.store = store  # like the real engine, start() records users here when given
        self.notifier: Any = RecordingNotifier()
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fail: dict[str, BaseException] = {}
        self.users: dict[str, UserAccount] = {}
        self.contexts: dict[str, PaymentContext] = {}
        self.balances: dict[str, int] = {NEAR: 2 * 10**24, HUSD: 2_000 * 10**6}
        self.hold_list: list[Hold] = []
        self.why_views: dict[int, WhyView] = {}
        self.refusal: PaymentOutcome | None = None
        self.csv = "hold_id,payee,amount\n0,ravi.testnet,12.5\n"
        self.invite_code = "inviteCODE123"
        self.stats_value: dict[str, Any] = {
            "users": 3,
            "holds": 12,
            "instant": 2,
            "provisional_brakes": 4,
            "brakes": 5,
            "braked_holds": 6,
            "fail_closed": 1,
            "cancels": 4,
            "guardian_cancels": 1,
            "cancels_after_flag": 3,
            "releases": 6,
            "payout_failures": 0,
            "ai_checks": 9,
            "ai_steps": 31,
            "fallbacks": 2,
            "ai_cost_usd": 0.0213,
        }

    # ------------------------------------------------------------------
    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if name in self.fail:
            raise self.fail[name]

    def called(self, name: str) -> list[tuple[Any, ...]]:
        return [args for called, args in self.calls if called == name]

    # ------------------------------------------------------------------
    def account_for(self, user_key: str) -> UserAccount:
        if user_key in self.users:
            return self.users[user_key]
        digest = hashlib.sha256(user_key.encode()).hexdigest()[:10]
        return UserAccount(user_key, f"u{digest}.{ROOT}", "ed25519:fake")

    async def start(
        self, user_key: str, display_name: str = "", chat_ref: str | None = None
    ) -> tuple[UserAccount, bool]:
        self._record("start", user_key, display_name, chat_ref)
        created = user_key not in self.users
        account = replace(self.account_for(user_key), display_name=display_name)
        self.users[user_key] = account
        if self.store is not None:
            self.store.upsert_user(user_key, account.account_id, chat_ref, display_name)
        return account, created

    async def balance(self, user_key: str) -> dict[str, int]:
        self._record("balance", user_key)
        return dict(self.balances)

    async def pay(
        self, user_key: str, payee: str, asset_id: str, amount: int, ctx: PaymentContext | None = None
    ) -> PaymentOutcome:
        self._record("pay", user_key, payee, asset_id, amount, ctx)
        if self.refusal is not None:
            return self.refusal
        hold = make_hold(
            len(self.hold_list),
            sender=self.account_for(user_key).account_id,
            payee=payee,
            asset=asset_id,
            amount=amount,
        )
        self.hold_list.insert(0, hold)
        self.contexts.pop(user_key, None)
        text = f"Payment held: {amount} to {payee}."
        await self.notifier.notify(
            user_key, text, [[("Cancel & refund", f"cancel:{hold.id}"), ("Why?", f"why:{hold.id}")]]
        )
        return PaymentOutcome(hold=hold, tx=TxResult("tx-send"), message=text)

    async def cancel(self, user_key: str, hold_id: int) -> TxResult:
        self._record("cancel", user_key, hold_id)
        await self.notifier.notify(user_key, f"Cancelled. Payment #{hold_id} is back in your HoldUp balance.")
        return TxResult("tx-cancel")

    async def guardian_cancel(self, guardian_user_key: str, hold_id: int) -> TxResult:
        self._record("guardian_cancel", guardian_user_key, hold_id)
        return TxResult("tx-gcancel")

    async def trust(self, user_key: str, payee: str) -> TxResult:
        self._record("trust", user_key, payee)
        return TxResult("tx-trust")

    async def untrust(self, user_key: str, payee: str) -> TxResult:
        self._record("untrust", user_key, payee)
        return TxResult("tx-untrust")

    async def guardian_invite(self, user_key: str) -> str:
        self._record("guardian_invite", user_key)
        return self.invite_code

    async def accept_guardian(self, guardian_user_key: str, code: str) -> TxResult:
        self._record("accept_guardian", guardian_user_key, code)
        if code != self.invite_code:
            raise VaultError(E.INVALID_INVITE, "unknown invite")
        return TxResult("tx-guardian")

    async def veto_guardian_change(self, guardian_user_key: str, owner_account: str) -> TxResult:
        self._record("veto_guardian_change", guardian_user_key, owner_account)
        return TxResult("tx-veto")

    async def holds(self, user_key: str, limit: int = 20) -> list[Hold]:
        self._record("holds", user_key, limit)
        return self.hold_list[:limit]

    async def why(self, hold_id: int) -> WhyView | None:
        self._record("why", hold_id)
        return self.why_views.get(hold_id)

    async def export_csv(self, user_key: str) -> str:
        self._record("export_csv", user_key)
        return self.csv

    async def stats(self) -> dict[str, Any]:
        self._record("stats")
        return dict(self.stats_value)

    def remember_context(self, user_key: str, ctx: PaymentContext) -> None:
        self._record("remember_context", user_key, ctx)
        self.contexts[user_key] = ctx

    async def wait_idle(self) -> None:
        return None


BOT_USER = {"id": 4242, "is_bot": True, "first_name": "HoldUp", "username": "HoldUpTestBot"}


class FakeTelegram(BaseRequest):
    """An in-process Telegram Bot API: records every call and answers with plausible results."""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.uploads: list[tuple[str, dict[str, Any]]] = []
        self.files = files or {}
        self.errors: dict[str, tuple[int, dict[str, Any]]] = {}
        self._message_id = 1000

    @property
    def read_timeout(self) -> float:
        return 5.0

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def do_request(
        self, url: str, method: str, request_data: RequestData | None = None, **timeouts: Any
    ) -> tuple[int, bytes]:
        if "/file/bot" in url:
            path = url.split("/file/bot", 1)[1].split("/", 1)[1]
            return (200, self.files[path]) if path in self.files else (404, b"")
        name = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data is not None else {}
        self.calls.append((name, params))
        if request_data is not None and request_data.contains_files:
            self.uploads.append((name, request_data.multipart_data))
        if name in self.errors:
            status, body = self.errors[name]
            return status, json.dumps(body).encode()
        return 200, json.dumps({"ok": True, "result": self._result(name, params)}).encode()

    def _result(self, name: str, params: dict[str, Any]) -> Any:
        if name == "getMe":
            return BOT_USER
        if name in ("sendMessage", "sendDocument", "editMessageReplyMarkup"):
            self._message_id += 1
            message: dict[str, Any] = {
                "message_id": self._message_id,
                "date": 0,
                "chat": {"id": int(params.get("chat_id", 1)), "type": "private"},
            }
            if "text" in params:
                message["text"] = params["text"]
            return message
        if name == "getFile":
            file_id = params["file_id"]
            path = f"photos/{file_id}.jpg"
            return {
                "file_id": file_id,
                "file_unique_id": f"u{file_id}",
                "file_size": len(self.files.get(path, b"")),
                "file_path": path,
            }
        return True

    def sent(self, method: str = "sendMessage") -> list[dict[str, Any]]:
        return [params for name, params in self.calls if name == method]

    def texts(self) -> list[str]:
        return [str(params.get("text", "")) for params in self.sent()]


# ----------------------------------------------------------------------
# Telegram update builders
# ----------------------------------------------------------------------

_update_ids = iter(range(1, 1_000_000))


def tg_user(user_id: int = 111, first_name: str = "Priya") -> dict[str, Any]:
    return {"id": user_id, "is_bot": False, "first_name": first_name}


def message_update(
    text: str | None = None,
    *,
    user_id: int = 111,
    first_name: str = "Priya",
    chat_type: str = "private",
    chat_id: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": next(_update_ids),
        "date": 1_700_000_000,
        "chat": {"id": chat_id if chat_id is not None else user_id, "type": chat_type},
        "from": tg_user(user_id, first_name),
        **extra,
    }
    if text is not None:
        message["text"] = text
        if text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return {"update_id": next(_update_ids), "message": message}


def callback_update(data: str, *, user_id: int = 111, first_name: str = "Priya") -> dict[str, Any]:
    return {
        "update_id": next(_update_ids),
        "callback_query": {
            "id": f"cb{next(_update_ids)}",
            "from": tg_user(user_id, first_name),
            "chat_instance": "ci",
            "data": data,
            "message": {
                "message_id": 55,
                "date": 1_700_000_000,
                "chat": {"id": user_id, "type": "private"},
                "text": "prompt",
            },
        },
    }


# ----------------------------------------------------------------------
# the fakes keep to the real contracts
# ----------------------------------------------------------------------


def test_fake_engine_matches_the_engine_signatures() -> None:
    for name in ENGINE_API:
        real = inspect.signature(getattr(Engine, name))
        fake = inspect.signature(getattr(FakeEngine, name))
        assert list(real.parameters) == list(fake.parameters), name
        assert inspect.iscoroutinefunction(getattr(Engine, name)) == inspect.iscoroutinefunction(
            getattr(FakeEngine, name)
        ), name


async def test_fake_telegram_answers_get_me_and_records_calls() -> None:
    from telegram import Bot

    request = FakeTelegram()
    bot = Bot(BOT_TOKEN, request=request, get_updates_request=FakeTelegram())
    await bot.initialize()
    message = await bot.send_message(111, "hello")
    assert bot.username == "HoldUpTestBot"
    assert message.text == "hello"
    assert request.texts() == ["hello"]
