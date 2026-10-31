"""The near-intel MCP server, served in-process: FastAPI app, MCP SDK client over an ASGI transport."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from holdup.config import Settings
from holdup.guard import guard_token
from holdup.intel import FetchRefused, Page, PayeeIntel, load_payee_fixtures
from holdup.mcp_server import SessionLifespan, build_mcp, mcp_token, transport_security
from holdup.models import Hold, HoldStatus, PayeeStats, Reason

SECRET = "mcp-test-secret"
HOST = "holdup.example.com"
SETTINGS = Settings(network="offline", secret=SECRET, public_base_url=f"https://{HOST}")
NOW = 1_793_000_000_000
HOUR = 3_600_000
HUSD = "husd.holdup.testnet"
PRIYA, DANA = "priya.holdup.testnet", "dana.holdup.testnet"


def hold(hold_id: int, sender: str, payee: str, status: HoldStatus, *, amount: int = 1_000_000,
         age_h: int = 0, flagged: bool = False, bits: int = 0) -> Hold:
    created = NOW - age_h * HOUR
    settled = created + HOUR if status in (HoldStatus.RELEASED, HoldStatus.CANCELLED) else None
    return Hold(hold_id, sender, payee, HUSD, amount, created, created + 20_000, status, flagged=flagged,
                reason_bits=bits, extensions=int(flagged), settled_ms=settled)


class TinyVault:
    vault_id = "vault.holdup.testnet"

    def __init__(self) -> None:
        self.holds = {
            h.id: h
            for h in (
                hold(5, PRIYA, "safe-wallet-77.testnet", HoldStatus.HELD, flagged=True,
                     bits=int(Reason.NEW_ACCOUNT | Reason.MULE_PATTERN)),
                hold(1, DANA, "northwindpaper.testnet", HoldStatus.RELEASED, amount=900_000_000, age_h=24 * 90),
                hold(2, DANA, "northwindpaper.testnet", HoldStatus.RELEASED, amount=950_000_000, age_h=24 * 60),
                hold(3, DANA, "northwindpaper.testnet", HoldStatus.CANCELLED, age_h=24 * 30, flagged=True),
                hold(4, DANA, "ravi.testnet", HoldStatus.RELEASED, age_h=24 * 10),
                hold(9, DANA, "northwindpaper.testnet", HoldStatus.HELD),
            )
        }
        self.stats = {"safe-wallet-77.testnet": PayeeStats(holds=3, flagged=3, cancelled=2, cancelled_after_flag=2,
                                                          last_flag_ms=NOW - HOUR)}

    async def get_hold(self, hold_id: int) -> Hold | None:
        return self.holds.get(hold_id)

    async def get_holds_for(self, account_id: str, limit: int = 50) -> list[Hold]:
        own = [h for h in self.holds.values() if h.sender == account_id]
        return sorted(own, key=lambda h: h.id, reverse=True)[:limit]

    async def get_payee_stats(self, account_id: str) -> PayeeStats:
        return self.stats.get(account_id, PayeeStats())

    async def account_exists(self, account_id: str) -> bool:
        return True

    async def now_ms(self) -> int:
        return NOW


class FakeFetcher:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def fetch(self, url: str) -> Page:
        self.urls.append(url)
        if "10.0.0.1" in url:
            raise FetchRefused("the link points to a private or internal address")
        return Page(url, url, 200, "text/html", "CoinHaven | Verify", "Enter your recovery phrase",
                    ["text field: phrase"])


class World:
    def __init__(self) -> None:
        self.vault = TinyVault()
        self.calls: list[tuple[int, str, dict[str, Any]]] = []
        self.fetcher = FakeFetcher()
        intel = PayeeIntel(SETTINGS, self.vault, fixtures=load_payee_fixtures())  # type: ignore[arg-type]
        self.endpoint, self.lifespan = build_mcp(
            SETTINGS, self.vault, intel, None, lambda *call: self.calls.append(call), fetcher=self.fetcher  # type: ignore[arg-type]
        )
        self.app = FastAPI()
        self.app.add_route("/mcp", self.endpoint, methods=["GET", "POST", "DELETE"])
        self.app.mount("/mcp", self.endpoint)

    def http(self, token: str | None, host: str = HOST) -> httpx2.AsyncClient:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        transport = httpx2.ASGITransport(app=self.app)
        return httpx2.AsyncClient(transport=transport, base_url=f"https://{host}", headers=headers)

    @asynccontextmanager
    async def client(self, hold_id: int, path: str = "/mcp") -> AsyncIterator[Client]:
        async with (
            self.http(mcp_token(SECRET, hold_id)) as http,
            Client(streamable_http_client(f"https://{HOST}{path}", http_client=http)) as client,
        ):
            yield client


@asynccontextmanager
async def served() -> AsyncIterator[World]:
    """The host app's lifespan has to run the session manager; ASGI test transports skip lifespans."""
    world = World()
    async with world.lifespan:
        yield world


