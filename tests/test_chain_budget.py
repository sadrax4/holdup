"""The NEAR budget: what each deployed account gets, what a new user costs, and that it all fits the faucet."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from near.actions import function_call, transfer
from near.units import Amount, Gas

from holdup.chain import budget as B
from holdup.chain.deploy import DeployKeys, deploy_settings, plan_deploy
from holdup.chain.errors import VaultError
from holdup.chain.fake import HoldVaultModel, SimClock
from holdup.chain.keys import KEEPER_METHODS, ORACLE_METHODS, USER_METHODS
from holdup.config import DEMO_NEAR_YOCTO, DEMO_USER_GAS_NEAR, USDC_TESTNET, Settings
from holdup.models import NEAR as NEAR_ASSET

RES = Path(__file__).resolve().parents[1] / "res"
NEAR = 10**24
MILLI = 10**21
VAULT_WASM = (RES / "holdvault.wasm").read_bytes()
TOKEN_WASM = (RES / "demo_token.wasm").read_bytes()


def hosted_plan(**overrides: Any) -> Any:
    base = Settings(network="testnet", usdc_token=USDC_TESTNET)
    settings = deploy_settings(base, "testnet", "holdup.testnet")
    return plan_deploy(settings, profile="hosted", vault_wasm=VAULT_WASM, token_wasm=TOKEN_WASM,
                       keys=DeployKeys(), **overrides)


def tx(plan: Any, label: str) -> Any:
    (found,) = [t for t in plan.txs if t.label == label]
    return found


def transferred(planned: Any) -> int:
    return sum(a.deposit for a in planned.actions if type(a).__name__ == "Transfer")


def test_measured_testnet_values() -> None:
    # EXPERIMENTAL_protocol_config and gas_price on testnet, protocol 88, 2026-10-31.
    assert B.STORAGE_BYTE_COST == 10**19  # 1 NEAR per 100 kB
    assert (B.ACCOUNT_RECORD_BYTES, B.RECORD_OVERHEAD_BYTES, B.ZERO_BALANCE_ACCOUNT_BYTES) == (100, 40, 770)
    assert (B.GAS_PRICE, B.GAS_PURCHASE_PRICE) == (10**8, 10**9)
    assert B.ACCOUNT_CREATION_CHARGE == 7 * MILLI
    # The wasm files the deploy budget is computed from.
    assert (len(VAULT_WASM), len(TOKEN_WASM)) == (325_126, 153_866)


def test_attached_gas_is_bought_up_front_at_ten_times_the_price_it_burns_at() -> None:
    cost = B.tx_cost([function_call("x", b"", gas=Gas.tgas(100))])
    send = 108_059_500_000 + 200_000_000_000 + 47_683_715 * 1
    execution = 108_059_500_000 + 780_000_000_000 + 2_235_934 * 1
    assert cost.upfront == send * 10**8 + (execution + 100 * 10**12) * 10**9  # about 0.1009 NEAR at hand
    assert cost.burnt == (send + execution + 100 * 10**12) * 10**8  # at most 0.0101 NEAR spent
    assert cost.deposits == 0 and cost.at_hand == cost.upfront
    moved = B.tx_cost([transfer(Amount.near("1"))])
    assert moved.deposits == NEAR and moved.spent == NEAR + moved.burnt


def test_small_accounts_need_no_storage_balance() -> None:
    assert B.storage_stake(770) == 0 and B.storage_stake(771) == 771 * 10**19
    assert B.available_balance(5 * NEAR, 0, 182) == 5 * NEAR
    assert B.available_balance(5 * NEAR, 0, 100_000) == 4 * NEAR
    vault = "vault.holdup.testnet"
    for methods in (USER_METHODS, ORACLE_METHODS, KEEPER_METHODS):
        assert B.ACCOUNT_RECORD_BYTES + B.function_call_key_bytes(vault, methods) <= 770
    # A user account: 100 B record + one key with seven method names = 321 B.
    assert B.ACCOUNT_RECORD_BYTES + B.function_call_key_bytes(vault, USER_METHODS) == 321


def test_the_contract_accounts_get_storage_their_own_gas_and_a_margin() -> None:
    plan = hosted_plan()
    for name, code, margin in (("vault", VAULT_WASM, B.VAULT_MARGIN), ("token", TOKEN_WASM, B.TOKEN_MARGIN)):
        sent = transferred(tx(plan, f"create {name}"))
        storage = (100 + len(code) + 1_000 + 82) * 10**19
        init = B.tx_cost(tx(plan, f"deploy and initialise {name}").actions, sir=True)
        delete = B.tx_cost(tx(plan, f"delete the {name}'s last key").actions, sir=True)
        assert sent == storage + init.upfront + delete.upfront + margin
        # Deploying buys the gas first, so the code's storage must still be covered at that moment...
        assert sent - init.upfront >= storage
        # ...and once the deploy and the key deletion have burnt their gas, at least the margin is left.
        assert sent - init.burnt - delete.burnt - storage >= margin
    assert 3.5 * NEAR < transferred(tx(plan, "create vault")) < 3.7 * NEAR  # 3.2631 storage + 0.0726 gas + 0.25
    assert 1.6 * NEAR < transferred(tx(plan, "create token")) < 1.7 * NEAR  # 1.5505 storage + 0.0614 gas + 0.05
    assert transferred(tx(hosted_plan(reserve_near="1"), "create vault")) == (
        transferred(tx(plan, "create vault")) - B.VAULT_MARGIN + NEAR
    )


def test_the_oracle_and_keeper_get_gas_for_two_thousand_calls() -> None:
    plan = hosted_plan()
    oracle = transferred(tx(plan, "create the oracle with one extend_hold key"))
    keeper = transferred(tx(plan, "create the keeper with one release key"))
    extend_upfront = B.tx_cost([function_call("extend_hold", B.largest_extension_args(), gas=Gas.tgas(30))]).upfront
    release_upfront = B.tx_cost([function_call("release", {"hold_id": "18446744073709551615"},
                                               gas=Gas.tgas(100))]).upfront
    # 5 Tgas burnt per extension and 15 Tgas per release, at 1e8 yoctoNEAR per gas.
    assert (oracle - 3 * extend_upfront) // (5 * 10**12 * 10**8) == 2_000
    assert (keeper - release_upfront) // (15 * 10**12 * 10**8) == 2_000
    assert oracle == NEAR + 3 * extend_upfront and keeper == 3 * NEAR + release_upfront


def test_payees_get_no_balance_and_the_budget_adds_up() -> None:
    plan = hosted_plan()
    budget = B.deploy_budget(plan)
    assert budget.total == sum(line.yocto for line in budget.lines)
    by_label = {line.label: line.yocto for line in budget.lines}
    assert by_label["create 14 demo payees"] == 0  # 4 the samples name, 10 practice friends
    root_txs = [t for t in plan.txs if t.signer == "root"]
    moved = sum(B.tx_cost(t.actions).deposits for t in root_txs)
    burnt = sum(B.tx_cost(t.actions).burnt for t in root_txs)
    assert by_label["gas the root pays"] == burnt
    assert by_label["treasury float"] == B.TREASURY_FLOAT == 3 * NEAR // 2
    assert budget.total == moved + burnt + by_label["gas bought up front, returned"] + B.TREASURY_FLOAT
    # 18 account creations (token, vault, oracle, keeper, 14 payees) at 0.007 NEAR each are in the root's gas.
    assert burnt > 18 * B.ACCOUNT_CREATION_CHARGE
    assert 10.5 * NEAR < budget.total < 11.5 * NEAR
    text = budget.describe()
    assert "total" in text and "2,000 release calls" in text and "NEP-448" in text


def test_the_hosted_budget_fits_what_bootstrap_and_four_faucet_drops_give() -> None:
    budget = B.deploy_budget(hosted_plan())
    bootstrap = Amount.near("4.8")  # the registrar call funds the new root with 4.8 NEAR
    assert budget.total <= bootstrap + 4 * 5 * NEAR
    assert budget.total - bootstrap <= 2 * 5 * NEAR  # in practice two drops


def test_a_new_user_costs_the_treasury_about_a_quarter_of_a_near() -> None:
    cost = B.onboarding_cost(vault_id="vault.holdup.testnet", user_gas=B.near_to_yocto(DEMO_USER_GAS_NEAR),
                             registration_fee=10 * MILLI, demo_near=DEMO_NEAR_YOCTO, tokens=1)
    create, deposit, token = cost.steps
    assert create.deposits == 30 * MILLI and deposit.deposits == 210 * MILLI and token.deposits == 1
    assert create.burnt >= B.ACCOUNT_CREATION_CHARGE
    assert 0.26 * NEAR < cost.spent < 0.262 * NEAR  # 0.24 NEAR handed out, about 0.021 NEAR of gas at most
    # The 100 Tgas of ft_transfer_call are bought up front and mostly come back.
    assert cost.at_hand == cost.spent + token.upfront - token.burnt
    assert 0.35 * NEAR < cost.at_hand < 0.36 * NEAR
    # 1.5 NEAR (the faucet threshold) pays for five users; one faucet drop of 5 NEAR for nineteen.
    assert B.TREASURY_FLOAT // cost.spent == 5 and (5 * NEAR) // cost.spent == 19


def test_demo_money_on_a_real_network() -> None:
    assert DEMO_NEAR_YOCTO == 200 * MILLI and DEMO_USER_GAS_NEAR == "0.03"
    hold_fee = 15 * MILLI  # HOLD_STORAGE_FEE in contracts/holdvault/src/lib.rs
    assert DEMO_NEAR_YOCTO // hold_fee >= 13  # at least ten holds, with room for small NEAR payments
    user_gas = B.near_to_yocto(DEMO_USER_GAS_NEAR)
    for method in USER_METHODS:
        upfront = B.call_upfront(method, {"payee": "u" * 64, "asset": "near", "amount": "1", "memo_hash": "f" * 64})
        # The user's 0.03 NEAR buys any user call's gas with room for many calls' burnt gas.
        assert user_gas - upfront > 9 * MILLI, method
    payout = B.call_upfront("send", {}, B.PAYOUT_SEND_GAS["token"])
    # A send that may pay out a token at once needs more than the user holds: the treasury tops up.
    assert user_gas < payout < user_gas + B.USER_TOP_UP_LIMIT


def test_near_text_rounds_up() -> None:
    assert B.near_text(1) == "0.0001" and B.near_text(NEAR) == "1.0000" and B.near_text(15 * MILLI, 3) == "0.015"


def test_the_vault_funding_passes_assert_solvent_from_the_first_send() -> None:
    plan = hosted_plan()
    sent = transferred(tx(plan, "create vault"))
    burnt = sum(B.tx_cost(tx(plan, label).actions, sir=True).burnt
                for label in ("deploy and initialise vault", "delete the vault's last key"))
    after_deploy = sent - burnt  # what the vault holds once its own deploy gas is paid

    def vault(balance: int) -> HoldVaultModel:
        # The model adds 1,000 bytes of state to the code it is given, as VAULT_STATE_BYTES does.
        return HoldVaultModel(plan.config, vault_id=plan.vault_id, clock=SimClock(), reserve=balance,
                              code_bytes=B.ACCOUNT_RECORD_BYTES + len(VAULT_WASM))

    model = vault(after_deploy)
    user = f"u1.{plan.root}"
    fee = plan.config.registration_fee
    model.deposit_near_for(plan.root, user, fee + DEMO_NEAR_YOCTO)
    model.send(user, f"ravi.{plan.root}", NEAR_ASSET, 50 * MILLI)  # the first send: assert_solvent passes
    surplus = int(model.get_solvency()["surplus"])
    assert surplus >= B.VAULT_MARGIN  # the margin is untouched: every fee pays for its own storage
    # Without the margin and the returned deploy gas, a vault 0.01 NEAR short of its storage refuses users.
    storage = (B.ACCOUNT_RECORD_BYTES + len(VAULT_WASM) + B.VAULT_STATE_BYTES) * B.STORAGE_BYTE_COST
    with pytest.raises(VaultError, match="E_INSOLVENT"):
        vault(storage - 10 * MILLI).deposit_near_for(plan.root, user, fee + DEMO_NEAR_YOCTO)
