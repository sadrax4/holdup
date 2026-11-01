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
from holdup import messages
from holdup.models import (
    STILL_CHECKING,
    STILL_CHECKING_LABEL,
    Finding,
    Hold,
    HoldStatus,
    PaymentContext,
    Reason,
    Verdict,
)
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
            clock.advance(20_000)  # the hold's time passes on the vault's clock while the check runs
            await asyncio.sleep(0.2)
            return replace(brake(Reason.URGENCY, 24 * HOUR, "triage"), hold_id=hold.id)

    settings = offline_settings()
    engine = Engine(settings, vault, Scripted(CLEAN), Slow(), store, notifier, clock=clock)
    engine.deadline_margin_ms = 19_950  # with a 20 s hold, the guard looks after 0.05 s
    await engine.start("tg:1", "Priya")
    outcome = await engine.pay("tg:1", PAYEE, settings.husd_token or "", HUSD)
    await engine.wait_idle()
    texts = [text for _, text in notifier.sent]
    assert any("taking longer than the hold" in text for text in texts)
    fresh = await vault.get_hold(outcome.hold.id)  # type: ignore[union-attr]
    assert fresh is not None and fresh.release_at_ms == clock() + 24 * HOUR
    assert fresh.reason_bits == Reason.AI_UNAVAILABLE | Reason.URGENCY
    assert await engine.tick() == 0  # the slow check finished, so there is nothing to retry


async def test_a_slow_check_is_recorded_as_still_running_not_as_a_failure(clock, setup) -> None:  # type: ignore[no-untyped-def]
    """Live 2026-10-31: every honest payment to a new payee showed 'AI check could not run ... AI check failed:
    it is still running [fail-closed]', and /stats counted it as fail-closed."""
    from holdup.web import views

    vault, store, notifier = setup

    class Slow:
        async def assess(self, hold: Hold, ctx: PaymentContext) -> Verdict:
            clock.advance(20_000)
            await asyncio.sleep(0.2)
            return replace(CLEAN, hold_id=hold.id)

    settings = offline_settings()
    engine = Engine(settings, vault, Scripted(CLEAN), Slow(), store, notifier, clock=clock)
    engine.deadline_margin_ms = 19_950
    await engine.start("tg:1", "Priya")
    outcome = await engine.pay("tg:1", PAYEE, settings.husd_token or "", HUSD)
    await engine.wait_idle()
    slow = store.list_verdicts(outcome.hold.id)[0]  # type: ignore[union-attr]
    (finding,) = [f for f in slow["findings"] if f["reason"] == "AI_UNAVAILABLE"]
    assert finding["source"] == STILL_CHECKING and "failed" not in finding["detail"]
    stats = await engine.stats()
    assert (stats["fail_closed"], stats["braked_holds"]) == (0, 1)
    shown = Finding(Reason.AI_UNAVAILABLE, finding["detail"], finding["source"])
    (row,) = views.reason_rows([shown])
    assert row.label == STILL_CHECKING_LABEL and "[still checking]" in messages.reason_lines([shown])[0]
    assert views._extension_title([shown], {}) == STILL_CHECKING_LABEL


async def test_a_check_slower_than_the_hold_in_real_time_only_is_not_braked(clock, setup) -> None:  # type: ignore[no-untyped-def]
    """The offline demo with live AI: simulated time stands still while a 17 s check runs."""
    vault, store, notifier = setup

    class Slow:
        async def assess(self, hold: Hold, ctx: PaymentContext) -> Verdict:
            await asyncio.sleep(0.2)
            return replace(CLEAN, hold_id=hold.id)

    settings = offline_settings()
    engine = Engine(settings, vault, Scripted(CLEAN), Slow(), store, notifier, clock=clock)
    engine.deadline_margin_ms = 19_950  # with a 20 s hold, the guard looks after 0.05 s
    await engine.start("tg:1", "Priya")
    outcome = await engine.pay("tg:1", PAYEE, settings.husd_token or "", HUSD)
    await engine.wait_idle()
    assert not any("taking longer than the hold" in text for _, text in notifier.sent)
    fresh = await vault.get_hold(outcome.hold.id)  # type: ignore[union-attr]
    assert fresh is not None and fresh.extensions == 0 and fresh.reason_bits == 0
    assert fresh.release_at_ms == outcome.hold.release_at_ms  # type: ignore[union-attr]
    clock.advance(21_000)
    released = await engine.make_keeper().run_once()
    assert released and (await vault.get_hold(fresh.id)).status == HoldStatus.RELEASED  # type: ignore[union-attr]


