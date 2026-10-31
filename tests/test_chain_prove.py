"""Forbidden actions are recognised from nearcore's recorded error JSON."""

from __future__ import annotations

from typing import Any

from near.errors import ContractPanicError, RpcError
from near.wire import Action

from holdup.chain.errors import error_path, rejection_name
from holdup.chain.fake import SimClock
from holdup.chain.keys import derive_user
from holdup.chain.prove import plan_attempts, prove, prove_fake, to_near_actions
from holdup.config import offline_settings
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
