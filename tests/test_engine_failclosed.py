"""Fail-closed: when a check fails or is too slow, the brake stays on (+1 h per failure, 6 h at most)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from holdup.chain import errors as E
from holdup.chain.errors import VaultError
from holdup.chain.fake import FakeVault, SimClock
from holdup.config import offline_settings
from holdup.engine import Engine
from holdup.models import Finding, Hold, PaymentContext, Reason, Verdict
from holdup.runtime import offline_vault
from holdup.store import Store

HOUR = 3_600_000
MINUTE = 60_000
HUSD = 10**6
PAYEE = "ravi.testnet"


class Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def notify(self, user_key: str, text: str, buttons: Any = None) -> None:
        self.sent.append((user_key, text))

    def last(self) -> str:
        return self.sent[-1][1]


class Scripted:
    """Risk check returning scripted results in order; the last one repeats."""

    def __init__(self, *results: Verdict | Exception) -> None:
        self.results = list(results)
        self.calls = 0

    async def assess(self, hold: Hold, ctx: PaymentContext) -> Verdict:
        self.calls += 1
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return replace(result, hold_id=hold.id)


def brake(reason: Reason, hold_for: int, source: str = "rules") -> Verdict:
    return Verdict(0, True, hold_for, [Finding(reason, reason.name.lower(), source)])


CLEAN = Verdict(0, False, 0)
UNAVAILABLE = Verdict(0, True, HOUR, [Finding(Reason.AI_UNAVAILABLE, "all candidates failed", "investigator")])
NO_BUDGET = Verdict(0, True, HOUR, [Finding(Reason.BUDGET_EXHAUSTED, "budget exceeded", "triage")])


@pytest.fixture
def clock() -> SimClock:
    return SimClock()


@pytest.fixture
def setup(clock: SimClock) -> tuple[FakeVault, Store, Recorder]:
    settings = offline_settings()
    return offline_vault(settings, clock), Store(":memory:", clock=clock), Recorder()


async def held_payment(
    clock: SimClock, setup: tuple[FakeVault, Store, Recorder], fast: Any, full: Any
) -> tuple[Engine, Hold]:
    vault, store, notifier = setup
    settings = offline_settings()
    engine = Engine(settings, vault, fast, full, store, notifier, clock=clock)
    await engine.start("tg:1", "Priya")
    outcome = await engine.pay("tg:1", PAYEE, settings.husd_token or "", 5 * HUSD)
    assert outcome.hold is not None
    await engine.wait_idle()
    return engine, outcome.hold


async def current(engine: Engine, hold: Hold) -> Hold:
    fresh = await engine.vault.get_hold(hold.id)
    assert fresh is not None
    return fresh


async def test_a_crashing_check_keeps_the_brake_on_and_says_so(clock, setup) -> None:  # type: ignore[no-untyped-def]
    engine, hold = await held_payment(clock, setup, Scripted(CLEAN), Scripted(RuntimeError("gateway down")))
    fresh = await current(engine, hold)
    assert fresh.release_at_ms == hold.release_at_ms + HOUR
    assert fresh.reason_bits == Reason.AI_UNAVAILABLE
    text = setup[2].last()
    assert text.startswith("Brakes on - held until")
    assert "The AI check could not run (the AI service failed with RuntimeError)" in text
    assert "We will try again." in text
    assert (await engine.stats())["fail_closed"] == 1


async def test_a_budget_error_from_the_pipeline_is_reported_as_budget(clock, setup) -> None:  # type: ignore[no-untyped-def]
    class BudgetExceeded(Exception):
        pass

    engine, hold = await held_payment(clock, setup, Scripted(CLEAN), Scripted(BudgetExceeded("403 budget_exceeded")))
    fresh = await current(engine, hold)
    assert fresh.reason_bits == Reason.BUDGET_EXHAUSTED
    assert "The AI check budget is used up" in setup[2].last()


@pytest.mark.parametrize(
    ("result", "bit", "phrase"),
    [(UNAVAILABLE, Reason.AI_UNAVAILABLE, "could not run (all candidates failed)"),
     (NO_BUDGET, Reason.BUDGET_EXHAUSTED, "The AI check budget is used up")],
)
async def test_failure_verdicts_fail_closed(clock, setup, result: Verdict, bit: Reason, phrase: str) -> None:  # type: ignore[no-untyped-def]
    engine, hold = await held_payment(clock, setup, Scripted(CLEAN), Scripted(result))
    fresh = await current(engine, hold)
    assert fresh.reason_bits & bit
    assert fresh.release_at_ms == hold.release_at_ms + HOUR
    assert phrase in setup[2].last()


async def test_each_failure_adds_an_hour_up_to_six_hours(clock, setup) -> None:  # type: ignore[no-untyped-def]
    full = Scripted(RuntimeError("down"))
    engine, hold = await held_payment(clock, setup, Scripted(CLEAN), full)
    releases = [(await current(engine, hold)).release_at_ms]
    for _ in range(8):
        clock.advance(31 * MINUTE)
        await engine.tick()
        await engine.wait_idle()
        releases.append((await current(engine, hold)).release_at_ms)
    extra = [release - hold.release_at_ms for release in releases]
    assert extra[:6] == [h * HOUR for h in range(1, 7)]
    assert max(extra) == 6 * HOUR  # never more than six hours of fail-closed waiting
    assert full.calls == 6  # the sixth failure is final: no more retries
    assert "still failing after the extra waiting time" in setup[2].last()
    assert await engine.tick() == 0


async def test_retries_wait_for_their_time(clock, setup) -> None:  # type: ignore[no-untyped-def]
    full = Scripted(RuntimeError("down"))
    engine, _ = await held_payment(clock, setup, Scripted(CLEAN), full)
    assert await engine.tick() == 0
    clock.advance(4 * MINUTE)
    assert await engine.tick() == 0
    clock.advance(MINUTE)
    assert await engine.tick() == 1
    await engine.wait_idle()
    assert full.calls == 2


async def test_a_successful_retry_applies_the_real_verdict(clock, setup) -> None:  # type: ignore[no-untyped-def]
    full = Scripted(RuntimeError("down"), brake(Reason.IMPERSONATION, 24 * HOUR, "triage"))
    engine, hold = await held_payment(clock, setup, Scripted(CLEAN), full)
    clock.advance(5 * MINUTE)
    await engine.tick()
    await engine.wait_idle()
    fresh = await current(engine, hold)
    assert fresh.release_at_ms == clock() + 24 * HOUR
    assert fresh.reason_bits == Reason.AI_UNAVAILABLE | Reason.IMPERSONATION
    assert setup[2].last().startswith("Brakes on - held until")
    clock.advance(HOUR)
    assert await engine.tick() == 0  # nothing left to retry


async def test_with_the_ai_down_a_strong_rules_verdict_still_counts(clock, setup) -> None:  # type: ignore[no-untyped-def]
    fast = Scripted(brake(Reason.PAYEE_REPORTED, 24 * HOUR))
    engine, hold = await held_payment(clock, setup, fast, Scripted(RuntimeError("down")))
    fresh = await current(engine, hold)
    assert fresh.release_at_ms == clock() + 24 * HOUR
    assert fresh.reason_bits == Reason.PAYEE_REPORTED | Reason.AI_UNAVAILABLE


async def test_main_branch_rules_failure_also_fails_closed(clock, setup) -> None:  # type: ignore[no-untyped-def]
    fast = Scripted(RuntimeError("intel timeout"), CLEAN)
    engine, hold = await held_payment(clock, setup, fast, None)
    assert (await current(engine, hold)).release_at_ms == hold.release_at_ms + HOUR
    clock.advance(5 * MINUTE)
    await engine.tick()
    await engine.wait_idle()
    assert setup[2].last().startswith("Full check finished")  # the retry ran the rules again


async def test_a_check_slower_than_the_hold_keeps_the_brake_on(clock, setup) -> None:  # type: ignore[no-untyped-def]
    vault, store, notifier = setup

    class Slow:
        async def assess(self, hold: Hold, ctx: PaymentContext) -> Verdict:
            await asyncio.sleep(0.2)
            return replace(brake(Reason.URGENCY, 24 * HOUR, "triage"), hold_id=hold.id)

    settings = offline_settings()
    engine = Engine(settings, vault, Scripted(CLEAN), Slow(), store, notifier, clock=clock)
    engine.deadline_margin_ms = 19_950  # with a 20 s hold, the guard fires after 0.05 s
    await engine.start("tg:1", "Priya")
    outcome = await engine.pay("tg:1", PAYEE, settings.husd_token or "", HUSD)
    await engine.wait_idle()
    texts = [text for _, text in notifier.sent]
    assert any("taking longer than the hold" in text for text in texts)
    fresh = await vault.get_hold(outcome.hold.id)  # type: ignore[union-attr]
    assert fresh is not None and fresh.release_at_ms == clock() + 24 * HOUR
    assert fresh.reason_bits == Reason.AI_UNAVAILABLE | Reason.URGENCY
    assert await engine.tick() == 0  # the slow check finished, so there is nothing to retry


async def test_a_cancelled_payment_is_left_alone(clock, setup) -> None:  # type: ignore[no-untyped-def]
    vault, store, notifier = setup
    gate = asyncio.Event()

    class Waiting:
        async def assess(self, hold: Hold, ctx: PaymentContext) -> Verdict:
            await gate.wait()
            return replace(brake(Reason.URGENCY, 24 * HOUR, "triage"), hold_id=hold.id)

    settings = offline_settings()
    engine = Engine(settings, vault, Scripted(CLEAN), Waiting(), store, notifier, clock=clock)
    await engine.start("tg:1", "Priya")
    outcome = await engine.pay("tg:1", PAYEE, settings.husd_token or "", HUSD)
    await engine.cancel("tg:1", outcome.hold.id)  # type: ignore[union-attr]
    gate.set()
    await engine.wait_idle()
    fresh = await vault.get_hold(outcome.hold.id)  # type: ignore[union-attr]
    assert fresh is not None and fresh.extensions == 0
    assert notifier.sent[-1][1].startswith("Cancelled.")


async def test_when_the_chain_refuses_the_brake_the_user_is_warned(clock, setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    vault, _, notifier = setup

    async def refuse(*args: Any) -> Any:
        raise VaultError(E.E_RPC, "node unreachable")

    monkeypatch.setattr(vault, "extend_hold", refuse)
    await held_payment(clock, setup, Scripted(brake(Reason.MULE_PATTERN, HOUR)), None)
    text = notifier.last()
    assert "We could not add time on-chain" in text and "Cancel now if you are not sure." in text
