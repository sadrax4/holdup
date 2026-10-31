"""The keeper releases holds whose time has passed.

``release`` is open to anyone on-chain; the keeper account exists so the
server does not need a full-access key to do it, and its only key can call
nothing else.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..chain import errors as E
from ..chain.errors import VaultError
from ..interfaces import VaultPort
from ..models import Hold, TxResult

log = logging.getLogger(__name__)

OnSettled = Callable[[Hold, TxResult], Awaitable[None]]
OnTick = Callable[[], Awaitable[object]]
Skip = Callable[[Hold], bool]


@dataclass
class Released:
    hold: Hold
    tx: TxResult | None
    error: str | None = None


class Keeper:
    def __init__(
        self,
        vault: VaultPort,
        *,
        on_settled: OnSettled | None = None,
        on_tick: OnTick | None = None,
        skip: Skip | None = None,
        interval_s: float = 5.0,
        batch: int = 50,
    ) -> None:
        self.vault = vault
        self.on_settled = on_settled
        self.on_tick = on_tick
        self.skip = skip  # holds not to release yet, e.g. while a brake for them is being retried
        self.interval_s = interval_s
        self.batch = batch

    async def run_once(self) -> list[Released]:
        """Release every due hold once; report each one to ``on_settled``."""
        done: list[Released] = []
        try:
            due = await self.vault.get_due_holds(self.batch)
        except VaultError as exc:
            log.warning("keeper could not list due holds: %s", exc)
            due = []
        for hold in due:
            if self.skip is not None and self.skip(hold):
                log.info("keeper leaves hold %s for now: a brake for it is still being retried", hold.id)
                continue
            try:
                tx = await self.vault.release(hold.id)
            except VaultError as exc:
                if exc.code not in (E.E_HOLD_NOT_ACTIVE, E.E_NOT_DUE):
                    log.warning("keeper could not release hold %s: %s", hold.id, exc)
                    done.append(Released(hold, None, exc.code))
                continue
            settled = await self.vault.get_hold(hold.id) or hold
            done.append(Released(settled, tx))
            if self.on_settled is not None:
                try:
                    await self.on_settled(settled, tx)
                except Exception:
                    log.exception("settlement callback failed for hold %s", hold.id)
        if self.on_tick is not None:
            try:
                await self.on_tick()
            except Exception:
                log.exception("keeper tick callback failed")
        return done

    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Loop until ``stop`` is set."""
        stop = stop or asyncio.Event()
        while not stop.is_set():
            await self.run_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.interval_s)