async def test_on_a_real_network_a_wall_clock_step_does_not_delay_the_guard(clock, setup) -> None:  # type: ignore[no-untyped-def]
    """The hold runs out on chain time, so the guard waits in real time even if the server's clock steps back."""
    vault, store, notifier = setup
    wall = SimClock(clock())

    class Slow:
        async def assess(self, hold: Hold, ctx: PaymentContext) -> Verdict:
            wall.advance(-60_000)
            await asyncio.sleep(0.3)
            return replace(CLEAN, hold_id=hold.id)

    settings = offline_settings()
    engine = Engine(settings, vault, Scripted(CLEAN), Slow(), store, notifier, clock=wall)
    engine.simulated = False  # as on testnet: the vault keeps chain time, the engine reads the wall clock
    engine.deadline_margin_ms = 19_950  # with a 20 s hold, the guard looks after 0.05 s
    await engine.start("tg:1", "Priya")
    await engine.pay("tg:1", PAYEE, settings.husd_token or "", HUSD)
    await asyncio.wait_for(engine.wait_idle(), 10)
    assert any("taking longer than the hold" in text for _, text in notifier.sent)


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


# ----------------------------------------------------------------------
# a brake that cannot reach the chain is retried, never dropped
# ----------------------------------------------------------------------


class FlakyChain:
    """``extend_hold`` fails like an unreachable RPC node while ``down`` is set."""

    def __init__(self, vault: FakeVault, monkeypatch: Any) -> None:
        self.down = True
        self.failures = 0
        real = vault.extend_hold

        async def extend_hold(*args: Any) -> Any:
            if self.down:
                self.failures += 1
                raise VaultError(E.E_RPC, "RPC timeout")
            return await real(*args)

        monkeypatch.setattr(vault, "extend_hold", extend_hold)


