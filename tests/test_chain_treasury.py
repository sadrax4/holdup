"""The treasury's faucet top-ups: testnet only, the treasury only, rate-limited, never blocking."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from holdup.chain.budget import YOCTO_PER_NEAR, OnboardingCost, TxCost
from holdup.chain.errors import VaultError
from holdup.chain.faucet import (
    FAUCET_URL,
    FaucetReply,
    faucet_account_ok,
    request_drop,
    request_drop_async,
)
from holdup.chain.treasury import FAUCET_EVENT, TREASURY_EMPTY, FaucetPolicy, Treasury
from holdup.config import Settings
from holdup.store import Store

NEAR = YOCTO_PER_NEAR
MINUTE = 60_000
ROOT = "holdup.testnet"
# A new user needs about 0.35 NEAR at hand (see test_chain_budget).
COST = OnboardingCost((TxCost(deposits=30 * NEAR // 100, upfront=NEAR // 10, burnt=NEAR // 20),))


class Clock:
    def __init__(self) -> None:
        self.now = 1_793_610_000_000

    def __call__(self) -> int:
        return self.now


class Vault:
    """Only what the treasury reads: its balance."""

    def __init__(self, near: float) -> None:
        self.balance = int(Decimal(str(near)) * NEAR)
        self.reads = 0

    async def treasury_balance(self) -> int:
        self.reads += 1
        return self.balance


class Faucet:
    """The faucet API: records each request and answers like the documented one."""

    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = status
        self.body = body if body is not None else {"success": True, "message": "Funded with 5 NEAR"}

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == FAUCET_URL and request.method == "POST"
        self.requests.append(json.loads(request.content))
        return httpx.Response(self.status, json=self.body)


def treasury(
    near: float, *, network: str = "testnet", root: str = ROOT, **policy: Any
) -> tuple[Treasury, Vault, Faucet, Clock]:
    vault, faucet, clock = Vault(near), Faucet(), Clock()
    http = httpx.AsyncClient(transport=httpx.MockTransport(faucet.handle))
    settings = Settings(network=network, root_account=root)
    found = Treasury(settings, vault, Store(":memory:"), policy=FaucetPolicy(**policy), clock=clock, http=http)
    return found, vault, faucet, clock


async def test_a_low_treasury_asks_the_faucet_once_in_the_background() -> None:
    found, _, faucet, _ = treasury(1.0)  # below 1.5 NEAR, but still enough for one more user
    await found.check_onboarding(COST)
    await found.wait_idle()
    assert faucet.requests == [{"accountId": ROOT}]
    [request] = found.store.events(FAUCET_EVENT)
    assert request["data"]["account_id"] == ROOT
    [reply] = found.store.events("faucet_reply")
    assert reply["data"] == {"account_id": ROOT, "ok": True, "status": 200}


async def test_a_treasury_that_cannot_pay_for_a_user_refuses_and_asks_for_more() -> None:
    found, _, faucet, _ = treasury(0.2)
    with pytest.raises(VaultError) as info:
        await found.check_onboarding(COST)
    assert info.value.code == TREASURY_EMPTY and "0.2000 NEAR" in info.value.message
    await found.wait_idle()
    assert faucet.requests == [{"accountId": ROOT}]


async def test_a_full_treasury_leaves_the_faucet_alone() -> None:
    found, vault, faucet, _ = treasury(3.0)
    await found.check_onboarding(COST)
    await found.wait_idle()
    assert faucet.requests == [] and vault.reads == 1


@pytest.mark.parametrize(("network", "root"), [("offline", ROOT), ("sandbox", "sandbox"), ("sandbox", ROOT)])
async def test_the_faucet_is_never_used_off_testnet(network: str, root: str) -> None:
    found, _, faucet, _ = treasury(0.0, network=network, root=root)
    with pytest.raises(VaultError):
        await found.check_onboarding(COST)
    assert not found.faucet_on and found.request_top_up() is False
    await found.wait_idle()
    assert faucet.requests == []


async def test_only_a_testnet_treasury_is_sent_to_the_faucet() -> None:
    found, _, faucet, _ = treasury(0.0, root="vault")  # not a .testnet or implicit account
    assert not found.faucet_on and found.request_top_up() is False
    assert faucet.requests == []
    assert all(faucet_account_ok(account) for account in ("holdup.testnet", "u1.holdup.testnet", "a" * 64))
    assert not faucet_account_ok("holdup.near") and not faucet_account_ok("Holdup.testnet")


async def test_faucet_requests_wait_ten_minutes_and_stop_after_thirty_a_day() -> None:
    found, _, faucet, clock = treasury(1.0)
    assert found.request_top_up() is True
    await found.wait_idle()
    clock.now += 9 * MINUTE
    assert found.request_top_up() is False  # less than ten minutes since the last request
    for _ in range(29):
        clock.now += 10 * MINUTE
        assert found.request_top_up() is True
        await found.wait_idle()
    clock.now += 10 * MINUTE
    assert found.request_top_up() is False  # thirty in the last 24 hours
    assert len(faucet.requests) == 30
    # The limits live in the store, so a restarted service keeps them.
    again = Treasury(found.settings, found.vault, found.store, policy=found.policy, clock=clock, http=found.http)
    assert again.request_top_up() is False
    clock.now += 24 * 60 * MINUTE
    assert again.request_top_up() is True
    await again.wait_idle()


async def test_one_request_at_a_time() -> None:
    found, _, faucet, _ = treasury(1.0, interval_s=0)
    assert found.request_top_up() is True
    assert found.request_top_up() is False  # the first is still on its way
    await found.wait_idle()
    assert len(faucet.requests) == 1


async def test_watch_checks_the_balance_at_most_every_interval() -> None:
    found, vault, faucet, clock = treasury(1.0)
    found.watch()
    await found.wait_idle()
    assert vault.reads == 1 and faucet.requests == [{"accountId": ROOT}]
    clock.now += 5 * MINUTE
    found.watch()
    await found.wait_idle()
    assert vault.reads == 1  # not yet
    clock.now += 5 * MINUTE
    vault.balance = 4 * NEAR  # refilled
    found.watch()
    await found.wait_idle()
    assert vault.reads == 2 and len(faucet.requests) == 1


def test_the_policy_reads_the_environment() -> None:
    default = FaucetPolicy.from_env({})
    assert (default.enabled, default.threshold, default.interval_s, default.per_day) == (True, 3 * NEAR // 2, 600.0, 30)
    assert default.url == FAUCET_URL
    custom = FaucetPolicy.from_env({"HOLDUP_FAUCET_BELOW_NEAR": "2.5", "HOLDUP_FAUCET_INTERVAL_S": "900",
                                    "HOLDUP_FAUCET_PER_DAY": "5", "HOLDUP_FAUCET_URL": "http://127.0.0.1:1/f"})
    assert (custom.threshold, custom.interval_s, custom.per_day) == (5 * NEAR // 2, 900.0, 5)
    assert custom.url == "http://127.0.0.1:1/f"
    assert FaucetPolicy.from_env({"HOLDUP_FAUCET": "off"}).enabled is False
    with pytest.raises(ValueError):
        FaucetPolicy.from_env({"HOLDUP_FAUCET_PER_DAY": "many"})


async def test_faucet_replies() -> None:
    def answer(status: int, body: Any) -> httpx.MockTransport:
        return httpx.MockTransport(lambda request: httpx.Response(status, json=body))

    async with httpx.AsyncClient(transport=answer(200, {"success": True, "message": "Funded x with 5 NEAR"})) as http:
        reply = await request_drop_async(http, ROOT)
    assert reply == FaucetReply(True, 200, "Funded x with 5 NEAR") and reply.maybe_funded
    async with httpx.AsyncClient(transport=answer(400, {"error": "accountId is required"})) as http:
        reply = await request_drop_async(http, ROOT)
    assert (reply.ok, reply.status, reply.message, reply.maybe_funded) == (False, 400, "accountId is required", False)

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with httpx.Client(transport=httpx.MockTransport(timeout)) as http:
        reply = request_drop(http, ROOT)
    # A request that timed out may still have funded the account: re-read the balance.
    assert (reply.ok, reply.status, reply.maybe_funded) == (False, None, True)
    with httpx.Client(transport=answer(500, {"error": "funding failed"})) as http:
        assert request_drop(http, ROOT).maybe_funded
    with pytest.raises(ValueError):
        request_drop(httpx.Client(), "someone.near")
