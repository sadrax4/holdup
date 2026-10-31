"""Payee facts: the offline fixture table, FastNEAR activity, the vault record, and failures."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from holdup.config import Settings
from holdup.intel import PayeeIntel, load_payee_fixtures, parse_account_txs, parse_signers
from holdup.models import PayeeStats

NOW = 1_793_000_000_000
HOUR = 3_600_000
FASTNEAR = "https://tx.test.fastnear.example"


class TinyVault:
    vault_id = "vault.holdup.testnet"

    def __init__(self) -> None:
        self.stats: dict[str, PayeeStats] = {}
        self.existing = {"safe-wallet-77.testnet", "fresh.testnet", "busy.testnet"}

    async def now_ms(self) -> int:
        return NOW

    async def get_payee_stats(self, account_id: str) -> PayeeStats:
        return self.stats.get(account_id, PayeeStats())

    async def account_exists(self, account_id: str) -> bool:
        return account_id in self.existing


def tx(tx_hash: str, age_ms: int, **flags: bool) -> dict[str, Any]:
    """One row of a FastNEAR /v0/account answer (shape as recorded from tx.test.fastnear.com)."""
    row = {"account_id": "fresh.testnet", "transaction_hash": tx_hash, "tx_block_height": 275571714,
           "tx_block_timestamp": str((NOW - age_ms) * 1_000_000), "tx_index": 0, "is_signer": False,
           "is_any_signer": False, "is_receiver": False, "is_real_receiver": False, "is_action_arg": False,
           "is_event_log": False, "is_explicit_refund_to": False, "is_success": True}
    return {**row, **flags}


class FastNear:
    """A mock of the two FastNEAR endpoints the intel uses."""

    def __init__(self, txs: list[dict[str, Any]], count: int | None = None, signers: dict[str, str] | None = None):
        self.txs, self.count, self.signers = txs, count, signers or {}
        self.requests: list[tuple[str, Any]] = []
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append((request.url.path, body))
        if self.fail:
            return httpx.Response(503, text="unavailable")
        if request.url.path == "/v0/account":
            return httpx.Response(200, json={"account_txs": self.txs, "txs_count": self.count or len(self.txs)})
        transactions = [{"transaction": {"hash": h, "signer_id": self.signers[h]}} for h in body["tx_hashes"]]
        return httpx.Response(200, json={"transactions": transactions})

    def intel(self, vault: TinyVault, **kwargs: Any) -> PayeeIntel:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return PayeeIntel(Settings(network="testnet"), vault, http, fastnear_url=FASTNEAR, wall_clock_ms=lambda: NOW,
                          **kwargs)


@pytest.fixture
def vault() -> TinyVault:
    return TinyVault()


# ----------------------------------------------------------------------
# offline fixtures
# ----------------------------------------------------------------------


async def test_fixture_profiles_are_measured_from_the_vault_clock(vault: TinyVault) -> None:
    vault.stats["safe-wallet-77.testnet"] = PayeeStats(holds=1, flagged=1, cancelled=1, cancelled_after_flag=1,
                                                       last_flag_ms=NOW - HOUR)
    intel = PayeeIntel(Settings(network="offline"), vault, fixtures=load_payee_fixtures())
    profile = await intel.profile("safe-wallet-77.testnet")
    assert profile == {
        "account_id": "safe-wallet-77.testnet",
        "exists": True,
        "created_ms": NOW - 2 * HOUR,
        "age_hours": 2.0,
        "age_is_lower_bound": False,
        "distinct_inbound_senders_24h": 14,
        "tx_count": 31,
        "reputation": {"holds": 1, "flagged": 1, "cancelled": 1, "cancelled_after_flag": 1, "released": 0,
                       "last_flag_ms": NOW - HOUR},
        "source": "fixture+vault",
        "unavailable": [],
    }


async def test_fixture_history_adds_to_what_the_vault_records(vault: TinyVault) -> None:
    vault.stats["northwindpaper.testnet"] = PayeeStats(holds=1, released=1)
    intel = PayeeIntel(Settings(network="offline"), vault, fixtures=load_payee_fixtures())
    reputation = await intel.reputation("northwindpaper.testnet")
    assert reputation is not None and (reputation["holds"], reputation["released"]) == (7, 7)
    assert (await intel.profile("northwindpaper.testnet"))["age_hours"] == 2 * 365 * 24


def test_the_offline_world_is_built_from_the_fixture_table() -> None:
    from holdup.chain.fake import SimClock
    from holdup.config import offline_settings
    from holdup.runtime import offline_vault

    clock = SimClock()
    vault = offline_vault(offline_settings(), clock)
    for account, facts in load_payee_fixtures()["payees"].items():
        assert vault.account_created_ms(account) == clock() - int(facts["age_hours"] * HOUR), account


async def test_accounts_outside_the_table_come_from_the_chain(vault: TinyVault) -> None:
    intel = PayeeIntel(Settings(network="offline"), vault, fixtures=load_payee_fixtures())
    known = await intel.profile("fresh.testnet")
    assert (known["exists"], known["age_hours"], known["source"]) == (True, None, "chain+vault")
    assert known["unavailable"] == []
    missing = await intel.profile("nobody.testnet")
    assert (missing["exists"], missing["reputation"], missing["source"]) == (False, None, "chain")


# ----------------------------------------------------------------------
# FastNEAR
# ----------------------------------------------------------------------


async def test_age_and_inbound_senders_come_from_fastnear(vault: TinyVault) -> None:
    index = FastNear(
        [
            tx("own", HOUR, is_signer=True, is_any_signer=True),
            tx("in-1", 2 * HOUR, is_receiver=True, is_real_receiver=True),
            tx("in-2", 3 * HOUR, is_action_arg=True),
            tx("in-3", 4 * HOUR, is_event_log=True),
            tx("refund", 5 * HOUR, is_explicit_refund_to=True, is_receiver=True),
            tx("old", 30 * HOUR, is_receiver=True),
            tx("created", 120 * HOUR, is_receiver=True, is_real_receiver=True),
        ],
        signers={"in-1": "alice.testnet", "in-2": "bob.testnet", "in-3": "alice.testnet"},
    )
    profile = await index.intel(vault).profile("fresh.testnet")
    assert (profile["created_ms"], profile["age_hours"]) == (NOW - 120 * HOUR, 120.0)
    assert profile["age_is_lower_bound"] is False
    assert (profile["distinct_inbound_senders_24h"], profile["tx_count"]) == (2, 7)
    assert profile["source"] == "chain+fastnear+vault"
    assert index.requests == [
        ("/v0/account", {"account_id": "fresh.testnet"}),
        ("/v0/transactions", {"tx_hashes": ["in-1", "in-2", "in-3"]}),
    ]


async def test_a_partial_index_page_only_bounds_the_age(vault: TinyVault) -> None:
    index = FastNear([tx("a", 10 * HOUR, is_signer=True)], count=900)
    profile = await index.intel(vault).profile("busy.testnet")
    assert (profile["age_hours"], profile["age_is_lower_bound"], profile["tx_count"]) == (10.0, True, 900)
    assert profile["distinct_inbound_senders_24h"] == 0
    assert [path for path, _ in index.requests] == ["/v0/account"]


async def test_activity_is_cached_briefly(vault: TinyVault) -> None:
    clock = [0.0]
    index = FastNear([tx("a", 10 * HOUR, is_receiver=True)], signers={"a": "x.testnet"})
    intel = index.intel(vault, monotonic=lambda: clock[0], cache_ttl_s=60)
    await intel.profile("fresh.testnet")
    await intel.profile("fresh.testnet")
    assert len(index.requests) == 2  # one account lookup, one signer lookup
    clock[0] = 61.0
    await intel.profile("fresh.testnet")
    assert len(index.requests) == 4


async def test_failures_are_reported_not_raised(vault: TinyVault) -> None:
    index = FastNear([])
    index.fail = True
    profile = await index.intel(vault).profile("fresh.testnet")
    assert (profile["exists"], profile["age_hours"], profile["unavailable"]) == (True, None, ["fastnear"])

    async def boom(*_args: Any) -> Any:
        raise ConnectionError("rpc down")

    vault.account_exists = boom  # type: ignore[method-assign]
    vault.get_payee_stats = boom  # type: ignore[method-assign]
    vault.now_ms = boom  # type: ignore[method-assign]
    index.fail = False
    profile = await index.intel(vault).profile("fresh.testnet")
    assert (profile["exists"], profile["reputation"]) == (None, None)
    assert profile["unavailable"] == ["chain", "vault"] and profile["source"] == "fastnear"


async def test_a_slow_index_times_out(vault: TinyVault) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    http = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    intel = PayeeIntel(Settings(network="testnet"), vault, http, fastnear_url=FASTNEAR, timeout_s=0.05)
    profile = await intel.profile("fresh.testnet")
    assert profile["unavailable"] == ["fastnear"]


def test_parsers_tolerate_odd_rows() -> None:
    activity, inbound = parse_account_txs({"account_txs": [{"tx_block_timestamp": "x"}, "junk"]}, NOW)
    assert (activity.created_ms, activity.tx_count, inbound) == (None, 1, [])
    assert parse_signers({"transactions": [{"transaction": {"signer_id": "a.testnet"}}, {"signer_id": "b.testnet"},
                                           {"transaction": {}}, 7]}) == {"a.testnet", "b.testnet"}
    assert parse_signers({}) == set()


async def test_the_owned_http_client_is_closed(vault: TinyVault) -> None:
    intel = PayeeIntel(Settings(network="testnet"), vault)
    client = intel._client()
    await intel.aclose()
    assert client.is_closed


async def test_lookups_run_concurrently(vault: TinyVault) -> None:
    class SlowVault(TinyVault):
        async def now_ms(self) -> int:
            await asyncio.sleep(0.3)
            return NOW

        async def account_exists(self, account_id: str) -> bool:
            await asyncio.sleep(0.3)
            return True

        async def get_payee_stats(self, account_id: str) -> PayeeStats:
            await asyncio.sleep(0.3)
            return PayeeStats()

    started = asyncio.get_running_loop().time()
    profile = await PayeeIntel(Settings(network="offline"), SlowVault()).profile("fresh.testnet")
    assert asyncio.get_running_loop().time() - started < 0.7  # one round trip, not three
    assert profile["source"] == "chain+vault"
