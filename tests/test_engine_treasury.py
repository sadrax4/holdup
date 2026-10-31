"""Onboarding pays from the treasury: refused politely when it cannot pay, refilled from the faucet on testnet."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from holdup.bot.dialog import Dialog
from holdup.chain.budget import YOCTO_PER_NEAR
from holdup.chain.errors import VaultError
from holdup.chain.fake import FakeVault, SimClock
from holdup.chain.faucet import FAUCET_URL
from holdup.chain.treasury import TREASURY_EMPTY, FaucetPolicy, Treasury
from holdup.config import offline_settings
from holdup.engine import Engine
from holdup.messages import error_text
from holdup.runtime import build_runtime
from holdup.store import Store

NEAR = YOCTO_PER_NEAR


class Quiet:
    async def notify(self, user_key: str, text: str, buttons: Any = None) -> None:
        pass


class NoRisk:
    async def assess(self, hold: Any, ctx: Any) -> Any:
        raise AssertionError("no payments here")


def engine_with_treasury(near: float, *, network: str = "offline") -> tuple[Engine, FakeVault]:
    clock = SimClock()
    settings = replace(offline_settings(), network=network, secret="s" * 32)
    vault = FakeVault.from_settings(settings, clock=clock, treasury_near=int(near * 1000) * NEAR // 1000)
    engine = Engine(settings, vault, NoRisk(), None, Store(":memory:", clock=clock), Quiet(), clock=clock)
    return engine, vault


async def test_onboarding_takes_what_it_costs_from_the_treasury() -> None:
    engine, vault = engine_with_treasury(10)
    cost = await engine.onboarding_cost()
    account, created = await engine.start("tg:1", "Priya")
    assert created and await vault.treasury_balance() == 10 * NEAR - cost.spent
    # 0.03 NEAR of gas in the account, 2 NEAR (offline demo money) plus the 0.01 NEAR fee in the vault.
    assert vault.external_balance(account.account_id) == 3 * NEAR // 100
    assert (await engine.balance("tg:1"))["near"] == 2 * NEAR
    # Existing users never need the treasury again.
    vault.model.world.near[vault.treasury_id] = 0
    assert (await engine.start("tg:1"))[1] is False


async def test_a_poor_treasury_refuses_new_users_before_sending_anything(caplog: pytest.LogCaptureFixture) -> None:
    engine, vault = engine_with_treasury(0.3)  # a user needs 0.03 + 0.01 + 2 NEAR offline, plus gas
    caplog.set_level(logging.WARNING, logger="holdup")
    with pytest.raises(VaultError) as info:
        await engine.start("tg:1", "Priya")
    assert info.value.code == TREASURY_EMPTY
    assert vault.transactions == [] and await vault.treasury_balance() == 3 * NEAR // 10
    assert not await vault.account_exists(engine.account_for("tg:1").account_id)
    assert "onboarding refused" in caplog.text


async def test_the_bot_answers_a_poor_treasury_with_a_friendly_message() -> None:
    engine, _ = engine_with_treasury(0.3)
    dialog = Dialog(engine.settings, engine, samples=[])
    [reply] = await dialog.start("tg:1", display_name="Priya")
    assert reply.text == error_text(TREASURY_EMPTY)
    assert "try again in about 10 minutes" in reply.text and "Traceback" not in reply.text
    [reply] = await dialog.text("tg:1", "send 1 HUSD to ravi.testnet")  # every action tries to onboard first
    assert reply.text == error_text(TREASURY_EMPTY)


def test_the_web_console_answers_a_poor_treasury_with_a_friendly_message() -> None:
    runtime = build_runtime(offline_settings(public_base_url="http://testserver"), clock=SimClock())
    fake = runtime.fake
    assert fake is not None
    fake.model.world.near[fake.treasury_id] = NEAR // 10
    with TestClient(runtime.app) as client:
        response = client.post("/console/api/start", json={"name": "Priya"})
    assert response.status_code == 200, response.text
    texts = [message["text"] for message in response.json()["messages"]]
    assert error_text(TREASURY_EMPTY) in texts


async def test_on_testnet_a_low_treasury_asks_the_faucet_for_the_treasury_only() -> None:
    engine, vault = engine_with_treasury(1.0, network="testnet")  # below 1.5 NEAR
    requests: list[dict[str, Any]] = []

    def faucet(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == FAUCET_URL
        requests.append(json.loads(request.content))
        vault.model.world.near[vault.treasury_id] += 5 * NEAR
        return httpx.Response(200, json={"success": True, "message": "Funded with 5 NEAR"})

    engine.treasury = Treasury(
        engine.settings, vault, engine.store, policy=FaucetPolicy(), clock=engine.clock,
        http=httpx.AsyncClient(transport=httpx.MockTransport(faucet)),
    )
    # Too little for the offline world's 2 NEAR of demo money: refused, and a top-up is on its way.
    with pytest.raises(VaultError):
        await engine.start("tg:1")
    await engine.treasury.wait_idle()
    assert requests == [{"accountId": engine.settings.root_account}]
    account, created = await engine.start("tg:1")
    assert created and account.account_id.endswith(f".{engine.settings.root_account}")
    # The keeper's ticks check the balance at most every ten minutes, and ask again only after that.
    await engine.tick()
    await engine.treasury.wait_idle()
    assert len(requests) == 1