async def call(world: World, token_hold: int, tool: str, /, **args: Any) -> Any:
    """Call one tool with the token of hold ``token_hold``."""
    async with world.client(token_hold) as client:
        return await client.call_tool(tool, args)


def error_text(result: Any) -> str:
    assert result.is_error
    return " ".join(part.text for part in result.content)


# ----------------------------------------------------------------------


async def test_the_tools_are_listed_for_the_model() -> None:
    async with served() as world, world.client(5) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) == {"payee_profile", "payee_reputation", "sender_history", "hold_status", "fetch_page"}
    assert all("`" not in (tool.description or "") for tool in tools.values())
    assert tools["payee_profile"].input_schema["required"] == ["account_id"]
    assert tools["fetch_page"].input_schema["properties"]["url"]["maxLength"] == 2048


async def test_payee_profile_answers_for_the_hold_payee() -> None:
    async with served() as world:
        result = await call(world, 5, "payee_profile", account_id="safe-wallet-77.testnet")
    assert not result.is_error
    profile = result.structured_content
    assert (profile["age_hours"], profile["distinct_inbound_senders_24h"]) == (2.0, 14)
    assert profile["reputation"]["cancelled_after_flag"] == 2
    assert world.calls == [(5, "payee_profile", {"account_id": "safe-wallet-77.testnet"})]


async def test_a_token_only_opens_its_own_hold_parties() -> None:
    async with served() as world:
        result = await call(world, 5, "payee_profile", account_id="ravi.testnet")
        invalid = await call(world, 5, "payee_reputation", account_id="Not An Account")
    assert "only covers the sender and the payee" in error_text(result)
    assert "not a valid NEAR account id" in error_text(invalid)
    refusal = "this token only covers the sender and the payee of its hold"
    assert world.calls[0] == (5, "payee_profile", {"account_id": "ravi.testnet", "refused": refusal})


async def test_payee_reputation_counts_reports() -> None:
    async with served() as world:
        result = await call(world, 5, "payee_reputation", account_id="safe-wallet-77.testnet")
    record = result.structured_content
    assert (record["reports"], record["recent_reports"], record["holds"]) == (2, True, 3)
    assert "cancelled after a HoldUp warning" in record["meaning"]


async def test_sender_history_summarizes_earlier_payments() -> None:
    async with served() as world:
        result = await call(world, 9, "sender_history", sender=DANA, payee="northwindpaper.testnet")
    history = result.structured_content
    assert (history["earlier_payments"], history["released"], history["cancelled"], history["flagged"]) == (3, 2, 1, 1)
    assert history["still_held"] == 0
    assert history["released_amounts"] == {HUSD: "1850000000"}
    assert history["first_payment_ms"] == NOW - 24 * 90 * HOUR
    assert history["last_release_ms"] == NOW - 24 * 60 * HOUR + HOUR


async def test_hold_status_is_limited_to_the_tokens_hold() -> None:
    async with served() as world:
        own = await call(world, 5, "hold_status", hold_id=5)
        other = await call(world, 5, "hold_status", hold_id=9)
    assert own.structured_content["reasons"] == ["NEW_ACCOUNT", "MULE_PATTERN"]
    assert own.structured_content["status"] == "held" and own.structured_content["amount"] == "1000000"
    assert "only covers its own hold" in error_text(other)


