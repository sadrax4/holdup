"""The in-memory vault follows the HoldVault contract rules exactly."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from holdup.chain import errors as E
from holdup.chain.errors import VaultError
from holdup.chain.events import events_from_logs, first_item
from holdup.chain.fake import FakeVault, HoldVaultModel, SimClock
from holdup.chain.keys import USER_METHODS, derive_user
from holdup.models import NEAR, Hold, HoldStatus, VaultConfig

VAULT = "vault.holdup.testnet"
ORACLE = "oracle.holdup.testnet"
TOKEN = "husd.holdup.testnet"
ALICE, BOB, GUARD, SHOP = "alice.testnet", "bob.testnet", "guard.testnet", "shop.testnet"
YOCTO = 10**24
HUSD = 10**6
FEE = 10**22
HOUR = 3_600_000
HOLD_MS = 20_000
MAX_HOLD = 72 * HOUR


def make_config(**overrides: object) -> VaultConfig:
    values: dict[str, object] = {
        "oracles": [ORACLE],
        "tokens": [TOKEN],
        "new_payee_hold_ms": HOLD_MS,
        "max_hold_ms": MAX_HOLD,
        "trust_delay_ms": 30_000,
        "guardian_delay_ms": 30_000,
        "instant_caps": {NEAR: 10 * YOCTO, TOKEN: 100 * HUSD},
        "max_active_holds": 3,
        "registration_fee": FEE,
    }
    values.update(overrides)
    return VaultConfig(**values)  # type: ignore[arg-type]


@pytest.fixture
def clock() -> SimClock:
    return SimClock()


@pytest.fixture
def vault(clock: SimClock) -> HoldVaultModel:
    model = HoldVaultModel(make_config(), vault_id=VAULT, clock=clock)
    for account in (ALICE, BOB, GUARD):
        model.deposit_near_for(ALICE, account, FEE + 20 * YOCTO)
        model.ft_on_transfer(TOKEN, "treasury.testnet", 1_000 * HUSD, json.dumps({"credit_to": account}))
    model.world.create(SHOP, near=YOCTO, created_ms=clock())
    model.take_events()
    return model


def code_of(call) -> str:  # type: ignore[no-untyped-def]
    with pytest.raises(VaultError) as info:
        call()
    return info.value.code


def snapshot(model: HoldVaultModel) -> tuple:
    return copy.deepcopy(
        (model.users, model.holds, model.stats, model.near_liabilities, model.settlement_reserve, model.next_hold_id)
    )


def balance(model: HoldVaultModel, account: str, asset: str = NEAR) -> int:
    view = model.get_balance(account)
    return int(view["near"]) if asset == NEAR else int(view["tokens"][asset])


def held(model: HoldVaultModel, sender: str = ALICE, payee: str = SHOP, amount: int = YOCTO) -> Hold:
    return Hold.from_view(model.send(sender, payee, NEAR, amount)["hold"])


# ----------------------------------------------------------------------
# configuration and deposits
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"oracles": []},
        {"oracles": ["a.testnet", "b.testnet", "c.testnet", "d.testnet"]},
        {"oracles": [ORACLE, ORACLE]},
        {"oracles": [VAULT]},
        {"tokens": [NEAR]},
        {"tokens": ["t1.testnet", "t2.testnet", "t3.testnet", "t4.testnet", "t5.testnet"]},
        {"new_payee_hold_ms": 0},
        {"max_hold_ms": HOLD_MS - 1},
        {"trust_delay_ms": 366 * 24 * HOUR},
        {"max_active_holds": 0},
        {"max_active_holds": 101},
        {"instant_caps": {"usdc.testnet": 1}},
    ],
)
def test_invalid_configs_are_rejected(clock: SimClock, overrides: dict[str, object]) -> None:
    assert code_of(lambda: HoldVaultModel(make_config(**overrides), vault_id=VAULT, clock=clock)) == E.E_BAD_CONFIG


def test_registration_keeps_the_fee_and_credits_the_rest(clock: SimClock) -> None:
    model = HoldVaultModel(make_config(), vault_id=VAULT, clock=clock)
    assert code_of(lambda: model.deposit_near_for(ALICE, BOB, FEE - 1)) == E.E_FEE_TOO_LOW
    assert not model.is_registered(BOB)

    assert model.deposit_near_for(ALICE, BOB, FEE + 5 * YOCTO) == str(5 * YOCTO)
    names = [e["event"] for e in model.take_events()]
    assert names == ["user_registered", "deposit"]
    assert model.deposit_near_for(ALICE, BOB, YOCTO) == str(6 * YOCTO)
    deposit = model.take_events()[0]["data"][0]
    assert deposit == {"account_id": BOB, "asset": NEAR, "amount": str(YOCTO), "balance": str(6 * YOCTO)}
    assert model.near_liabilities == 6 * YOCTO

    model.deposit_near_for(ALICE, GUARD, FEE)
    assert [e["event"] for e in model.take_events()] == ["user_registered"]
    assert balance(model, GUARD) == 0


def test_token_deposits_need_an_allowed_token_and_a_registered_target(vault: HoldVaultModel) -> None:
    assert code_of(lambda: vault.ft_on_transfer("fake.testnet", ALICE, 5, "")) == E.E_NOT_TOKEN
    assert vault.ft_on_transfer(TOKEN, "stranger.testnet", 7 * HUSD, "") == str(7 * HUSD)
    assert vault.ft_on_transfer(TOKEN, "x.testnet", 7 * HUSD, '{"credit_to":"nobody.testnet"}') == str(7 * HUSD)
    assert vault.take_events() == []

    assert vault.ft_on_transfer(TOKEN, ALICE, 5 * HUSD, "") == "0"
    assert balance(vault, ALICE, TOKEN) == 1_005 * HUSD
    for bad in ("not json", '{"credit_to": 5}', '{"credit_to":"bob.testnet","x":1}', '{"credit_to":"Bad!"}'):
        assert code_of(lambda bad=bad: vault.ft_on_transfer(TOKEN, ALICE, 1, bad)) == E.E_BAD_MSG


# ----------------------------------------------------------------------
# sending
# ----------------------------------------------------------------------


def test_send_validates_every_input_and_changes_nothing_on_failure(vault: HoldVaultModel) -> None:
    before = snapshot(vault)
    assert code_of(lambda: vault.send("mallory.testnet", SHOP, NEAR, 1)) == E.E_NOT_REGISTERED
    assert code_of(lambda: vault.send(ALICE, SHOP, NEAR, 0)) == E.E_ZERO_AMOUNT
    assert code_of(lambda: vault.send(ALICE, SHOP, "usdc.testnet", 1)) == E.E_UNKNOWN_ASSET
    assert code_of(lambda: vault.send(ALICE, SHOP, "NEAR", 1)) == E.E_UNKNOWN_ASSET
    assert code_of(lambda: vault.send(ALICE, ALICE, NEAR, 1)) == E.E_SELF_PAYMENT
    assert code_of(lambda: vault.send(ALICE, VAULT, NEAR, 1)) == E.E_SELF_PAYMENT
    assert code_of(lambda: vault.send(ALICE, SHOP, NEAR, 20 * YOCTO + 1)) == E.E_INSUFFICIENT_BALANCE
    assert code_of(lambda: vault.send(ALICE, SHOP, NEAR, 1, "x" * 65)) == E.E_MEMO_TOO_LONG
    assert code_of(lambda: vault.send(ALICE, "Not An Account", NEAR, 1)) == E.E_BAD_ARGS
    assert snapshot(vault) == before
    assert vault.take_events() == []


def test_a_payment_to_a_new_payee_is_held(vault: HoldVaultModel, clock: SimClock) -> None:
    view = vault.send(ALICE, SHOP, TOKEN, 50 * HUSD, "ab" * 32)
    hold = Hold.from_view(view["hold"])
    assert hold.id == 0
    assert (hold.status, hold.instant, hold.flagged) == (HoldStatus.HELD, False, False)
    assert hold.release_at_ms == clock() + HOLD_MS
    assert hold.memo_hash == "ab" * 32
    assert balance(vault, ALICE, TOKEN) == 950 * HUSD
    assert vault.get_payee_stats(SHOP)["holds"] == 1
    created = first_item(vault.take_events(), "hold_created")
    assert created == {
        "hold_id": "0", "sender": ALICE, "payee": SHOP, "asset": TOKEN, "amount": str(50 * HUSD),
        "created_ms": str(clock()), "release_at_ms": str(clock() + HOLD_MS), "instant": False,
        "memo_hash": "ab" * 32,
    }


def test_senders_have_a_limit_on_held_payments(vault: HoldVaultModel) -> None:
    holds = [held(vault) for _ in range(3)]
    assert code_of(lambda: held(vault)) == E.E_TOO_MANY_HOLDS
    vault.cancel(ALICE, holds[1].id)
    held(vault)
    assert vault.get_user(ALICE)["active_holds"] == 3


# ----------------------------------------------------------------------
# cancelling
# ----------------------------------------------------------------------


def test_only_the_sender_cancels_and_only_once(vault: HoldVaultModel, clock: SimClock) -> None:
    hold = held(vault, amount=2 * YOCTO)
    assert code_of(lambda: vault.cancel(BOB, hold.id)) == E.E_NOT_SENDER
    assert code_of(lambda: vault.cancel(ALICE, 99)) == E.E_HOLD_NOT_FOUND
    view = vault.cancel(ALICE, hold.id)
    assert (view["status"], view["cancelled_by"], view["settled_ms"]) == ("cancelled", ALICE, str(clock()))
    assert balance(vault, ALICE) == 20 * YOCTO
    assert code_of(lambda: vault.cancel(ALICE, hold.id)) == E.E_HOLD_NOT_ACTIVE
    stats = vault.get_payee_stats(SHOP)
    assert (stats["cancelled"], stats["cancelled_after_flag"]) == (1, 0)
    event = first_item(vault.take_events(), "hold_cancelled")
    assert event is not None and (event["by"], event["flagged"]) == ("sender", False)


def test_cancelling_after_a_flag_counts_as_a_report(vault: HoldVaultModel, clock: SimClock) -> None:
    hold = held(vault)
    vault.extend_hold(ORACLE, hold.id, hold.release_at_ms + HOUR, 1, "{}")
    vault.cancel(ALICE, hold.id)
    stats = vault.get_payee_stats(SHOP)
    assert (stats["flagged"], stats["cancelled_after_flag"], stats["last_flag_ms"]) == (1, 1, str(clock()))


def test_only_the_effective_guardian_can_cancel_for_the_sender(vault: HoldVaultModel) -> None:
    hold = held(vault)
    assert code_of(lambda: vault.guardian_cancel(GUARD, hold.id)) == E.E_NOT_GUARDIAN
    vault.set_guardian(ALICE, GUARD)
    assert code_of(lambda: vault.guardian_cancel(BOB, hold.id)) == E.E_NOT_GUARDIAN
    view = vault.guardian_cancel(GUARD, hold.id)
    assert (view["status"], view["cancelled_by"]) == ("cancelled", GUARD)
    assert first_item(vault.take_events(), "hold_cancelled")["by"] == "guardian"  # type: ignore[index]
    assert code_of(lambda: vault.guardian_cancel(GUARD, hold.id)) == E.E_HOLD_NOT_ACTIVE


# ----------------------------------------------------------------------
# extending
# ----------------------------------------------------------------------


def test_extend_hold_checks_in_contract_order(vault: HoldVaultModel) -> None:
    hold = held(vault)
    later = hold.release_at_ms + HOUR
    assert code_of(lambda: vault.extend_hold(ALICE, hold.id, later, 1, "{}")) == E.E_NOT_ORACLE
    assert code_of(lambda: vault.extend_hold(ORACLE, 99, later, 1, "{}")) == E.E_HOLD_NOT_FOUND
    assert code_of(lambda: vault.extend_hold(ORACLE, hold.id, hold.release_at_ms, 1, "{}")) == E.E_ONLY_LONGER
    assert code_of(lambda: vault.extend_hold(ORACLE, hold.id, hold.release_at_ms - 1, 1, "{}")) == E.E_ONLY_LONGER
    cap = hold.created_ms + MAX_HOLD
    assert code_of(lambda: vault.extend_hold(ORACLE, hold.id, cap + 1, 1, "{}")) == E.E_ABOVE_MAX_HOLD
    assert code_of(lambda: vault.extend_hold(ORACLE, hold.id, later, 1, "x" * 4097)) == E.E_EVIDENCE_TOO_LONG
    assert code_of(lambda: vault.extend_hold(ORACLE, hold.id, later, 1, "é" * 2049)) == E.E_EVIDENCE_TOO_LONG
    assert code_of(lambda: vault.extend_hold(ORACLE, hold.id, later, 0, "{}")) == E.E_NO_REASON
    assert code_of(lambda: vault.extend_hold(ORACLE, hold.id, later, 2**32, "{}")) == E.E_BAD_ARGS
    vault.extend_hold(ORACLE, hold.id, cap, 1, "x" * 4096)  # both limits are inclusive
    vault.cancel(ALICE, hold.id)
    assert code_of(lambda: vault.extend_hold(ORACLE, hold.id, cap, 1, "{}")) == E.E_HOLD_NOT_ACTIVE


def test_extend_hold_records_reasons_and_the_evidence_hash(vault: HoldVaultModel) -> None:
    hold = held(vault)
    evidence = '{"v":1,"reasons":["NEW_ACCOUNT"]}'
    first = vault.extend_hold(ORACLE, hold.id, hold.release_at_ms + HOUR, 0b01, evidence)
    second = vault.extend_hold(ORACLE, hold.id, hold.release_at_ms + 2 * HOUR, 0b10, "{}")
    assert first["evidence_hash"] == hashlib.sha256(evidence.encode()).hexdigest()
    assert (second["reason_bits"], second["extensions"], second["flagged"]) == (0b11, 2, True)
    assert second["evidence_hash"] == hashlib.sha256(b"{}").hexdigest()
    assert vault.get_payee_stats(SHOP)["flagged"] == 1  # counted once per hold
    events = [e["data"][0] for e in vault.take_events() if e["event"] == "hold_extended"]
    assert events[1]["previous_release_at_ms"] == str(hold.release_at_ms + HOUR)
    assert events[1]["reason_bits"] == 0b11


# ----------------------------------------------------------------------
# releasing and payouts
# ----------------------------------------------------------------------


def test_nobody_can_release_early_or_twice(vault: HoldVaultModel, clock: SimClock) -> None:
    hold = held(vault, payee=BOB)
    assert code_of(lambda: vault.release("anyone.testnet", hold.id)) == E.E_NOT_DUE
    vault.extend_hold(ORACLE, hold.id, hold.release_at_ms + HOUR, 1, "{}")
    clock.advance(HOLD_MS)
    assert code_of(lambda: vault.release("anyone.testnet", hold.id)) == E.E_NOT_DUE
    clock.advance(HOUR)
    view = vault.release("anyone.testnet", hold.id)
    assert view["status"] == "released"
    assert code_of(lambda: vault.release("anyone.testnet", hold.id)) == E.E_HOLD_NOT_ACTIVE


def test_payout_to_a_vault_user_is_internal(vault: HoldVaultModel, clock: SimClock) -> None:
    hold = held(vault, payee=BOB, amount=3 * YOCTO)
    clock.advance(HOLD_MS)
    vault.release("keeper.testnet", hold.id)
    assert balance(vault, BOB) == 23 * YOCTO
    released = first_item(vault.take_events(), "hold_released")
    assert released is not None and released["internal"] is True
    assert vault.get_payee_stats(BOB)["released"] == 1


def test_external_near_payout_leaves_the_vault(vault: HoldVaultModel, clock: SimClock) -> None:
    liabilities, account = vault.near_liabilities, vault.account_balance
    hold = held(vault, amount=2 * YOCTO)
    clock.advance(HOLD_MS)
    vault.release("keeper.testnet", hold.id)
    assert vault.world.near[SHOP] == 3 * YOCTO
    assert vault.near_liabilities == liabilities - 2 * YOCTO
    assert vault.account_balance == account - 2 * YOCTO
    assert first_item(vault.take_events(), "hold_released")["internal"] is False  # type: ignore[index]


def test_failed_payouts_refund_the_sender(vault: HoldVaultModel, clock: SimClock) -> None:
    liabilities = vault.near_liabilities
    near_hold = held(vault, payee="missing.testnet", amount=YOCTO)  # the account does not exist
    vault.world.rejecting.add(SHOP)  # and the token transfer to the shop fails
    token_hold = Hold.from_view(vault.send(ALICE, SHOP, TOKEN, 10 * HUSD)["hold"])
    clock.advance(HOLD_MS)
    assert vault.release("k.testnet", near_hold.id)["status"] == "failed"
    assert vault.release("k.testnet", token_hold.id)["status"] == "failed"
    assert balance(vault, ALICE) == 20 * YOCTO
    assert balance(vault, ALICE, TOKEN) == 1_000 * HUSD
    assert vault.near_liabilities == liabilities
    failed = [e["data"][0] for e in vault.take_events() if e["event"] == "hold_failed"]
    assert [f["refunded_to"] for f in failed] == [ALICE, ALICE]
    assert vault.get_payee_stats(SHOP)["released"] == 0


def test_token_payout_registers_the_payee_once(vault: HoldVaultModel, clock: SimClock) -> None:
    before = vault.account_balance
    for _ in range(2):
        hold = Hold.from_view(vault.send(ALICE, SHOP, TOKEN, 5 * HUSD)["hold"])
        clock.advance(HOLD_MS)
        vault.release("k.testnet", hold.id)
    assert vault.world.tokens[(TOKEN, SHOP)] == 10 * HUSD
    assert before - vault.account_balance == 1_250 * 10**18 + 2  # one storage deposit, two 1-yocto transfers


def test_releasing_holds_cannot_be_cancelled_or_released_again(vault: HoldVaultModel, clock: SimClock) -> None:
    vault.async_payouts = True
    hold = held(vault)
    clock.advance(HOLD_MS)
    assert vault.release("k.testnet", hold.id)["status"] == "releasing"
    assert code_of(lambda: vault.cancel(ALICE, hold.id)) == E.E_HOLD_NOT_ACTIVE
    assert code_of(lambda: vault.release("k.testnet", hold.id)) == E.E_HOLD_NOT_ACTIVE
    assert vault.get_due_holds() == []
    assert vault.settle_payouts() == 1
    assert vault.get_hold(hold.id)["status"] == "released"  # type: ignore[index]


# ----------------------------------------------------------------------
# trust and instant payments
# ----------------------------------------------------------------------


def test_trust_takes_effect_after_the_delay(vault: HoldVaultModel, clock: SimClock) -> None:
    trusted = vault.add_trusted(ALICE, SHOP)
    assert trusted == {"payee": SHOP, "effective_ms": str(clock() + 30_000)}
    assert held(vault).instant is False
    clock.advance(30_000)
    hold = held(vault)
    assert hold.instant and hold.release_at_ms == hold.created_ms
    assert vault.get_hold(hold.id)["status"] == "released"  # type: ignore[index]
    stats = vault.get_payee_stats(SHOP)
    assert (stats["holds"], stats["released"]) == (1, 0)  # instant payments are not counted


def test_external_instant_payout_reports_releasing_in_the_send_view(vault: HoldVaultModel, clock: SimClock) -> None:
    vault.add_trusted(ALICE, SHOP)
    clock.advance(30_000)
    view = vault.send(ALICE, SHOP, NEAR, YOCTO)["hold"]
    assert view["status"] == "releasing"  # the transfer settles in a later receipt
    assert vault.get_hold(int(view["id"]))["status"] == "released"  # type: ignore[index]


def test_daily_instant_cap_and_rollover(vault: HoldVaultModel, clock: SimClock) -> None:
    vault.add_trusted(ALICE, SHOP)
    clock.advance(30_000)
    send = lambda units: Hold.from_view(vault.send(ALICE, SHOP, TOKEN, units * HUSD)["hold"])  # noqa: E731
    assert send(60).instant
    assert not send(50).instant  # 110 would pass the 100 HUSD cap
    assert send(40).instant
    assert not send(1).instant
    clock.advance(24 * HOUR)
    assert send(100).instant


def test_re_adding_trust_keeps_the_entry(vault: HoldVaultModel, clock: SimClock) -> None:
    first = vault.add_trusted(ALICE, SHOP)
    clock.advance(10_000)
    assert vault.add_trusted(ALICE, SHOP) == first
    assert code_of(lambda: vault.add_trusted(ALICE, ALICE)) == E.E_SELF_PAYMENT
    assert code_of(lambda: vault.add_trusted(ALICE, VAULT)) == E.E_SELF_PAYMENT
    assert vault.remove_trusted(ALICE, SHOP) is True
    assert vault.remove_trusted(ALICE, SHOP) is False
    assert [e["event"] for e in vault.take_events()] == ["trust_added", "trust_added", "trust_removed"]


def test_at_most_fifty_trusted_payees(vault: HoldVaultModel) -> None:
    for n in range(50):
        vault.add_trusted(ALICE, f"payee{n}.testnet")
    assert code_of(lambda: vault.add_trusted(ALICE, "one-more.testnet")) == E.E_TOO_MANY_TRUSTED
    vault.add_trusted(ALICE, "payee7.testnet")  # re-adding an existing one is fine


def test_removing_trust_is_immediate(vault: HoldVaultModel, clock: SimClock) -> None:
    vault.add_trusted(ALICE, SHOP)
    clock.advance(30_000)
    vault.remove_trusted(ALICE, SHOP)
    assert held(vault).instant is False


# ----------------------------------------------------------------------
# guardians
# ----------------------------------------------------------------------


def test_guardian_changes_wait_while_a_guardian_is_in_charge(vault: HoldVaultModel, clock: SimClock) -> None:
    assert vault.set_guardian(ALICE, GUARD)["guardian"] == GUARD  # the first one is immediate
    view = vault.set_guardian(ALICE, BOB)
    assert view["guardian"] == GUARD
    assert view["pending_guardian"] == {"guardian": BOB, "effective_ms": str(clock() + 30_000)}
    hold = held(vault)
    assert code_of(lambda: vault.guardian_cancel(BOB, hold.id)) == E.E_NOT_GUARDIAN
    clock.advance(30_000)
    assert vault.get_user(ALICE)["guardian"] == BOB  # type: ignore[index]
    assert code_of(lambda: vault.guardian_cancel(GUARD, hold.id)) == E.E_NOT_GUARDIAN
    vault.guardian_cancel(BOB, hold.id)


def test_a_newer_guardian_change_replaces_the_pending_one(vault: HoldVaultModel, clock: SimClock) -> None:
    vault.set_guardian(ALICE, GUARD)
    vault.set_guardian(ALICE, BOB)
    clock.advance(20_000)
    view = vault.set_guardian(ALICE, None)
    assert view["pending_guardian"] == {"guardian": None, "effective_ms": str(clock() + 30_000)}
    clock.advance(30_000)
    assert vault.get_user(ALICE)["guardian"] is None  # type: ignore[index]
    assert code_of(lambda: vault.set_guardian(ALICE, ALICE)) == E.E_BAD_GUARDIAN
    assert code_of(lambda: vault.set_guardian(ALICE, VAULT)) == E.E_BAD_GUARDIAN
    assert code_of(lambda: vault.set_guardian("mallory.testnet", GUARD)) == E.E_NOT_REGISTERED


# ----------------------------------------------------------------------
# views
# ----------------------------------------------------------------------


def test_history_is_newest_first_and_bounded(vault: HoldVaultModel, clock: SimClock) -> None:
    ids = []
    for _ in range(5):
        hold = held(vault, amount=1)
        vault.cancel(ALICE, hold.id)
        ids.append(hold.id)
    assert [int(v["id"]) for v in vault.get_holds_for(ALICE)] == ids[::-1]
    assert len(vault.get_holds_for(ALICE, 2)) == 2
    assert vault.get_holds_for("unknown.testnet") == []


def test_due_holds_are_earliest_first(vault: HoldVaultModel, clock: SimClock) -> None:
    first = held(vault, amount=1)
    clock.advance(1_000)
    second = held(vault, amount=1)
    vault.extend_hold(ORACLE, first.id, first.release_at_ms + HOUR, 1, "{}")
    assert [int(v["id"]) for v in vault.get_active_holds(ALICE)] == [first.id, second.id]
    clock.advance(HOLD_MS)
    assert [int(v["id"]) for v in vault.get_due_holds()] == [second.id]
    clock.advance(HOUR)
    assert [int(v["id"]) for v in vault.get_due_holds()] == [second.id, first.id]
    assert len(vault.get_due_holds(1)) == 1


def test_views_for_unknown_accounts(vault: HoldVaultModel) -> None:
    assert vault.get_balance("x.testnet") == {"near": "0", "tokens": {TOKEN: "0"}}
    assert vault.get_user("x.testnet") is None
    assert vault.is_registered("x.testnet") is False
    assert vault.get_payee_stats("x.testnet")["last_flag_ms"] is None
    config = vault.get_config()
    assert config["version"] == "1.0.0" and config["max_active_holds"] == 3


# ----------------------------------------------------------------------
# solvency
# ----------------------------------------------------------------------


def test_registration_fails_when_the_reserve_cannot_cover_storage(clock: SimClock) -> None:
    model = HoldVaultModel(make_config(), vault_id=VAULT, clock=clock, reserve=YOCTO)
    assert code_of(lambda: model.deposit_near_for(ALICE, BOB, FEE)) == E.E_INSOLVENT
    assert not model.is_registered(BOB)


def test_the_last_user_can_take_everything_out(vault: HoldVaultModel, clock: SimClock) -> None:
    for account in (ALICE, BOB, GUARD):
        hold = held(vault, sender=account, amount=20 * YOCTO)
        clock.advance(HOLD_MS)
        vault.release("k.testnet", hold.id)
    solvency = {k: int(v) for k, v in vault.get_solvency().items()}
    assert solvency["near_liabilities"] == 0
    assert solvency["surplus"] > 0
    assert solvency["account_balance"] >= solvency["storage_cost"] + solvency["settlement_reserve"]


# ----------------------------------------------------------------------
# property: release times only grow, and only through extend_hold
# ----------------------------------------------------------------------

operations = st.lists(
    st.tuples(
        st.sampled_from(["send", "extend", "cancel", "release", "wait", "trust"]),
        st.integers(min_value=0, max_value=6),
        st.integers(min_value=1, max_value=5),
    ),
    max_size=40,
)


@settings(max_examples=60, deadline=None)
@given(operations)
def test_release_time_only_grows_and_money_is_conserved(ops: list[tuple[str, int, int]]) -> None:
    clock = SimClock()
    vault = HoldVaultModel(make_config(), vault_id=VAULT, clock=clock)
    for account in (ALICE, BOB):
        vault.deposit_near_for(ALICE, account, FEE + 20 * YOCTO)
    vault.world.create(SHOP, near=0, created_ms=clock())
    deposited = vault.near_liabilities
    seen: dict[int, int] = {}
    for op, index, size in ops:
        sender = (ALICE, BOB)[index % 2]
        ids = sorted(vault.holds)
        target = ids[index % len(ids)] if ids else 0
        before = {h.id: h.release_at_ms for h in vault.holds.values()}
        try:
            if op == "send":
                vault.send(sender, (SHOP, BOB if sender == ALICE else ALICE)[size % 2], NEAR, size * YOCTO // 10)
            elif op == "extend":
                hold = vault.holds.get(target)
                if hold is not None:
                    vault.extend_hold(ORACLE, target, hold.release_at_ms + size * 1_000, 1 << index, "{}")
            elif op == "cancel":
                vault.cancel(sender, target)
            elif op == "release":
                vault.release(ALICE, target)
            elif op == "wait":
                clock.advance(size * 7_000)
            else:
                vault.add_trusted(sender, SHOP)
        except VaultError:
            pass
        for hold in vault.holds.values():
            if op != "extend":
                assert hold.release_at_ms == before.get(hold.id, hold.release_at_ms)
            assert hold.release_at_ms >= seen.get(hold.id, 0)
            assert hold.release_at_ms <= hold.created_ms + MAX_HOLD
            seen[hold.id] = hold.release_at_ms
        in_holds = sum(h.amount for h in vault.holds.values() if h.status in (HoldStatus.HELD, HoldStatus.RELEASING))
        balances = sum(u.balances[NEAR] for u in vault.users.values())
        assert vault.near_liabilities == balances + in_holds
        assert balances + in_holds + vault.world.near[SHOP] == deposited


# ----------------------------------------------------------------------
# the async VaultPort adapter
# ----------------------------------------------------------------------


async def test_adapter_returns_transactions_with_parsed_events() -> None:
    clock = SimClock()
    fake = FakeVault(make_config(), vault_id=VAULT, clock=clock)
    user = derive_user("secret", "tg:1", "holdup.testnet")
    txs = await fake.onboard_user(user, {NEAR: 3 * YOCTO, TOKEN: 50 * HUSD})
    assert [tx.events[0]["event"] if tx.events else None for tx in txs] == [None, "user_registered", "deposit"]
    assert await fake.get_balance(user.account_id) == {NEAR: 3 * YOCTO, TOKEN: 50 * HUSD}
    assert await fake.onboard_user(user, {NEAR: YOCTO}) == []  # already done

    fake.create_account(SHOP)
    sent = await fake.send(user, SHOP, TOKEN, 5 * HUSD)
    assert sent.hold.status == HoldStatus.HELD and sent.hold.id == 0
    assert events_from_logs(sent.tx.logs) == sent.tx.events
    assert len(sent.tx.tx_hash) in (43, 44)
    assert fake.transactions[-1].method == "send"

    with pytest.raises(VaultError) as info:
        await fake.extend_hold(sent.hold.id, sent.hold.release_at_ms, 1, "{}")
    assert info.value.code == E.E_ONLY_LONGER
    assert info.value.tx_hash and not fake.transactions[-1].ok


async def test_adapter_keys_match_the_deployment_policy() -> None:
    fake = FakeVault(make_config(), vault_id=VAULT, clock=SimClock())
    user = derive_user("secret", "tg:1", "holdup.testnet")
    await fake.onboard_user(user, {})
    assert fake.view_access_key_list(VAULT)["keys"] == []
    (user_key,) = fake.view_access_key_list(user.account_id)["keys"]
    permission = user_key["access_key"]["permission"]["FunctionCall"]
    assert permission == {"allowance": None, "receiver_id": VAULT, "method_names": list(USER_METHODS)}
    assert fake.check_transaction(ORACLE, VAULT, [{"FunctionCall": {"method_name": "extend_hold", "deposit": "0"}}]) is None
    denied = fake.check_transaction(ORACLE, VAULT, [{"FunctionCall": {"method_name": "send", "deposit": "0"}}])
    assert denied is not None and denied["cause"]["name"] == "INVALID_TRANSACTION"