async def test_a_brake_that_fails_on_chain_is_retried_and_the_keeper_waits(clock, setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The review's scenario: the RPC node is down while both brakes are sent. Before, nothing retried
    them and the keeper released the payment at its original time."""
    vault, _, notifier = setup
    chain = FlakyChain(vault, monkeypatch)
    fast = Scripted(brake(Reason.NEW_ACCOUNT, HOUR))
    full = Scripted(brake(Reason.IMPERSONATION, 72 * HOUR, "triage"))
    engine, hold = await held_payment(clock, setup, fast, full)
    assert chain.failures == 2  # the quick brake and the full verdict
    assert (await current(engine, hold)).extensions == 0
    warnings = [text for _, text in notifier.sent if "could not add time on-chain" in text]
    assert len(warnings) == 1 and "HoldUp will not release it meanwhile" in warnings[0]

    keeper = engine.make_keeper()
    clock.advance(hold.release_at_ms - clock() + 5_000)  # due, and the node is still down
    await keeper.run_once()
    await engine.wait_idle()
    assert (await current(engine, hold)).status == HoldStatus.HELD
    assert chain.failures == 3

    chain.down = False
    clock.advance(5_000)
    await keeper.run_once()
    await engine.wait_idle()
    fresh = await current(engine, hold)
    assert (fresh.status, fresh.extensions) == (HoldStatus.HELD, 1)
    assert fresh.reason_bits == Reason.IMPERSONATION  # the newest verdict replaced the quick brake
    assert fresh.release_at_ms == hold.created_ms + 72 * HOUR
    assert notifier.last().startswith("Brakes on - held until")
    await keeper.run_once()
    await engine.wait_idle()
    assert (await current(engine, hold)).status == HoldStatus.HELD
    assert (await engine.stats())["brakes"] == 1


async def test_a_pending_brake_ends_when_the_payment_is_cancelled(clock, setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    vault, _, _ = setup
    chain = FlakyChain(vault, monkeypatch)
    engine, hold = await held_payment(clock, setup, Scripted(brake(Reason.MULE_PATTERN, HOUR)), None)
    assert engine.brake_pending(hold)
    await engine.cancel("tg:1", hold.id)
    assert not engine.brake_pending(hold)
    await engine.tick()
    await engine.wait_idle()
    assert chain.failures == 1


async def test_a_pending_brake_gives_up_at_the_maximum_hold(clock, setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Past created + max hold no extension is possible, so the keeper may release again."""
    vault, _, _ = setup
    FlakyChain(vault, monkeypatch)
    engine, hold = await held_payment(clock, setup, Scripted(brake(Reason.MULE_PATTERN, HOUR)), None)
    keeper = engine.make_keeper()
    clock.advance(72 * HOUR)
    await keeper.run_once()
    await engine.wait_idle()
    assert (await current(engine, hold)).status == HoldStatus.HELD
    assert not engine.brake_pending(hold)
    await keeper.run_once()
    assert (await current(engine, hold)).status == HoldStatus.RELEASED


async def test_a_failed_fail_closed_extension_is_retried_too(clock, setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    vault, _, notifier = setup
    chain = FlakyChain(vault, monkeypatch)
    engine, hold = await held_payment(clock, setup, Scripted(CLEAN), Scripted(RuntimeError("gateway down")))
    assert engine.brake_pending(hold) and chain.failures == 1
    chain.down = False
    await engine.tick()
    await engine.wait_idle()
    fresh = await current(engine, hold)
    assert fresh.extensions >= 1 and fresh.reason_bits & Reason.AI_UNAVAILABLE
    assert not engine.brake_pending(hold)
    assert any(text.startswith("Brakes on - held until") for _, text in notifier.sent)


# ----------------------------------------------------------------------
# one brake per decision
# ----------------------------------------------------------------------


async def test_a_full_check_that_adds_nothing_does_not_brake_twice(clock, setup) -> None:  # type: ignore[no-untyped-def]
    """The rules flag, the full check confirms only that: one extension and one "Brakes on" (review finding)."""
    rules = brake(Reason.NEW_ACCOUNT, HOUR)
    engine, hold = await held_payment(clock, setup, Scripted(rules), Scripted(rules))
    fresh = await current(engine, hold)
    assert fresh.extensions == 1
    texts = [text for _, text in setup[2].sent]
    assert sum(text.startswith("Brakes on") for text in texts) == 1
    assert texts[-1].startswith("Full check finished: no further warning signs.")
    stats = await engine.stats()
    assert (stats["provisional_brakes"], stats["brakes"]) == (1, 0)


async def test_new_reasons_that_cannot_extend_are_still_reported_but_not_counted(clock, setup) -> None:  # type: ignore[no-untyped-def]
    full = Verdict(0, True, HOUR, [Finding(Reason.NEW_ACCOUNT, "new", "rules"), Finding(Reason.URGENCY, "rush", "triage")])
    engine, hold = await held_payment(clock, setup, Scripted(brake(Reason.NEW_ACCOUNT, HOUR)), Scripted(full))
    assert (await current(engine, hold)).extensions == 1  # the target did not move, so nothing was sent
    last = setup[2].last()
    assert last.startswith("Brakes on - held until") and "Pressure or an artificial deadline" in last
    assert (await engine.stats())["brakes"] == 0


async def test_a_judgement_that_waited_for_a_failed_step_holds_a_day_once_the_retries_are_spent(  # type: ignore[no-untyped-def]
    clock, setup
) -> None:
    """Review: with the reviewer down for good, triage's reading used to release at +6 h; it braked 24 h before."""
    waited = Verdict(
        0,
        True,
        24 * HOUR,
        [
            Finding(Reason.AI_UNAVAILABLE, "the reviewer step could not run (all_candidates_failed)", "reviewer"),
            Finding(Reason.IMPERSONATION, "claims to come from a bank", "triage", "openai/gpt-oss-20b", context=True),
        ],
        summary="fail-closed: triage nothing found, 1 as context; reviewer failed; a judgement waited",
    )
    full = Scripted(waited)
    engine, hold = await held_payment(clock, setup, Scripted(CLEAN), full)
    # Retries first: the reviewer may be back within the hour, and an honest payment then goes on time.
    assert (await current(engine, hold)).release_at_ms == hold.release_at_ms + HOUR
    assert "We will try again." in setup[2].last()
    for _ in range(5):
        clock.advance(31 * MINUTE)
        await engine.tick()
        await engine.wait_idle()
    assert full.calls == 6
    fresh = await current(engine, hold)
    assert fresh.release_at_ms == clock() + 24 * HOUR  # the day the judgement held before it was weighed
    assert fresh.reason_bits == Reason.AI_UNAVAILABLE
    assert "still failing after the extra waiting time" in setup[2].last()
    assert await engine.tick() == 0


async def test_a_retry_that_finds_the_reviewer_back_adds_no_more_time(clock, setup) -> None:  # type: ignore[no-untyped-def]
    failed = Finding(Reason.AI_UNAVAILABLE, "the reviewer step could not run", "reviewer")
    waited = Verdict(0, True, 24 * HOUR, [failed])
    full = Scripted(waited, CLEAN)
    engine, hold = await held_payment(clock, setup, Scripted(CLEAN), full)
    clock.advance(5 * MINUTE)
    await engine.tick()
    await engine.wait_idle()
    assert full.calls == 2
    assert (await current(engine, hold)).release_at_ms == hold.release_at_ms + HOUR  # no longer than the failure
    assert await engine.tick() == 0
