"""Per-user Otari keys, each capped by a spend ceiling of its own.

A HoldUp user can get an Otari API key whose spend is limited by a daily
budget, so one person (or one attacker flooding the bot) can never use up the
AI budget of everyone else. Otari enforces it: a budget defines an amount and a
period, and a spend ceiling scoped to the key (``scope_type: api_token``)
applies it; a refused request answers 403 ``budget_exceeded``, which the risk
pipeline turns into a fail-closed brake.

Routes (OpenAPI schemas in brackets):

* ``POST /api/v1/organizations/me/keys`` [CreateOwnKeyRequest -> CreateKeyResponse]
* ``POST /api/v1/organizations/me/budgets`` [OrganizationBudgetCreate -> OrganizationBudgetPublic]
* ``POST /api/v1/organizations/me/spend-ceilings`` [OrganizationScopedBudgetCreate -> OrganizationScopedBudgetPublic]

On the hosted gateway these routes accept only the master key or a dashboard
session; an ordinary ``otk_`` API key is refused with 401 ``Invalid master key``
(observed 2026-10-31). With such a key, mint the per-user keys and their
ceilings in the dashboard (Organization, Spend & Budgets) and list the keys in
``OTARI_USER_KEYS``: ``KeyPool`` shares them out by a stable hash of the
account, so each account always spends against the same ceiling.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .otari import OtariClient, OtariError

log = logging.getLogger(__name__)

KEYS = "/organizations/me/keys"
BUDGETS = "/organizations/me/budgets"
CEILINGS = "/organizations/me/spend-ceilings"


@dataclass(frozen=True)
class UserKey:
    """An Otari key minted for one user, with the budget and ceiling that cap it."""

    key_id: str
    key: str = field(repr=False)
    budget_id: str
    ceiling_id: str
    name: str


class OtariAdmin:
    """Organization-level calls made with an owner or admin key."""

    def __init__(self, client: OtariClient) -> None:
        self.client = client

    async def create_key(
        self, name: str, *, workspace_id: str | None = None, metadata: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """A new API key owned by the caller; the response carries the secret once, as ``key``."""
        body: dict[str, Any] = {"key_name": name}
        if workspace_id:
            body["workspace_id"] = workspace_id
        if metadata:
            body["metadata"] = dict(metadata)
        return await self.client.rest("POST", KEYS, json_body=body)

    async def create_budget(
        self, name: str, max_usd: float, *, reset_alignment: str = "calendar_day"
    ) -> dict[str, Any]:
        """A USD budget that resets on a UTC calendar boundary (``calendar_day``, ``_week`` or ``_month``)."""
        body = {"name": name, "max_budget": max_usd, "reset_alignment": reset_alignment}
        return await self.client.rest("POST", BUDGETS, json_body=body)

    async def create_ceiling(
        self, budget_id: str, scope_id: str, *, scope_type: str = "api_token", name: str | None = None
    ) -> dict[str, Any]:
        """Attach ``budget_id`` to one identity; a budget limits nothing until a ceiling applies it."""
        body: dict[str, Any] = {"scope_type": scope_type, "scope_id": scope_id, "budget_id": budget_id}
        if name:
            body["name"] = name
        return await self.client.rest("POST", CEILINGS, json_body=body)

    async def revoke_key(self, key_id: str) -> None:
        await self.client.rest("DELETE", f"{KEYS}/{key_id}")

    async def list_keys(self) -> list[dict[str, Any]]:
        return await self.client.rest("GET", KEYS) or []

    async def list_budgets(self) -> Any:
        return await self.client.rest("GET", BUDGETS)

    async def list_ceilings(self) -> Any:
        return await self.client.rest("GET", CEILINGS)

    async def provision_user_key(self, label: str, daily_usd: float) -> UserKey:
        """Mint a key for one user and cap it at ``daily_usd`` per UTC day.

        ``label`` should be the user's NEAR account id or another pseudonym,
        never a chat id. If the budget or the ceiling cannot be created, the
        new key is revoked again so that no uncapped key is left behind.
        """
        name = f"holdup-{label}"
        created = await self.create_key(name, metadata={"app": "holdup"})
        key_id, secret = str(created["id"]), str(created["key"])
        try:
            budget = await self.create_budget(f"{name}-daily", daily_usd)
            ceiling = await self.create_ceiling(str(budget["budget_id"]), key_id, name=f"{name}-ceiling")
        except (OtariError, KeyError):
            log.warning("could not cap the new key %s; revoking it", key_id)
            try:
                await self.revoke_key(key_id)
            except OtariError:
                log.exception("revoking key %s failed; remove it in the dashboard", key_id)
            raise
        return UserKey(key_id, secret, str(budget["budget_id"]), str(ceiling["id"]), name)


class KeyPool:
    """Pre-made keys (``OTARI_USER_KEYS``, comma separated) shared out by account."""

    def __init__(self, keys: Sequence[str]) -> None:
        self.keys = [k.strip() for k in keys if k.strip()]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> KeyPool:
        env = os.environ if env is None else env
        return cls(env.get("OTARI_USER_KEYS", "").split(","))

    def __len__(self) -> int:
        return len(self.keys)

    def key_for(self, account_id: str) -> str | None:
        """The same key for the same account every time, so its ceiling caps that account."""
        if not self.keys:
            return None
        index = int.from_bytes(hashlib.sha256(account_id.encode()).digest()[:8], "big") % len(self.keys)
        return self.keys[index]


def user_key_resolver(store: Any, pool: KeyPool | None = None) -> Callable[[str], str | None]:
    """Account id -> Otari key: the user's own key from the store, else one from the pool.

    ``store`` is ``holdup.store.Store`` (``user_by_account`` and ``get_otari_key``).
    """

    def resolve(account_id: str) -> str | None:
        user = store.user_by_account(account_id)
        if user:
            saved = store.get_otari_key(user["user_key"])
            if saved and saved[1]:
                return saved[1]
        return pool.key_for(account_id) if pool else None

    return resolve
