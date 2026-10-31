"""Per-user Otari keys: organization calls, cleanup on failure, the env key pool and the resolver."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from holdup.config import Settings
from holdup.risk.budgets import KeyPool, OtariAdmin, UserKey, user_key_resolver
from holdup.risk.otari import OtariClient, OtariRequestError


class Organization:
    """Answers the organization routes; ``fail_on`` makes one route refuse."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if self.fail_on and path.startswith(self.fail_on):
            return httpx.Response(401, json={"detail": "Invalid master key"})
        if request.method == "POST" and path == "/organizations/me/keys":
            return httpx.Response(200, json={"id": "key-1", "key": "otk_v1_us_minted", "key_name": body["key_name"]})
        if request.method == "POST" and path == "/organizations/me/budgets":
            return httpx.Response(200, json={"budget_id": "budget-1", **body})
        if request.method == "POST" and path == "/organizations/me/spend-ceilings":
            return httpx.Response(200, json={"id": "ceiling-1", **body})
        if request.method == "DELETE":
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"detail": "not found"})


def admin_for(org: Organization) -> OtariAdmin:
    http = httpx.AsyncClient(transport=httpx.MockTransport(org))
    return OtariAdmin(OtariClient(Settings(otari_api_key="otk_v1_us_admin"), http=http))


async def test_a_user_key_is_minted_and_capped_by_its_own_ceiling() -> None:
    org = Organization()
    created = await admin_for(org).provision_user_key("u3kq7x2m9aa.holdup.testnet", 0.25)
    assert created == UserKey("key-1", "otk_v1_us_minted", "budget-1", "ceiling-1", "holdup-u3kq7x2m9aa.holdup.testnet")
    assert "otk_v1_us_minted" not in repr(created)
    assert org.calls == [
        (
            "POST",
            "/organizations/me/keys",
            {"key_name": "holdup-u3kq7x2m9aa.holdup.testnet", "metadata": {"app": "holdup"}},
        ),
        (
            "POST",
            "/organizations/me/budgets",
            {"name": "holdup-u3kq7x2m9aa.holdup.testnet-daily", "max_budget": 0.25, "reset_alignment": "calendar_day"},
        ),
        (
            "POST",
            "/organizations/me/spend-ceilings",
            {
                "scope_type": "api_token",
                "scope_id": "key-1",
                "budget_id": "budget-1",
                "name": "holdup-u3kq7x2m9aa.holdup.testnet-ceiling",
            },
        ),
    ]


async def test_a_key_that_cannot_be_capped_is_revoked() -> None:
    org = Organization(fail_on="/organizations/me/spend-ceilings")
    with pytest.raises(OtariRequestError, match="Invalid master key"):
        await admin_for(org).provision_user_key("u1.holdup.testnet", 0.25)
    assert org.calls[-1] == ("DELETE", "/organizations/me/keys/key-1", None)


async def test_an_ordinary_api_key_is_refused_by_the_organization_routes() -> None:
    org = Organization(fail_on="/organizations/me/keys")
    with pytest.raises(OtariRequestError):
        await admin_for(org).create_key("x")


def test_the_pool_reads_the_environment_and_sticks_to_one_key_per_account() -> None:
    pool = KeyPool.from_env({"OTARI_USER_KEYS": " otk_a, ,otk_b,otk_c "})
    assert pool.keys == ["otk_a", "otk_b", "otk_c"] and len(pool) == 3
    assert pool.key_for("u1.holdup.testnet") == pool.key_for("u1.holdup.testnet")
    spread = {pool.key_for(f"u{n}.holdup.testnet") for n in range(60)}
    assert spread == {"otk_a", "otk_b", "otk_c"}
    empty = KeyPool.from_env({})
    assert len(empty) == 0 and empty.key_for("u1.holdup.testnet") is None


class Store:
    def __init__(self) -> None:
        self.users = {"u1.holdup.testnet": {"user_key": "tg:1"}, "u2.holdup.testnet": {"user_key": "tg:2"}}
        self.keys = {"tg:1": ("key-1", "otk_v1_us_own"), "tg:2": ("key-2", None)}

    def user_by_account(self, account_id: str) -> dict[str, Any] | None:
        return self.users.get(account_id)

    def get_otari_key(self, user_key: str) -> tuple[str, str | None] | None:
        return self.keys.get(user_key)


def test_the_resolver_prefers_the_users_own_key_then_the_pool() -> None:
    resolve = user_key_resolver(Store(), KeyPool(["otk_pool"]))
    assert resolve("u1.holdup.testnet") == "otk_v1_us_own"
    assert resolve("u2.holdup.testnet") == "otk_pool"  # only the key id is stored
    assert resolve("stranger.testnet") == "otk_pool"
    assert user_key_resolver(Store())("stranger.testnet") is None
