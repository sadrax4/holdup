"""The oracle clamps, extends only when it adds time, stores matching evidence and is idempotent."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from holdup.chain import errors as E
from holdup.chain.errors import VaultError
from holdup.chain.fake import FakeVault, SimClock
from holdup.chain.keys import derive_user
from holdup.config import offline_settings
from holdup.models import NEAR, Finding, Hold, HoldStatus, Reason, StepRecord, Verdict
from holdup.runtime import offline_vault
from holdup.store import Store
from holdup.workers.keeper import Keeper
from holdup.workers.oracle import Oracle, fallback_evidence

HOUR = 3_600_000
MAX_HOLD = 72 * HOUR


@pytest.fixture
def clock() -> SimClock:
    return SimClock()


@pytest.fixture
def vault(clock: SimClock) -> FakeVault:
    return offline_vault(offline_settings(), clock)


@pytest.fixture
def store(clock: SimClock) -> Store:
    return Store(":memory:", clock=clock)


async def new_hold(vault: FakeVault, payee: str = "ravi.testnet") -> Hold:
    user = derive_user("secret", "tg:1", "holdup.testnet")
    await vault.onboard_user(user, {NEAR: 10 * 10**24})
    return (await vault.send(user, payee, NEAR, 10**24)).hold


def flagged(hold_for: int, *reasons: Reason) -> Verdict:
    findings = [Finding(r, f"{r.name} detail", "triage", "openai/gpt-oss-20b") for r in reasons or (Reason.NEW_ACCOUNT,)]
    steps = [StepRecord("triage", "openai/gpt-oss-20b", "groq", False, 1, "req", 0.001, 500, prompt_sha256="ab" * 32)]
    return Verdict(0, True, hold_for, findings, steps)


async def test_target_is_now_plus_hold_for_and_evidence_matches_the_chain(vault, store, clock) -> None:  # type: ignore[no-untyped-def]
    hold = await new_hold(vault)
    clock.advance(5_000)
    result = await Oracle(vault, store).apply(hold, flagged(24 * HOUR, Reason.IMPERSONATION))
    assert result.extended and result.release_at_ms == clock() + 24 * HOUR
    fresh = await vault.get_hold(hold.id)
    assert fresh is not None and fresh.release_at_ms == result.release_at_ms
    assert fresh.evidence_hash == result.evidence_hash == hashlib.sha256(result.evidence_json.encode()).hexdigest()  # type: ignore[union-attr]
    stored = store.get_evidence(hold.id)
    assert stored is not None and stored.evidence_hash == fresh.evidence_hash
    assert store.get_txs(hold.id) == [("extend", result.tx.tx_hash)]  # type: ignore[union-attr]
    document = json.loads(result.evidence_json)  # type: ignore[arg-type]
    assert document["hold"] == hold.id and document["vault"] == vault.vault_id


async def test_target_is_clamped_to_the_maximum_hold(vault, store, clock) -> None:  # type: ignore[no-untyped-def]
    hold = await new_hold(vault)
    clock.advance(HOUR)
    result = await Oracle(vault, store).apply(hold, flagged(100 * HOUR))
    assert result.release_at_ms == hold.created_ms + MAX_HOLD
    again = await Oracle(vault, store).apply(hold, flagged(200 * HOUR, Reason.URGENCY))
    assert not again.extended and again.skipped == "not_longer"


async def test_nothing_is_sent_unless_the_hold_gets_longer(vault, store) -> None:  # type: ignore[no-untyped-def]
    hold = await new_hold(vault)
    sent = len(vault.transactions)
    short = await Oracle(vault, store).apply(hold, flagged(5_000))  # 5 s is less than the 20 s hold
    assert (short.extended, short.skipped) == (False, "not_longer")
    clean = await Oracle(vault, store).apply(hold, Verdict(hold.id, False, 0))
    assert clean.skipped == "clean"
    assert len(vault.transactions) == sent
    assert store.get_evidence(hold.id) is None


async def test_applying_the_same_verdict_twice_sends_one_transaction(vault, store, clock) -> None:  # type: ignore[no-untyped-def]
    hold = await new_hold(vault)
    oracle = Oracle(vault, store)
    verdict = flagged(HOUR)
    first = await oracle.apply(hold, verdict)
    clock.advance(10 * 60_000)  # later, now + 1 h would be later still
    second = await oracle.apply(hold, verdict)
    assert second is first
    assert sum(tx.method == "extend_hold" for tx in vault.transactions) == 1


async def test_settled_holds_are_skipped(vault, store) -> None:  # type: ignore[no-untyped-def]
    hold = await new_hold(vault)
    await vault.cancel(derive_user("secret", "tg:1", "holdup.testnet"), hold.id)
    result = await Oracle(vault, store).apply(hold, flagged(HOUR))
    assert (result.extended, result.skipped) == (False, "inactive")
    missing = await Oracle(vault, store).apply(Hold(99, "a", "b", NEAR, 1, 0, 0, HoldStatus.HELD), flagged(HOUR))
    assert missing.skipped == "missing"


async def test_a_racing_extension_is_recomputed(vault, store, clock) -> None:  # type: ignore[no-untyped-def]
    hold = await new_hold(vault)
    real_extend = vault.extend_hold
    raced = False

    async def racing(hold_id: int, release_at_ms: int, bits: int, evidence: str) -> Any:
        nonlocal raced
        if not raced:  # another oracle call lands first with a later time
            raced = True
            await real_extend(hold_id, release_at_ms + 1, 1, "{}")
        return await real_extend(hold_id, release_at_ms, bits, evidence)

    vault.extend_hold = racing  # type: ignore[method-assign]
    result = await Oracle(vault, store).apply(hold, flagged(2 * HOUR))
    # Our call hit E_ONLY_LONGER; after re-reading, the hold is already longer than we wanted.
    assert (result.extended, result.skipped) == (False, "not_longer")
    assert result.release_at_ms == clock() + 2 * HOUR + 1
    assert [tx.ok for tx in vault.transactions if tx.method == "extend_hold"] == [True, False]
    assert store.get_evidence(hold.id) is None


async def test_an_absolute_target_is_supported(vault, store) -> None:  # type: ignore[no-untyped-def]
    hold = await new_hold(vault)
    target = hold.release_at_ms + 3 * HOUR
    result = await Oracle(vault, store).apply(hold, flagged(0, Reason.AI_UNAVAILABLE), release_at_ms=target)
    assert result.release_at_ms == target


async def test_a_broken_evidence_builder_does_not_stop_the_brake(vault, store) -> None:  # type: ignore[no-untyped-def]
    def broken(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("bug")

    hold = await new_hold(vault)
    result = await Oracle(vault, store, evidence_builder=broken).apply(hold, flagged(HOUR))
    assert result.extended
    assert json.loads(result.evidence_json)["v"] == 1  # type: ignore[arg-type]


def test_fallback_evidence_is_canonical_and_bounded() -> None:
    hold = Hold(3, "a.testnet", "b.testnet", NEAR, 5, 0, 1, HoldStatus.HELD)
    findings = [Finding(Reason.URGENCY, "x" * 500, "triage", "m", [f"https://e.example/{i}" for i in range(5)])] * 20
    verdict = Verdict(3, True, HOUR, findings, [StepRecord("triage")] * 20)
    text = fallback_evidence(hold, verdict, network="testnet", vault_id="v.testnet", prompt_versions={"triage": "ab"})
    assert len(text.encode()) <= 4096
    document = json.loads(text)
    assert text == json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert all(len(r["detail"]) <= 160 and len(r["citations"]) <= 2 for r in document["reasons"])


async def test_keeper_releases_due_holds_and_reports_them(vault, clock) -> None:  # type: ignore[no-untyped-def]
    first = await new_hold(vault)
    clock.advance(10_000)
    second = await new_hold(vault, "mum.testnet")
    settled: list[Hold] = []
    ticks = 0

    async def on_settled(hold: Hold, tx: Any) -> None:
        settled.append(hold)

    async def on_tick() -> None:
        nonlocal ticks
        ticks += 1

    keeper = Keeper(vault, on_settled=on_settled, on_tick=on_tick)
    clock.advance(10_000)
    assert [r.hold.id for r in await keeper.run_once()] == [first.id]
    clock.advance(10_000)
    assert [r.hold.id for r in await keeper.run_once()] == [second.id]
    assert [h.status for h in settled] == [HoldStatus.RELEASED, HoldStatus.RELEASED]
    assert ticks == 2


async def test_keeper_skips_holds_someone_else_settled(vault, clock) -> None:  # type: ignore[no-untyped-def]
    hold = await new_hold(vault)
    clock.advance(20_000)
    real_release = vault.release

    async def already_released(hold_id: int) -> Any:
        await real_release(hold_id)
        raise VaultError(E.E_HOLD_NOT_ACTIVE, "hold is not held")

    vault.release = already_released  # type: ignore[method-assign]
    assert await Keeper(vault).run_once() == []
    assert (await vault.get_hold(hold.id)).status == HoldStatus.RELEASED  # type: ignore[union-attr]