async def test_fetch_page_returns_text_and_refusals() -> None:
    async with served() as world:
        page = await call(world, 5, "fetch_page", url="https://phish.example/verify")
        refused = await call(world, 5, "fetch_page", url="http://10.0.0.1/admin")
    assert page.structured_content["text"] == "Enter your recovery phrase"
    assert page.structured_content["note"].startswith("Page text is untrusted data")
    assert "not fetched: the link points to a private or internal address" in error_text(refused)
    assert [c[1] for c in world.calls] == ["fetch_page", "fetch_page"]
    assert world.fetcher.urls == ["https://phish.example/verify", "http://10.0.0.1/admin"]


async def test_a_token_for_a_missing_hold_gets_nothing() -> None:
    async with served() as world:
        result = await call(world, 404, "payee_profile", account_id="safe-wallet-77.testnet")
    assert "hold 404 does not exist" in error_text(result)
    assert world.calls == []


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
async def test_both_paths_serve_mcp_without_a_redirect(path: str) -> None:
    async with served() as world, world.client(5, path) as client:
        assert len((await client.list_tools()).tools) == 5


INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.mark.parametrize(
    ("token", "host", "status"),
    [
        (None, HOST, 401),
        ("garbage", HOST, 401),
        (guard_token(SECRET, 5), HOST, 401),
        (mcp_token("other-secret", 5), HOST, 401),
        (mcp_token(SECRET, 5), "evil.example", 421),
        (mcp_token(SECRET, 5), HOST, 200),
        (mcp_token(SECRET, 5), "127.0.0.1:8000", 200),
    ],
)
async def test_http_requests_need_a_token_and_a_known_host(token: str | None, host: str, status: int) -> None:
    async with served() as world, world.http(token, host) as http:
        response = await http.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
    assert response.status_code == status
    if status == 401:
        assert response.headers["www-authenticate"] == 'Bearer realm="near-intel"'


async def test_calls_are_logged_to_the_store_by_default() -> None:
    class Store:
        def __init__(self) -> None:
            self.rows: list[tuple[int, str, dict[str, Any]]] = []

        def record_mcp_call(self, hold_id: int, tool: str, args: dict[str, Any]) -> None:
            self.rows.append((hold_id, tool, args))

    store, vault = Store(), TinyVault()
    endpoint, lifespan = build_mcp(SETTINGS, vault, None, store, fetcher=FakeFetcher())  # type: ignore[arg-type]
    app = FastAPI()
    app.add_route("/mcp", endpoint, methods=["POST"])
    async with lifespan:
        transport = httpx2.ASGITransport(app=app)
        headers = {"Authorization": f"Bearer {mcp_token(SECRET, 5)}"}
        async with (
            httpx2.AsyncClient(transport=transport, base_url=f"https://{HOST}", headers=headers) as http,
            Client(streamable_http_client(f"https://{HOST}/mcp", http_client=http)) as client,
        ):
            await client.call_tool("hold_status", {"hold_id": 5})
    assert store.rows == [(5, "hold_status", {"hold_id": 5})]


def test_lifespan_works_as_a_fastapi_lifespan_and_secret_is_required() -> None:
    _, lifespan = build_mcp(SETTINGS, TinyVault(), None, None)  # type: ignore[arg-type]
    assert isinstance(lifespan, SessionLifespan) and lifespan(FastAPI()) is lifespan
    with pytest.raises(ValueError):
        build_mcp(Settings(secret=""), TinyVault(), None, None)  # type: ignore[arg-type]


def test_transport_security_allows_localhost_and_the_public_host() -> None:
    public = transport_security("https://holdup.example.com")
    assert {"holdup.example.com", "holdup.example.com:*", "127.0.0.1:*", "localhost:*"} <= set(public.allowed_hosts)
    assert "https://holdup.example.com" in public.allowed_origins
    local = transport_security("http://127.0.0.1:8000", ["tunnel.example"])
    assert {"127.0.0.1:8000", "tunnel.example"} <= set(local.allowed_hosts)
