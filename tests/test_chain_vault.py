"""NearVault against a mocked JSON-RPC node with realistic nearcore response shapes."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from typing import Any

import base58
import httpx
import pytest
import respx
from near import PublicKey
from near.wire import Action, SignedTransaction

from holdup.chain import errors as E
from holdup.chain.errors import VaultError
from holdup.chain.keys import USER_METHODS, derive_user
from holdup.chain.vault import NearVault
from holdup.config import Settings
from holdup.models import NEAR, HoldStatus

RPC = "http://rpc.test"
VAULT = "vault.holdup.testnet"
ORACLE = "oracle.holdup.testnet"
KEEPER = "keeper.holdup.testnet"
ROOT = "holdup.testnet"
TOKEN = "husd.holdup.testnet"
BLOCK_HASH = base58.b58encode(b"\x07" * 32).decode()
NOW_MS = 1_793_610_000_000
USER = derive_user("secret", "tg:1", ROOT)


def _key(seed: bytes) -> str:
    from near import Ed25519KeyPair

    return Ed25519KeyPair(hashlib.sha256(seed).digest()).secret_key


SETTINGS = Settings(
    network="testnet",
    rpc_url=RPC,
    root_account=ROOT,
    vault_account=VAULT,
    oracle_accounts=[ORACLE],
    keeper_account=KEEPER,
    treasury_private_key=_key(b"treasury"),
    oracle_private_key=_key(b"oracle"),
    keeper_private_key=_key(b"keeper"),
    husd_token=TOKEN,
)


class RpcFault(Exception):
    def __init__(self, error: dict[str, Any]) -> None:
        self.error = error


def hold_view(**overrides: Any) -> dict[str, Any]:
    view = {
        "id": "7", "sender": USER.account_id, "payee": "shop.testnet", "asset": NEAR,
        "amount": str(10**24), "created_ms": str(NOW_MS), "release_at_ms": str(NOW_MS + 600_000),
        "status": "held", "instant": False, "flagged": False, "extensions": 0, "reason_bits": 0,
        "evidence_hash": None, "memo_hash": None, "settled_ms": None, "cancelled_by": None,
    }
    view.update(overrides)
    return view


def event_log(name: str, data: dict[str, Any]) -> str:
    return "EVENT_JSON:" + json.dumps({"standard": "holdup", "version": "1.0.0", "event": name, "data": [data]})


def receipt(receipt_id: str, executor: str, status: dict[str, Any], logs: list[str] | None = None) -> dict[str, Any]:
    return {
        "proof": [],
        "block_hash": BLOCK_HASH,
        "id": receipt_id,
        "outcome": {
            "logs": logs or [],
            "receipt_ids": [],
            "gas_burnt": 2_428_000_000_000,
            "tokens_burnt": "242800000000000000000",
            "executor_id": executor,
            "status": status,
            "metadata": {"version": 3, "gas_profile": []},
        },
    }


def outcome(tx: SignedTransaction, tx_hash: str, status: dict[str, Any], receipts: list[dict[str, Any]]) -> dict[str, Any]:
    t = tx.transaction
    return {
        "final_execution_status": "EXECUTED_OPTIMISTIC",
        "status": status,
        "transaction": {
            "signer_id": t.signer_id, "public_key": "ed25519:x", "nonce": t.nonce, "receiver_id": t.receiver_id,
            "actions": [], "priority_fee": 0, "signature": "ed25519:x", "hash": tx_hash,
        },
        "transaction_outcome": receipt(tx_hash, t.signer_id, {"SuccessReceiptId": "R1"}),
        "receipts_outcome": receipts,
    }


def success(value: Any) -> dict[str, Any]:
    return {"SuccessValue": base64.b64encode(json.dumps(value).encode()).decode() if value is not None else ""}


def panic(message: str) -> dict[str, Any]:
    return {"Failure": {"ActionError": {"index": 0, "kind": {"FunctionCallError": {"ExecutionError": message}}}}}


Outcome = Callable[[SignedTransaction, str], dict[str, Any]]


class FakeNode:
    """Answers ``block``, ``query`` and ``send_tx`` the way nearcore 2.x does."""

    def __init__(self) -> None:
        self.views: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self.accounts: set[str] = {VAULT, ROOT}
        self.outcomes: list[Outcome] = []
        self.sent: list[SignedTransaction] = []
        self.methods: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.methods.append(body["method"])
        try:
            result = self.dispatch(body["method"], body["params"])
        except RpcFault as fault:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": fault.error})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "block":
            nanos = NOW_MS * 1_000_000
            return {"header": {"height": 1000, "hash": BLOCK_HASH, "timestamp": nanos, "timestamp_nanosec": str(nanos)}}
        if method == "send_tx":
            signed = SignedTransaction.from_borsh(base64.b64decode(params["signed_tx_base64"]))
            self.sent.append(signed)
            tx_hash = base58.b58encode(hashlib.sha256(signed.transaction.to_borsh()).digest()).decode()
            return self.outcomes.pop(0)(signed, tx_hash)
        kind = params["request_type"]
        if kind == "view_access_key":
            return {"nonce": 41, "permission": "FullAccess", "block_height": 1000, "block_hash": BLOCK_HASH}
        if kind == "view_account":
            if params["account_id"] not in self.accounts:
                raise RpcFault(unknown_account(params["account_id"]))
            return {"amount": "1", "locked": "0", "code_hash": "11111111111111111111111111111111",
                    "storage_usage": 182, "storage_paid_at": 0, "block_height": 1000, "block_hash": BLOCK_HASH}
        if kind == "call_function":
            args = json.loads(base64.b64decode(params["args_base64"]) or b"{}")
            value = self.views[params["method_name"]](args)
            return {"result": list(json.dumps(value).encode()), "logs": [], "block_height": 1000, "block_hash": BLOCK_HASH}
        raise AssertionError(f"unexpected query {kind}")


def unknown_account(account_id: str) -> dict[str, Any]:
    return {
        "name": "HANDLER_ERROR",
        "cause": {"name": "UNKNOWN_ACCOUNT", "info": {"requested_account_id": account_id, "block_height": 1000, "block_hash": BLOCK_HASH}},
        "code": -32000,
        "message": "Server error",
        "data": f"account {account_id} does not exist while viewing",
    }


@pytest.fixture
def node() -> Any:
    fake = FakeNode()
    with respx.mock(assert_all_called=False) as router:
        router.post(RPC).mock(side_effect=fake.handle)
        yield fake


@pytest.fixture
async def vault(node: FakeNode) -> Any:
    near_vault = NearVault(SETTINGS)
    yield near_vault
    await near_vault.aclose()


def only_call(signed: SignedTransaction) -> Action.FunctionCall:
    (action,) = signed.transaction.actions
    assert isinstance(action, Action.FunctionCall)
    return action


async def test_send_is_signed_by_the_user_key_and_parses_the_hold(node: FakeNode, vault: NearVault) -> None:
    created = event_log("hold_created", {"hold_id": "7", "sender": USER.account_id, "payee": "shop.testnet",
                                         "asset": NEAR, "amount": str(10**24)})
    node.outcomes.append(
        lambda tx, h: outcome(tx, h, success({"hold": hold_view()}), [
            receipt("R1", VAULT, success({"hold": hold_view()}), [created]),
            receipt("R2", USER.account_id, {"SuccessValue": ""}),
        ])
    )
    result = await vault.send(USER, "shop.testnet", NEAR, 10**24, "ab" * 32)

    assert result.hold.id == 7 and result.hold.status == HoldStatus.HELD
    assert [e["event"] for e in result.tx.events] == ["hold_created"]
    assert result.tx.link == f"https://testnet.near.rocks/tx/{result.tx.tx_hash}"
    signed = node.sent[0]
    assert (signed.transaction.signer_id, signed.transaction.receiver_id) == (USER.account_id, VAULT)
    call = only_call(signed)
    assert call.method_name == "send" and call.deposit == 0
    assert json.loads(call.args) == {"payee": "shop.testnet", "asset": NEAR, "amount": str(10**24), "memo_hash": "ab" * 32}
    digest = hashlib.sha256(signed.transaction.to_borsh()).digest()
    assert PublicKey.parse(USER.public_key).verify(signed.signature.data, digest)


async def test_a_guardian_veto_is_signed_by_the_guardians_key(node: FakeNode, vault: NearVault) -> None:
    guardian = derive_user("secret", "tg:2", "holdup.testnet")
    vetoed = event_log("guardian_change_vetoed", {"owner": USER.account_id, "guardian": guardian.account_id,
                                                   "vetoed": None})
    node.outcomes.append(lambda tx, h: outcome(tx, h, success({}), [receipt("R1", VAULT, success({}), [vetoed])]))
    result = await vault.veto_guardian_change(guardian, USER.account_id)
    assert [e["event"] for e in result.events] == ["guardian_change_vetoed"]
    signed = node.sent[0]
    assert (signed.transaction.signer_id, signed.transaction.receiver_id) == (guardian.account_id, VAULT)
    call = only_call(signed)
    assert (call.method_name, call.deposit, json.loads(call.args)) == (
        "veto_guardian_change", 0, {"owner": USER.account_id}
    )
    assert "veto_guardian_change" in USER_METHODS  # every user key may call it, as a guardian


async def test_a_contract_panic_becomes_a_vault_error_with_its_code(node: FakeNode, vault: NearVault) -> None:
    failure = panic("Smart contract panicked: E_ONLY_LONGER: a hold can only be made longer")
    node.outcomes.append(lambda tx, h: outcome(tx, h, failure, [receipt("R1", VAULT, failure)]))
    with pytest.raises(VaultError) as info:
        await vault.extend_hold(7, NOW_MS, 1, "{}")
    assert info.value.code == E.E_ONLY_LONGER
    assert info.value.message == "a hold can only be made longer"
    assert info.value.tx_hash == base58.b58encode(hashlib.sha256(node.sent[0].transaction.to_borsh()).digest()).decode()
    assert node.sent[0].transaction.signer_id == ORACLE
    assert json.loads(only_call(node.sent[0]).args)["reason_bits"] == 1


async def test_a_failed_payout_receipt_does_not_fail_the_release(node: FakeNode, vault: NearVault) -> None:
    failed_view = hold_view(status="failed", settled_ms=str(NOW_MS))
    hold_failed = event_log("hold_failed", {"hold_id": "7", "refunded_to": USER.account_id})
    transfer_failure = {"Failure": {"ActionError": {"index": 0, "kind": {"AccountDoesNotExist": {"account_id": "gone.testnet"}}}}}
    node.outcomes.append(
        lambda tx, h: outcome(tx, h, success(failed_view), [
            receipt("R1", VAULT, {"SuccessReceiptId": "R3"}),
            receipt("R2", "gone.testnet", transfer_failure),
            receipt("R3", VAULT, success(failed_view), [hold_failed]),
        ])
    )
    tx = await vault.release(7)
    assert tx.return_value["status"] == "failed"
    assert [e["event"] for e in tx.events] == ["hold_failed"]
    assert node.sent[0].transaction.signer_id == KEEPER


async def test_access_key_rejections_are_reported(node: FakeNode, vault: NearVault) -> None:
    def reject(tx: SignedTransaction, tx_hash: str) -> dict[str, Any]:
        raise RpcFault({
            "name": "HANDLER_ERROR", "cause": {"name": "INVALID_TRANSACTION", "info": {}}, "code": -32000,
            "message": "Server error",
            "data": {"TxExecutionError": {"InvalidTxError": {"InvalidAccessKeyError": {"MethodNameMismatch": {"method_name": "send"}}}}},
        })

    node.outcomes.append(reject)
    with pytest.raises(VaultError) as info:
        await vault.cancel(USER, 7)
    assert info.value.code == E.E_TX_REJECTED
    assert "MethodNameMismatch" in info.value.message


async def test_views_parse_contract_json(node: FakeNode, vault: NearVault) -> None:
    node.views["get_hold"] = lambda args: hold_view(id=args["hold_id"]) if args["hold_id"] == "7" else None
    node.views["get_balance"] = lambda args: {"near": "5", "tokens": {TOKEN: "7"}}
    node.views["get_user"] = lambda args: {
        "account_id": args["account_id"], "registered_ms": "1", "guardian": None,
        "pending_guardian": {"guardian": "sam.testnet", "effective_ms": "9"},
        "trusted": [{"payee": "mum.testnet", "effective_ms": "3"}], "active_holds": 1,
    }
    node.views["get_due_holds"] = lambda args: [hold_view()] if args["limit"] == 50 else []
    node.views["get_payee_stats"] = lambda args: {"holds": 2, "flagged": 1, "cancelled": 1,
                                                  "cancelled_after_flag": 1, "released": 0, "last_flag_ms": "4"}
    calls = []

    def config(args: dict[str, Any]) -> dict[str, Any]:
        calls.append(args)
        return {"oracles": [ORACLE], "tokens": [TOKEN], "new_payee_hold_ms": "600000", "max_hold_ms": "259200000",
                "trust_delay_ms": "600000", "guardian_delay_ms": "600000", "instant_caps": {NEAR: "1"},
                "max_active_holds": 10, "registration_fee": "10000000000000000000000", "version": "1.0.0"}

    node.views["get_config"] = config

    assert (await vault.get_hold(7)).id == 7  # type: ignore[union-attr]
    assert await vault.get_hold(8) is None
    assert await vault.get_balance(USER.account_id) == {NEAR: 5, TOKEN: 7}
    user = await vault.get_user(USER.account_id)
    assert user is not None and user.pending_guardian == "sam.testnet" and user.trusted[0].payee == "mum.testnet"
    assert [h.id for h in await vault.get_due_holds()] == [7]
    assert (await vault.get_payee_stats("shop.testnet")).cancelled_after_flag == 1
    assert (await vault.get_config()).max_hold_ms == 259_200_000
    await vault.get_config()
    assert len(calls) == 1  # the configuration is fixed forever, so it is cached
    assert await vault.now_ms() == NOW_MS


async def test_view_panics_and_missing_accounts(node: FakeNode, vault: NearVault) -> None:
    def broken(args: dict[str, Any]) -> Any:
        message = 'wasm execution failed with error: FunctionCallError(ExecutionError("Smart contract panicked: E_HOLD_NOT_FOUND: hold 9 does not exist"))'
        raise RpcFault({
            "name": "HANDLER_ERROR",
            "cause": {"name": "CONTRACT_EXECUTION_ERROR", "info": {"vm_error": message, "block_height": 1000, "block_hash": BLOCK_HASH}},
            "code": -32000, "message": "Server error", "data": message,
        })

    node.views["get_hold"] = broken
    with pytest.raises(VaultError) as info:
        await vault.get_hold(9)
    assert (info.value.code, info.value.message) == (E.E_HOLD_NOT_FOUND, "hold 9 does not exist")
    assert await vault.account_exists(VAULT) is True
    assert await vault.account_exists("nobody.testnet") is False


async def test_onboarding_creates_a_restricted_key_and_funds_the_user(node: FakeNode, vault: NearVault) -> None:
    node.views["is_registered"] = lambda args: False
    node.views["get_config"] = lambda args: {
        "oracles": [ORACLE], "tokens": [TOKEN], "new_payee_hold_ms": "1", "max_hold_ms": "1", "trust_delay_ms": "1",
        "guardian_delay_ms": "1", "instant_caps": {}, "max_active_holds": 1, "registration_fee": "100", "version": "1.0.0"}
    for _ in range(3):
        node.outcomes.append(lambda tx, h: outcome(tx, h, success(None), [receipt("R1", VAULT, success(None))]))

    txs = await vault.onboard_user(USER, {NEAR: 2 * 10**24, TOKEN: 2_000 * 10**6})

    assert len(txs) == 3
    create, deposit, tokens = node.sent
    assert {s.transaction.signer_id for s in node.sent} == {ROOT}
    assert create.transaction.receiver_id == USER.account_id
    kinds = [type(a).__name__ for a in create.transaction.actions]
    assert kinds == ["CreateAccount", "Transfer", "AddKey"]
    assert create.transaction.actions[1].deposit == 5 * 10**22  # 0.05 NEAR for gas
    add_key = create.transaction.actions[2]
    permission = add_key.access_key.permission
    assert (permission.receiver_id, permission.method_names, permission.allowance) == (VAULT, list(USER_METHODS), None)
    assert add_key.public_key.data == PublicKey.parse(USER.public_key).data
    call = only_call(deposit)
    assert (call.method_name, call.deposit) == ("deposit_near_for", 100 + 2 * 10**24)
    assert json.loads(call.args) == {"account_id": USER.account_id}
    ft = only_call(tokens)
    assert tokens.transaction.receiver_id == TOKEN and ft.deposit == 1
    assert json.loads(ft.args) == {"receiver_id": VAULT, "amount": str(2_000 * 10**6),
                                   "msg": json.dumps({"credit_to": USER.account_id}, separators=(",", ":"))}


async def test_missing_role_keys_fail_before_signing(node: FakeNode) -> None:
    vault = NearVault(Settings(network="testnet", rpc_url=RPC, vault_account=VAULT, oracle_accounts=[ORACLE]))
    try:
        with pytest.raises(VaultError) as info:
            await vault.extend_hold(1, 2, 1, "{}")
        assert info.value.code == E.E_NOT_CONFIGURED
        assert node.sent == []
    finally:
        await vault.aclose()
