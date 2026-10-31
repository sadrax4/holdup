"""The treasury: the root account that pays for every new user, and its faucet top-ups.

A new user costs the treasury about a quarter of a NEAR (``budget.onboarding_cost``). On
testnet it refills itself from the NEAR faucet, 5 NEAR a request: before each onboarding, and
every ten minutes while the keeper runs, a balance below ``FaucetPolicy.threshold`` starts one
faucet request in the background, at most once every ``interval_s`` and ``per_day`` times a day
(counted in the store, so restarts do not reset them). A faucet that answers it is rate limited is
not asked again before the time it names (an hour when it names none, a day at most), also kept
in the store. When the treasury cannot pay for a new user, onboarding stops with ``TREASURY_EMPTY``
before any transaction, the log says why, and the bot and the web console tell the person to try
again later.

Only the configured treasury account is ever sent to the faucet, and only on testnet: the
sandbox has money of its own, and the offline world has no network.

Settings (environment): ``HOLDUP_FAUCET`` (``on`` or ``off``, default on),
``HOLDUP_FAUCET_BELOW_NEAR`` (default 1.5), ``HOLDUP_FAUCET_INTERVAL_S`` (default 600),
``HOLDUP_FAUCET_PER_DAY`` (default 30) and ``HOLDUP_FAUCET_URL`` (default the documented API).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from ..config import Settings
from .budget import TREASURY_FLOAT, YOCTO_PER_NEAR, OnboardingCost, near_text
from .errors import VaultError
from .faucet import FAUCET_URL, FaucetReply, faucet_account_ok, request_drop_async

log = logging.getLogger(__name__)

TREASURY_EMPTY = "TREASURY_EMPTY"  # the treasury cannot pay for what was asked; nothing was sent
FAUCET_EVENT = "faucet_request"
FAUCET_REPLY = "faucet_reply"
DAY_MS = 86_400_000
MAX_FAUCET_WAIT_S = 86_400.0  # the longest a rate-limited faucet is left alone, whatever it asks for


@dataclass(frozen=True)
class FaucetPolicy:
    enabled: bool = True
    threshold: int = TREASURY_FLOAT  # ask for more below this balance (yoctoNEAR)
    interval_s: float = 600.0
    per_day: int = 30
    url: str = FAUCET_URL

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> FaucetPolicy:
        env = os.environ if env is None else env
        default = cls()
        try:
            below = Decimal(env.get("HOLDUP_FAUCET_BELOW_NEAR") or "1.5")
            interval = float(env.get("HOLDUP_FAUCET_INTERVAL_S") or default.interval_s)
            per_day = int(env.get("HOLDUP_FAUCET_PER_DAY") or default.per_day)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"invalid faucet setting: {exc}") from exc
        if below < 0 or interval < 0 or per_day < 0:
            raise ValueError("faucet settings must not be negative")
        return cls(
            enabled=(env.get("HOLDUP_FAUCET") or "on").strip().lower() not in ("off", "0", "false", "no"),
            threshold=int(below * YOCTO_PER_NEAR),
            interval_s=interval,
            per_day=per_day,
            url=env.get("HOLDUP_FAUCET_URL") or FAUCET_URL,
        )


@dataclass
class Treasury:
    """Checks the treasury before onboarding and keeps it topped up from the faucet on testnet."""

    settings: Settings
    vault: Any  # a VaultPort with ``treasury_balance()``; without it the chain decides
    store: Any  # holdup.store.Store: faucet requests are logged as events
    policy: FaucetPolicy = field(default_factory=FaucetPolicy.from_env)
    clock: Callable[[], int] = field(default=lambda: int(time.time() * 1000))
    http: httpx.AsyncClient | None = None  # tests pass a client with a fake transport
    _task: asyncio.Task[Any] | None = field(default=None, init=False, repr=False)
    _last_watch_ms: int | None = field(default=None, init=False, repr=False)

    @property
    def account_id(self) -> str:
        return self.settings.root_account

    @property
    def faucet_on(self) -> bool:
        """Faucet top-ups happen on testnet only, for the treasury only, unless switched off."""
        return (
            self.settings.network == "testnet"
            and self.policy.enabled
            and self.policy.per_day > 0
            and faucet_account_ok(self.account_id)
        )

    async def balance(self) -> int | None:
        """What the treasury can spend now, or None when the vault adapter cannot tell."""
        read = getattr(self.vault, "treasury_balance", None)
        if read is None:
            return None
        try:
            return int(await read())
        except VaultError as exc:
            log.warning("could not read the treasury balance: %s", exc)
            return None

    async def check_onboarding(self, cost: OnboardingCost) -> None:
        """Raise ``TREASURY_EMPTY`` when the treasury cannot pay for a new user; refill it when low."""
        balance = await self.balance()
        if balance is None:
            return
        if balance < self.policy.threshold:
            self.request_top_up(balance)
        if balance < cost.at_hand:
            log.warning(
                "treasury %s holds %s NEAR and a new user needs %s NEAR: onboarding refused until it is refilled",
                self.account_id, near_text(balance, up=False), near_text(cost.at_hand),
            )
            raise VaultError(
                TREASURY_EMPTY,
                f"the treasury holds {near_text(balance, up=False)} NEAR "
                f"and a new account needs {near_text(cost.at_hand)} NEAR",
            )

    def watch(self) -> None:
        """Check the balance in the background at most every ``interval_s``; called on keeper ticks."""
        if not self.faucet_on:
            return
        now = self.clock()
        if self._last_watch_ms is not None and now - self._last_watch_ms < self.policy.interval_s * 1000:
            return
        if self._task is not None and not self._task.done():
            return
        self._last_watch_ms = now
        self._task = asyncio.create_task(self._watch(), name="holdup-treasury-watch")

    async def _watch(self) -> None:
        balance = await self.balance()
        if balance is not None and balance < self.policy.threshold:
            self._task = None
            self.request_top_up(balance)

    def request_top_up(self, balance: int | None = None) -> bool:
        """Start one faucet request in the background if the policy allows it now."""
        if not self.faucet_on:
            return False
        if self._task is not None and not self._task.done():
            return False
        now = self.clock()
        if now < self._rate_limited_until():
            return False
        recent = self._recent_requests(now)
        if len(recent) >= self.policy.per_day:
            log.warning("treasury %s is low but the faucet was asked %d times today", self.account_id, len(recent))
            return False
        if recent and now - max(recent) < self.policy.interval_s * 1000:
            return False
        # Logged before the request goes out, so a crash halfway still counts against the limits.
        self.store.log_event(FAUCET_EVENT, {"account_id": self.account_id, "at_ms": now, "balance": str(balance)})
        self._task = asyncio.create_task(self._request(), name="holdup-faucet")
        return True

    def _rate_limited_until(self) -> int:
        """Until when (ms) the faucet's last answer asked to be left alone; 0 if it did not."""
        for event in self.store.events(FAUCET_REPLY, limit=1):
            return int(event["data"].get("retry_at_ms") or 0)
        return 0

    def _recent_requests(self, now: int) -> list[int]:
        times = []
        for event in self.store.events(FAUCET_EVENT, limit=max(self.policy.per_day, 1)):
            at = int(event["data"].get("at_ms", event.get("created_ms", 0)))
            if now - at < DAY_MS:
                times.append(at)
        return times

    async def _request(self) -> FaucetReply:
        if self.http is not None:
            reply = await request_drop_async(self.http, self.account_id, url=self.policy.url)
        else:
            async with httpx.AsyncClient() as http:
                reply = await request_drop_async(http, self.account_id, url=self.policy.url)
        data: dict[str, Any] = {"account_id": self.account_id, "ok": reply.ok, "status": reply.status}
        if reply.ok:
            log.info("faucet funded treasury %s: %s", self.account_id, reply.message)
        elif reply.rate_limited:
            wait_s = min(reply.wait_s, MAX_FAUCET_WAIT_S)
            data["retry_at_ms"] = self.clock() + int(wait_s * 1000)
            log.warning(
                "faucet refused treasury %s for its rate limit (HTTP %s %s); not asking again for %.1f h",
                self.account_id, reply.status, reply.message, wait_s / 3600,
            )
        else:
            log.warning("faucet request for treasury %s: HTTP %s %s", self.account_id, reply.status, reply.message)
        self.store.log_event(FAUCET_REPLY, data)
        return reply

    async def wait_idle(self) -> None:
        """Wait for a background balance check or faucet request (tests)."""
        while self._task is not None and not self._task.done():
            await asyncio.gather(self._task, return_exceptions=True)
