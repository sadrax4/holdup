"""Forbidden actions are recognised from nearcore's recorded error JSON."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from near import AccountNotFoundError
from near.errors import ContractPanicError, RpcError
from near.units import Gas
from near.wire import Action

from holdup.chain.budget import GAS, call_upfront, near_to_yocto
from holdup.chain.errors import error_path, rejection_name
from holdup.chain.fake import SimClock
from holdup.chain.keys import USER_METHODS, derive_user
from holdup.chain.prove import attempt_cost, plan_attempts, prove, prove_fake, to_near_actions
from holdup.config import DEMO_USER_GAS_NEAR, offline_settings
from holdup.runtime import offline_vault


def rpc_error(invalid_tx: Any) -> dict[str, Any]:
    """The JSON-RPC error object nearcore 2.x returns for a rejected transaction."""
    return {
        "name": "HANDLER_ERROR",
        "cause": {"name": "INVALID_TRANSACTION", "info": {}},
        "code": -32000,
        "message": "Server error",
        "data": {"TxExecutionError": {"InvalidTxError": invalid_tx}},
    }


RECORDED = {
    "MethodNameMismatch": rpc_error({"InvalidAccessKeyError": {"MethodNameMismatch": {"method_name": "send"}}}),
    "RequiresFullAccess": rpc_error({"InvalidAccessKeyError": "RequiresFullAccess"}),
    "ReceiverMismatch": rpc_error(
        {"InvalidAccessKeyError": {"ReceiverMismatch": {"tx_receiver": "husd.testnet", "ak_receiver": "vault.testnet"}}}
    ),
    "DepositWithFunctionCall": rpc_error({"InvalidAccessKeyError": "DepositWithFunctionCall"}),
}


def test_rejection_names_come_from_the_nested_error() -> None:
    for name, error in RECORDED.items():
        assert rejection_name(error) == name
        assert rejection_name(error["data"]) == name
    assert error_path(RECORDED["MethodNameMismatch"]) == [
        "TxExecutionError", "InvalidTxError", "InvalidAccessKeyError", "MethodNameMismatch",
    ]


def test_older_and_unknown_error_shapes() -> None:
    legacy = {"name": "HANDLER_ERROR", "cause": {"name": "INVALID_TRANSACTION", "info": {}},
              "data": "InvalidTxError(InvalidAccessKeyError(DepositWithFunctionCall))"}
    assert rejection_name(legacy) == "DepositWithFunctionCall"
    assert rejection_name({"something": "else"}) == "unknown"
    assert rejection_name(None) == "unknown"


def test_attempts_are_harmless_even_if_a_key_were_unrestricted() -> None:
    settings = offline_settings()
    user = derive_user(settings.secret, "demo:priya", settings.root_account)
    attempts = plan_attempts(settings, user)
    assert {a.actor for a in attempts} == {"oracle", "keeper", "user"}
    assert {a.actor for a in plan_attempts(settings)} == {"oracle", "keeper"}
    for attempt in attempts:
        for action in attempt.actions:
            kind, body = next(iter(action.items()))
            if kind == "Transfer":
                assert attempt.receiver_id == attempt.signer_id  # money would only move to itself
            if kind in ("Transfer", "FunctionCall"):
                assert int(body["deposit"]) <= 1


def test_attempts_turn_into_near_kit_actions() -> None:
    settings = offline_settings()
    oracle_send, transfer, _, deposit, add_key, *_ = plan_attempts(settings)
    (call,) = to_near_actions(oracle_send.actions)
    assert isinstance(call, Action.FunctionCall) and call.method_name == "send" and call.deposit == 0
    assert isinstance(to_near_actions(transfer.actions)[0], Action.Transfer)
    assert to_near_actions(deposit.actions)[0].deposit == 1
    assert isinstance(to_near_actions(add_key.actions)[0], Action.AddKey)


async def test_offline_proof_rejects_every_forbidden_action() -> None:
    settings = offline_settings()
    fake = offline_vault(settings, SimClock())
    user = derive_user(settings.secret, "demo:priya", settings.root_account)
    await fake.onboard_user(user, {})
    report = prove_fake(fake, settings, user=user)
    assert report.simulated and report.ok
    observed = {(r.actor, r.description): r.observed for r in report.results}
    assert observed[("oracle", "call send on the vault")] == "MethodNameMismatch"
    assert observed[("oracle", "transfer NEAR")] == "RequiresFullAccess"
    assert observed[("oracle", "call another contract")] == "ReceiverMismatch"
    assert observed[("oracle", "attach a deposit to extend_hold")] == "DepositWithFunctionCall"
    assert report.to_dict()["ok"] is True


class RecordedNode:
    """Stands in for AsyncNear: each send_transaction replays one recorded outcome."""

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.sent: list[tuple[str, str]] = []

    async def send_transaction(self, receiver_id: str, actions: list[Any], *, signer: Any) -> Any:
        self.sent.append((signer.account_id, receiver_id))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def test_live_proof_records_the_network_errors() -> None:
    settings = offline_settings()
    settings.oracle_private_key = derive_user("k", "oracle", "x.testnet").private_key
    settings.keeper_private_key = derive_user("k", "keeper", "x.testnet").private_key
    expected = [a.expected[0] for a in plan_attempts(settings)]
    outcomes: list[Any] = [RpcError("RPC error [INVALID_TRANSACTION]", data=RECORDED[name]) for name in expected[:-2]]
    outcomes.append(ContractPanicError("Smart contract panicked: E_NOT_REGISTERED: x"))  # a key that was too broad
    outcomes.append(object())  # accepted
    node = RecordedNode(outcomes)
    report = await prove(settings, near=node)  # type: ignore[arg-type]
    assert [r.passed for r in report.results] == [True] * 4 + [False, False]
    assert report.results[-2].observed == "executed"
    assert report.results[-1].observed == "accepted"
    assert not report.ok and not report.simulated
    assert node.sent[0] == (settings.oracle_accounts[0], settings.vault_account)


async def test_live_proof_skips_roles_without_keys() -> None:
    report = await prove(offline_settings(), near=RecordedNode([]))  # type: ignore[arg-type]
    assert {r.observed for r in report.results} == {"skipped"}
    assert not report.ok


class AccountsNode(RecordedNode):
    """A RecordedNode that also answers ``account``: only the accounts in ``existing`` exist."""

    def __init__(self, outcomes: list[Any], existing: set[str]) -> None:
        super().__init__(outcomes)
        self.existing = existing

    async def account(self, account_id: str) -> Any:
        if account_id not in self.existing:
            raise AccountNotFoundError(account_id)
        return object()


async def test_live_proof_leaves_out_a_user_nobody_has_onboarded_yet() -> None:
    settings = offline_settings()
    settings.oracle_private_key = derive_user("k", "oracle", "x.testnet").private_key
    settings.keeper_private_key = derive_user("k", "keeper", "x.testnet").private_key
    user = derive_user(settings.secret, "demo:priya", settings.root_account)
    roles = plan_attempts(settings)
    node = AccountsNode([RpcError("rejected", data=RECORDED[a.expected[0]]) for a in roles], existing=set())
    report = await prove(settings, near=node, user=user)  # type: ignore[arg-type]
    assert report.ok and {r.actor for r in report.results} == {"oracle", "keeper"}
    assert len(report.notes) == 1 and user.account_id in report.notes[0]
    assert report.to_dict()["notes"] == report.notes

    everyone = plan_attempts(settings, user)
    node = AccountsNode([RpcError("rejected", data=RECORDED[a.expected[0]]) for a in everyone], {user.account_id})
    report = await prove(settings, near=node, user=user)  # type: ignore[arg-type]
    assert report.ok and [r.actor for r in report.results].count("user") == 4 and report.notes == []


async def test_every_attempt_reaches_the_key_check_of_a_user_with_little_gas_left() -> None:
    """On testnet a user held 0.0286 NEAR, and 30 Tgas attempts got NotEnoughBalance instead.

    nearcore charges the gas a transaction buys up front before it looks at the key's
    permission, so an attempt the signer cannot pay for proves nothing.
    """
    settings = offline_settings()
    fake = offline_vault(settings, SimClock())
    user = derive_user(settings.secret, "demo:priya", settings.root_account)
    await fake.onboard_user(user, {})
    fake.model.world.near[user.account_id] = near_to_yocto("0.0286")
    report = prove_fake(fake, settings, user=user)
    assert report.ok, [(r.description, r.observed) for r in report.results if not r.passed]
    assert [r.observed for r in report.results if r.actor == "user"] == [
        "MethodNameMismatch", "RequiresFullAccess", "DepositWithFunctionCall", "ReceiverMismatch"]

    # The attempts as they were: 30 Tgas cost about 0.031 NEAR up front, more than the user held.
    attempt = next(a for a in plan_attempts(settings, user) if a.actor == "user")
    (call,) = attempt.actions
    old = replace(attempt, actions=({"FunctionCall": {**call["FunctionCall"], "gas": str(int(Gas.tgas(30)))}},))
    error = fake.check_transaction(user.account_id, attempt.receiver_id, list(old.actions), cost=attempt_cost(old))
    assert rejection_name(error) == "NotEnoughBalance"


def test_attempts_cost_less_than_any_call_a_user_can_make() -> None:
    """A user who can still use HoldUp can always pay for the proof's attempts."""
    settings = offline_settings()
    user = derive_user(settings.secret, "demo:priya", settings.root_account)
    cheapest_call = min(call_upfront(method, {"hold_id": "1"}, GAS[method]) for method in USER_METHODS)
    assert max(attempt_cost(a) for a in plan_attempts(settings, user)) < cheapest_call / 5
    assert cheapest_call < near_to_yocto(DEMO_USER_GAS_NEAR)
