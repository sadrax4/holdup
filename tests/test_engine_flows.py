"""Engine flows on the in-memory vault with scripted risk checks."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
from dataclasses import dataclass, replace
from typing import Any

import pytest

from holdup.chain import errors as E
from holdup.chain.errors import VaultError
from holdup.chain.fake import FakeVault, SimClock
from holdup.config import Settings
from holdup.engine import CONTEXT_TTL_MS, Engine
from holdup.models import (
    NEAR,
    Finding,
    Hold,
    HoldStatus,
    PaymentContext,
    Reason,
    StepRecord,
    Verdict,
)
from holdup.store import Store
from holdup.config import offline_settings
from holdup.runtime import offline_vault

HOUR = 3_600_000
HUSD = 10**6
WALLET = "safe-wallet-77.testnet"
FRIEND = "ravi.testnet"


class Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, Any]] = []

    async def notify(self, user_key: str, text: str, buttons: Any = None) -> None:
        self.sent.append((user_key, text, buttons))

    def texts(self, user_key: str) -> list[str]:
        return [text for key, text, _ in self.sent if key == user_key]

    def buttons(self, user_key: str) -> list[Any]:
        return [buttons for key, _, buttons in self.sent if key == user_key and buttons]


class StubRisk:
    """Returns the scripted verdicts in order (the last one repeats) and records each request."""

    def __init__(self, *results: Verdict | Exception) -> None:
        self.results = list(results)
        self.calls: list[tuple[Hold, PaymentContext]] = []

    async def assess(self, hold: Hold, ctx: PaymentContext) -> Verdict:
        self.calls.append((hold, ctx))
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return replace(result, hold_id=hold.id)


def verdict(*reasons: Reason, hold_for: int = HOUR, source: str = "rules", model: str | None = None) -> Verdict:
    findings = [Finding(reason, f"{reason.name.lower()} found", source, model) for reason in reasons]
    steps = [StepRecord("triage", model, "groq", False, 1, "req-1", 0.0012, 800)] if model else []
    return Verdict(hold_id=0, brake=bool(reasons), hold_for_ms=hold_for if reasons else 0, findings=findings, steps=steps)


CLEAN = verdict()


@dataclass
class World:
    clock: SimClock
    settings: Settings
    vault: FakeVault
    store: Store
    notifier: Recorder

    def engine(self, fast: Any, full: Any = None) -> Engine:
        return Engine(self.settings, self.vault, fast, full, self.store, self.notifier, clock=self.clock)


@pytest.fixture
def world() -> World:
    clock = SimClock()
    settings = offline_settings(public_base_url="https://holdup.example")
    return World(clock, settings, offline_vault(settings, clock), Store(":memory:", clock=clock), Recorder())


@pytest.fixture
def husd(world: World) -> str:
    assert world.settings.husd_token
    return world.settings.husd_token


async def test_start_onboards_each_user_once(world: World, husd: str) -> None:
    engine = world.engine(StubRisk(CLEAN))
    account, created = await engine.start("tg:1", "Priya", chat_ref="chat-1")
    assert created and account.display_name == "Priya"
    assert account == engine.account_for("tg:1")
    assert await engine.balance("tg:1") == {NEAR: 2 * 10**24, husd: 2_000 * HUSD}
    again, created_again = await engine.start("tg:1", "Priya")
    assert not created_again and again.account_id == account.account_id
    assert world.store.user_by_account(account.account_id)["user_key"] == "tg:1"  # type: ignore[index]
    assert (await engine.stats())["users"] == 1


async def test_refusals_send_nothing(world: World, husd: str) -> None:
    engine = world.engine(StubRisk(CLEAN))
    outcome = await engine.pay("tg:9", WALLET, husd, HUSD)
    assert outcome.refused == E.E_NOT_REGISTERED
    await engine.start("tg:1", "Priya")
    me = engine.account_for("tg:1").account_id
    cases = {
        E.UNKNOWN_PAYEE: ("nobody-here.testnet", husd, HUSD),
        E.INVALID_PAYEE: ("Not Valid!", husd, HUSD),
        E.E_SELF_PAYMENT: (me, husd, HUSD),
        E.E_ZERO_AMOUNT: (WALLET, husd, 0),
        E.E_UNKNOWN_ASSET: (WALLET, "usdc.fake.testnet", HUSD),
        E.E_INSUFFICIENT_BALANCE: (WALLET, husd, 10_000 * HUSD),
    }
    for code, (payee, asset, amount) in cases.items():
        outcome = await engine.pay("tg:1", payee, asset, amount)
        assert (outcome.refused, outcome.hold) == (code, None)
        assert outcome.message
    assert not [tx for tx in world.vault.transactions if tx.method == "send"]
    assert "nobody-here.testnet does not exist" in (await engine.pay("tg:1", "nobody-here.testnet", husd, 1)).message


async def test_a_held_payment_is_announced_and_checked_in_the_background(world: World, husd: str) -> None:
    fast = StubRisk(CLEAN)
    engine = world.engine(fast)
    await engine.start("tg:1", "Priya")
    outcome = await engine.pay("tg:1", FRIEND, husd, 12 * HUSD)
    assert outcome.hold is not None and not outcome.instant and outcome.refused is None
    assert outcome.message.startswith("Payment held: 12 HUSD to ravi.testnet.")
    hold_id = outcome.hold.id
    assert world.notifier.buttons("tg:1")[0] == [[("Cancel & refund", f"cancel:{hold_id}"), ("Why?", f"why:{hold_id}")]]
    await engine.wait_idle()
    assert world.notifier.texts("tg:1")[-1].startswith("Checks finished: nothing unusual found.")
    hold = await world.vault.get_hold(hold_id)
    assert hold is not None and hold.extensions == 0
    assert world.store.get_txs(hold_id)[0][0] == "send"


async def test_without_an_ai_step_the_rules_verdict_is_final(world: World, husd: str) -> None:
    engine = world.engine(StubRisk(verdict(Reason.NEW_ACCOUNT, hold_for=24 * HOUR)))
    await engine.start("tg:1", "Priya")
    outcome = await engine.pay("tg:1", WALLET, husd, 50 * HUSD)
    await engine.wait_idle()
    hold = await world.vault.get_hold(outcome.hold.id)  # type: ignore[union-attr]
    assert hold is not None and hold.release_at_ms == world.clock() + 24 * HOUR
    assert hold.reason_bits == Reason.NEW_ACCOUNT
    brake = world.notifier.texts("tg:1")[-1]
    assert brake.startswith("Brakes on - held until")
    assert "[rules]" in brake and "simulated" in brake
    assert (await engine.stats())["brakes"] == 1


async def test_provisional_brake_then_the_full_verdict(world: World, husd: str) -> None:
    fast = StubRisk(verdict(Reason.NEW_ACCOUNT, Reason.MULE_PATTERN, hold_for=HOUR))
    full = StubRisk(verdict(Reason.IMPERSONATION, Reason.URGENCY, hold_for=24 * HOUR, source="triage",
                            model="openai/gpt-oss-20b"))
    engine = world.engine(fast, full)
    await engine.start("tg:1", "Priya")
    outcome = await engine.pay("tg:1", WALLET, husd, 50 * HUSD)
    await engine.wait_idle()

    hold = await world.vault.get_hold(outcome.hold.id)  # type: ignore[union-attr]
    assert hold is not None and hold.extensions == 2
    assert hold.release_at_ms == world.clock() + 24 * HOUR
    assert hold.reason_bits == Reason.NEW_ACCOUNT | Reason.MULE_PATTERN | Reason.IMPERSONATION | Reason.URGENCY
    held, provisional, final = world.notifier.texts("tg:1")[-3:]
    assert "Quick checks found:" in provisional and "[rules]" in provisional
    assert "[triage gpt-oss-20b]" in final and "Nobody can release it early." in final
    kinds = [v["kind"] for v in world.store.list_verdicts(hold.id)]
    assert kinds == ["provisional", "final"]
    stats = await engine.stats()
    assert (stats["provisional_brakes"], stats["brakes"], stats["ai_checks"]) == (1, 1, 1)
    assert stats["ai_cost_usd"] == pytest.approx(0.0012)


async def test_a_clean_full_check_cannot_shorten_the_provisional_brake(world: World, husd: str) -> None:
    engine = world.engine(StubRisk(verdict(Reason.NEW_ACCOUNT)), StubRisk(CLEAN))
    await engine.start("tg:1", "Priya")
    outcome = await engine.pay("tg:1", WALLET, husd, 5 * HUSD)
    await engine.wait_idle()
    hold = await world.vault.get_hold(outcome.hold.id)  # type: ignore[union-attr]
    assert hold is not None and hold.release_at_ms == world.clock() + HOUR
    assert world.notifier.texts("tg:1")[-1].startswith("Full check finished: no further warning signs.")


async def test_a_clean_ai_check_cannot_weaken_a_strong_rules_brake(world: World, husd: str) -> None:
    engine = world.engine(StubRisk(verdict(Reason.PAYEE_REPORTED, hold_for=24 * HOUR)), StubRisk(CLEAN))
    await engine.start("tg:1", "Priya")
    outcome = await engine.pay("tg:1", WALLET, husd, 5 * HUSD)
    await engine.wait_idle()
    hold = await world.vault.get_hold(outcome.hold.id)  # type: ignore[union-attr]
    assert hold is not None and hold.release_at_ms == world.clock() + 24 * HOUR
    final = world.notifier.texts("tg:1")[-1]
    assert final.startswith("Brakes on - held until 3 Nov 09:00 UTC") and "[rules]" in final


async def test_trusted_payees_are_paid_instantly(world: World, husd: str) -> None:
    fast = StubRisk(CLEAN)
    engine = world.engine(fast)
    await engine.start("tg:1", "Priya")
    await engine.trust("tg:1", "mum.testnet")
    assert "will be a trusted payee from" in world.notifier.texts("tg:1")[-1]
    world.clock.advance(30_000)
    outcome = await engine.pay("tg:1", "mum.testnet", husd, 20 * HUSD)
    await engine.wait_idle()
    assert outcome.instant and outcome.hold is not None and outcome.hold.status == HoldStatus.RELEASED
    assert outcome.message.startswith("Paid instantly: 20 HUSD to mum.testnet")
    assert fast.calls == []
    assert world.vault.external_balance("mum.testnet", husd) == 20 * HUSD
    await engine.untrust("tg:1", "mum.testnet")
    assert "no longer trusted" in world.notifier.texts("tg:1")[-1]
    with pytest.raises(VaultError) as info:
        await engine.trust("tg:1", "nobody-here.testnet")
    assert info.value.code == E.UNKNOWN_PAYEE


async def test_cancel_refunds_and_counts_a_cancel_after_a_warning(world: World, husd: str) -> None:
    engine = world.engine(StubRisk(verdict(Reason.MULE_PATTERN)))
    await engine.start("tg:1", "Priya")
    outcome = await engine.pay("tg:1", WALLET, husd, 50 * HUSD)
    await engine.wait_idle()
    tx = await engine.cancel("tg:1", outcome.hold.id)  # type: ignore[union-attr]
    assert tx.events[0]["event"] == "hold_cancelled"
    assert (await engine.balance("tg:1"))[husd] == 2_000 * HUSD
    assert world.notifier.texts("tg:1")[-1].startswith("Cancelled. 50 HUSD is back in your HoldUp balance")
    assert (await world.vault.get_payee_stats(WALLET)).cancelled_after_flag == 1
    stats = await engine.stats()
    assert (stats["cancels"], stats["cancels_after_flag"]) == (1, 1)
    with pytest.raises(VaultError) as info:
        await engine.cancel("tg:1", outcome.hold.id)  # type: ignore[union-attr]
    assert info.value.code == E.E_HOLD_NOT_ACTIVE


async def test_guardian_invite_alert_and_cancel(world: World, husd: str) -> None:
    engine = world.engine(StubRisk(verdict(Reason.URGENCY)))
    await engine.start("tg:1", "Priya")
    code = await engine.guardian_invite("tg:1")
    with pytest.raises(VaultError) as own:
        await engine.accept_guardian("tg:1", await engine.guardian_invite("tg:1"))
    assert own.value.code == E.E_BAD_GUARDIAN
    await engine.accept_guardian("tg:2", code)  # Sam has no account yet: he is onboarded too
    with pytest.raises(VaultError) as reused:
        await engine.accept_guardian("tg:3", code)
    assert reused.value.code == E.INVALID_INVITE
    sam = engine.account_for("tg:2")
    assert (await world.vault.get_user(engine.account_for("tg:1").account_id)).guardian == sam.account_id  # type: ignore[union-attr]

    outcome = await engine.pay("tg:1", WALLET, husd, 50 * HUSD)
    await engine.wait_idle()
    hold_id = outcome.hold.id  # type: ignore[union-attr]
    alert = world.notifier.texts("tg:2")[-1]
    assert alert.startswith("Guardian alert: Priya's payment of 50 HUSD")
    assert world.notifier.buttons("tg:2")[-1] == [[("Cancel for Priya", f"gcancel:{hold_id}"), ("Why?", f"why:{hold_id}")]]

    with pytest.raises(VaultError) as stranger:
        await engine.guardian_cancel("tg:3", hold_id)
    assert stranger.value.code == E.E_NOT_GUARDIAN
    await engine.guardian_cancel("tg:2", hold_id)
    assert world.notifier.texts("tg:2")[-1].startswith("Done: you cancelled Priya's payment")
    assert world.notifier.texts("tg:1")[-1].startswith("Your guardian ")
    assert (await engine.balance("tg:1"))[husd] == 2_000 * HUSD
    assert (await engine.stats())["guardian_cancels"] == 1


async def test_forwarded_context_is_used_once_within_fifteen_minutes(world: World, husd: str) -> None:
    fast = StubRisk(CLEAN)
    engine = world.engine(fast)
    await engine.start("tg:1", "Priya")
    engine.remember_context("tg:1", PaymentContext(forwarded_text="Move your USDC within 30 minutes"))
    engine.remember_context("tg:1", PaymentContext(image_bytes=b"png", image_mime="image/png"))
    refused = await engine.pay("tg:1", "nobody-here.testnet", husd, HUSD)
    assert refused.refused == E.UNKNOWN_PAYEE  # a refused payment keeps the context
    first = await engine.pay("tg:1", WALLET, husd, HUSD)
    second = await engine.pay("tg:1", WALLET, husd, HUSD)
    await engine.wait_idle()
    ctx_first, ctx_second = fast.calls[0][1], fast.calls[1][1]
    assert ctx_first.forwarded_text == "Move your USDC within 30 minutes" and ctx_first.image_bytes == b"png"
    assert ctx_second.forwarded_text is None
    assert first.hold is not None and first.hold.memo_hash and len(first.hold.memo_hash) == 64
    assert "30 minutes" not in first.hold.memo_hash and second.hold.memo_hash is None  # type: ignore[union-attr]
    summary = world.store.get_context(first.hold.id)
    assert summary is not None and summary["has_text"] and summary["has_image"]

    engine.remember_context("tg:1", PaymentContext(forwarded_text="old news"))
    world.clock.advance(CONTEXT_TTL_MS + 1)
    await engine.pay("tg:1", WALLET, husd, HUSD)
    await engine.wait_idle()
    assert fast.calls[-1][1].forwarded_text is None


async def test_rules_see_the_effective_trusted_payees(world: World, husd: str) -> None:
    fast = StubRisk(CLEAN)
    engine = world.engine(fast)
    await engine.start("tg:1", "Dana")
    await engine.trust("tg:1", "northwindpaper.testnet")
    await engine.trust("tg:1", "mum.testnet")
    world.clock.advance(30_000)
    await engine.trust("tg:1", "ravi.testnet")  # still pending
    await engine.pay("tg:1", "northwind-paper.testnet", husd, HUSD)
    await engine.wait_idle()
    assert fast.calls[0][1].trusted_payees == ["mum.testnet", "northwindpaper.testnet"]


async def test_why_explains_a_brake_with_matching_evidence(world: World, husd: str) -> None:
    full = StubRisk(verdict(Reason.IMPERSONATION, hold_for=24 * HOUR, source="triage", model="openai/gpt-oss-20b"))
    engine = world.engine(StubRisk(verdict(Reason.NEW_ACCOUNT)), full)
    await engine.start("tg:1", "Priya")
    outcome = await engine.pay("tg:1", WALLET, husd, 50 * HUSD)
    await engine.wait_idle()
    view = await engine.why(outcome.hold.id)  # type: ignore[union-attr]
    assert view is not None
    assert {f.reason for f in view.findings} == {Reason.NEW_ACCOUNT, Reason.IMPERSONATION}
    assert [s.model for s in view.steps] == ["openai/gpt-oss-20b"]
    assert view.evidence_json is not None
    assert hashlib.sha256(view.evidence_json.encode()).hexdigest() == view.evidence_hash == view.hold.evidence_hash
    assert len(view.evidence_json.encode()) <= 4096
    labels = [label for label, _ in view.tx_links]
    assert labels[0].startswith("send ") and all(label.endswith("(simulated)") for label in labels)
    assert await engine.why(999) is None


async def test_csv_export_lists_holds_with_transactions(world: World, husd: str) -> None:
    engine = world.engine(StubRisk(verdict(Reason.NEW_ACCOUNT)))
    await engine.start("tg:1", "Priya")
    outcome = await engine.pay("tg:1", WALLET, husd, 12_500_000)
    await engine.wait_idle()
    await engine.cancel("tg:1", outcome.hold.id)  # type: ignore[union-attr]
    rows = list(csv.DictReader(io.StringIO(await engine.export_csv("tg:1"))))
    assert len(rows) == 1
    row = rows[0]
    assert (row["payee"], row["asset"], row["amount"], row["status"]) == (WALLET, "HUSD", "12.5", "cancelled")
    assert row["flagged"] == "yes" and row["reasons"] == "NEW_ACCOUNT"
    assert row["created_utc"] == "2026-11-02T09:00:00Z"
    assert [part.split(":")[0] for part in row["transactions"].split(";")] == ["send", "extend", "cancel"]
    assert row["hold_page"] == f"https://holdup.example/h/{outcome.hold.id}"  # type: ignore[union-attr]
    assert row["explorer_links"] == ""  # offline transactions have no explorer


async def test_csv_export_links_to_the_explorer_on_testnet(world: World, husd: str) -> None:
    world.settings = replace(world.settings, network="testnet")
    engine = world.engine(StubRisk(CLEAN))
    await engine.start("tg:1", "Priya")
    await engine.pay("tg:1", FRIEND, husd, HUSD)
    await engine.wait_idle()
    row = next(csv.DictReader(io.StringIO(await engine.export_csv("tg:1"))))
    tx_hash = row["transactions"].split(":")[1]
    assert row["explorer_links"] == f"https://testnet.near.rocks/tx/{tx_hash}"


async def test_the_keeper_releases_and_tells_sender_and_payee(world: World, husd: str) -> None:
    engine = world.engine(StubRisk(CLEAN))
    await engine.start("tg:1", "Priya")
    payee, _ = await engine.start("tg:2", "Ravi")
    outcome = await engine.pay("tg:1", payee.account_id, husd, 10 * HUSD)
    await engine.wait_idle()
    keeper = engine.make_keeper()
    assert await keeper.run_once() == []  # not due yet
    world.clock.advance(20_000)
    released = await keeper.run_once()
    assert [r.hold.status for r in released] == [HoldStatus.RELEASED]
    assert world.notifier.texts("tg:1")[-1].startswith("Released: 10 HUSD paid to")
    assert world.notifier.texts("tg:2")[-1] == "You received 10 HUSD from Priya."
    assert (await engine.balance("tg:2"))[husd] == 2_010 * HUSD
    assert [label for label, _ in world.store.get_txs(outcome.hold.id)] == ["send", "release"]  # type: ignore[union-attr]
    assert (await engine.stats())["releases"] == 1


async def test_a_failed_payout_is_reported_and_refunded(world: World, husd: str) -> None:
    engine = world.engine(StubRisk(CLEAN))
    await engine.start("tg:1", "Priya")
    world.vault.reject_payouts_to(FRIEND)
    await engine.pay("tg:1", FRIEND, husd, 10 * HUSD)
    await engine.wait_idle()
    world.clock.advance(20_000)
    (released,) = await engine.make_keeper().run_once()
    assert released.hold.status == HoldStatus.FAILED
    assert "payout to ravi.testnet failed" in world.notifier.texts("tg:1")[-1]
    assert (await engine.balance("tg:1"))[husd] == 2_000 * HUSD


async def test_wait_idle_waits_for_slow_checks(world: World, husd: str) -> None:
    started = asyncio.Event()

    class Slow:
        async def assess(self, hold: Hold, ctx: PaymentContext) -> Verdict:
            started.set()
            await asyncio.sleep(0.05)
            return replace(CLEAN, hold_id=hold.id)

    engine = world.engine(Slow())
    await engine.start("tg:1", "Priya")
    await engine.pay("tg:1", FRIEND, husd, HUSD)
    await started.wait()
    assert not world.notifier.texts("tg:1")[-1].startswith("Checks finished")
    await engine.wait_idle()
    assert world.notifier.texts("tg:1")[-1].startswith("Checks finished")


def test_a_real_network_needs_a_secret() -> None:
    class RealVault:
        simulated = False
        vault_id = "vault.holdup.testnet"

    settings = Settings(network="testnet", secret="")
    with pytest.raises(ValueError):
        Engine(settings, RealVault(), StubRisk(CLEAN), None, Store(":memory:"), Recorder())  # type: ignore[arg-type]


async def test_the_guardian_hears_about_new_trust_and_guardian_changes(world: World, husd: str) -> None:
    engine = world.engine(StubRisk(CLEAN))
    await engine.start("tg:1", "Priya")
    await engine.start("tg:2", "Sam")
    await engine.start("tg:3", "Leo")
    await engine.accept_guardian("tg:2", await engine.guardian_invite("tg:1"))

    await engine.trust("tg:1", "mum.testnet")
    assert world.notifier.texts("tg:2")[-1].startswith(
        "Priya added mum.testnet as a trusted payee; payments to it become instant from 09:00:30 UTC."
    )

    await engine.accept_guardian("tg:3", await engine.guardian_invite("tg:1"))
    assert world.notifier.texts("tg:2")[-1].startswith("Priya asked to make Leo their guardian instead of you")
    assert world.notifier.texts("tg:3")[-1].startswith("You are Priya's guardian from 09:00:30 UTC.")
    view = await world.vault.get_user(engine.account_for("tg:1").account_id)
    assert view is not None and view.guardian == engine.account_for("tg:2").account_id
    assert view.pending_guardian == engine.account_for("tg:3").account_id
