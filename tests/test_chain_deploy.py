"""The deployment plan creates exactly the accounts and keys the security model needs."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from near import KeyPairSigner, generate_key
from near.wire import Action

from holdup.chain import deploy as deploy_module
from holdup.chain.deploy import (
    DeployKeys,
    credentials_for,
    deploy,
    deploy_settings,
    execute_plan,
    plan_deploy,
    write_credentials,
)
from holdup.chain.profiles import PROFILES, config_input, vault_config
from holdup.config import OFFLINE_SECRET, Settings

ROOT = "sandbox"


def make_plan(profile: str = "demo") -> Any:
    settings = deploy_settings(Settings(), "sandbox", ROOT)
    return plan_deploy(settings, profile=profile, vault_wasm=b"vault-wasm", token_wasm=b"token-wasm", keys=DeployKeys())


def kinds(tx: Any) -> list[str]:
    return [type(action).__name__ for action in tx.actions]


def test_profiles_match_the_spec() -> None:
    demo, hosted = PROFILES["demo"], PROFILES["hosted"]
    assert (demo.new_payee_hold_ms, demo.trust_delay_ms, demo.guardian_delay_ms) == (20_000, 30_000, 30_000)
    assert (hosted.new_payee_hold_ms, hosted.trust_delay_ms, hosted.guardian_delay_ms) == (600_000,) * 3
    assert demo.max_hold_ms == hosted.max_hold_ms == 72 * 3_600_000
    settings = Settings(oracle_accounts=["oracle.x.testnet"], husd_token="husd.x.testnet", usdc_token="usdc.testnet")
    config = vault_config(settings, "hosted")
    assert config.instant_caps == {"near": 10 * 10**24, "usdc.testnet": 100 * 10**6, "husd.x.testnet": 100 * 10**6}
    assert (config.max_active_holds, config.registration_fee) == (10, 10**22)
    as_json = config_input(config)
    assert as_json["new_payee_hold_ms"] == "600000" and as_json["max_active_holds"] == 10
    assert as_json["registration_fee"] == str(10**22)


def test_each_contract_initialises_itself_and_ends_without_keys() -> None:
    plan = make_plan()
    by_label = {tx.label: tx for tx in plan.txs}
    for name, account, key, wasm in (("vault", "vault.sandbox", plan.keys.vault, b"vault-wasm"),
                                     ("token", "husd.sandbox", plan.keys.token, b"token-wasm")):
        create = by_label[f"create {name}"]
        assert (create.signer, create.receiver) == ("root", account)
        assert kinds(create) == ["CreateAccount", "Transfer", "AddKey"]
        assert create.actions[2].public_key.data == key.public_key.data
        init = by_label[f"deploy and initialise {name}"]
        assert (init.signer, init.receiver) == (name, account)  # new() is private: the account calls it itself
        assert kinds(init) == ["DeployContract", "FunctionCall"]
        assert init.actions[0].code == wasm and init.actions[1].method_name == "new"
        delete = by_label[f"delete the {name}'s last key"]
        assert (delete.signer, delete.receiver) == (name, account)
        (action,) = delete.actions
        assert isinstance(action, Action.DeleteKey) and action.public_key.data == key.public_key.data

    config = json.loads(by_label["deploy and initialise vault"].actions[1].args)["config"]
    assert config["oracles"] == ["oracle.sandbox"] and config["tokens"] == ["husd.sandbox"]
    assert json.loads(by_label["deploy and initialise token"].actions[1].args) == {
        "owner_id": ROOT, "total_supply": str(1_000_000 * 10**6)}
    assert plan.txs[-1].label == "delete the vault's last key"


def test_oracle_and_keeper_get_one_narrow_key_each() -> None:
    plan = make_plan()
    for account, method, key in (("oracle.sandbox", "extend_hold", plan.keys.oracle),
                                 ("keeper.sandbox", "release", plan.keys.keeper)):
        (tx,) = [t for t in plan.txs if t.receiver == account]
        assert kinds(tx) == ["CreateAccount", "Transfer", "AddKey"]
        add_key = tx.actions[2]
        permission = add_key.access_key.permission
        assert (permission.receiver_id, permission.method_names, permission.allowance) == ("vault.sandbox", [method], None)
        assert add_key.public_key.data == key.public_key.data


def test_vault_registration_and_demo_payees() -> None:
    plan = make_plan()
    registration = [t for t in plan.txs if t.label.startswith("register the vault")]
    assert [t.receiver for t in registration] == ["husd.sandbox"]
    assert json.loads(registration[0].actions[0].args) == {"account_id": "vault.sandbox", "registration_only": True}
    payees = [t for t in plan.txs if t.label.startswith("create demo payee")]
    assert {t.receiver for t in payees} >= {"safe-wallet-77.sandbox", "northwindpaper.sandbox"}
    assert all(kinds(t) == ["CreateAccount", "Transfer"] for t in payees)  # keyless
    assert not [t for t in make_plan("hosted").txs if t.label.startswith("create demo payee")]
    with pytest.raises(ValueError):
        make_plan("fast")


class RecordingNear:
    def __init__(self, remaining_keys: list[Any]) -> None:
        self.calls: list[tuple[str, str, list[str]]] = []
        self.remaining = remaining_keys

    async def send_transaction(self, receiver: str, actions: list[Any], *, signer: Any) -> Any:
        self.calls.append((signer.account_id, receiver, [type(a).__name__ for a in actions]))
        return type("Result", (), {"transaction_hash": f"hash-{len(self.calls)}"})()

    async def access_keys(self, account_id: str) -> list[Any]:
        return self.remaining


async def test_execution_signs_in_order_and_checks_the_vault_is_keyless() -> None:
    plan = make_plan()
    root = KeyPairSigner(account_id=ROOT, key_pair=generate_key())
    near = RecordingNear([])
    done = await execute_plan(plan, near, root)  # type: ignore[arg-type]
    assert [label for label, _ in done] == [tx.label for tx in plan.txs]
    signers = {tx.label: call[0] for tx, call in zip(plan.txs, near.calls, strict=True)}
    assert signers["deploy and initialise vault"] == signers["delete the vault's last key"] == "vault.sandbox"
    assert signers["deploy and initialise token"] == "husd.sandbox"
    assert signers["create the oracle with one extend_hold key"] == ROOT
    with pytest.raises(RuntimeError):
        await execute_plan(plan, RecordingNear(["still-a-key"]), root)  # type: ignore[arg-type]


def test_credentials_file_is_private(tmp_path: Path) -> None:
    plan = make_plan()
    settings = deploy_settings(Settings(), "sandbox", ROOT)
    root = KeyPairSigner(account_id=ROOT, key_pair=generate_key())
    data = credentials_for(plan, settings, [("x", "hash")], root)
    path = tmp_path / ".holdup" / "credentials.json"
    write_credentials(path, data)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    saved = json.loads(path.read_text())
    assert saved["oracle_private_key"] == plan.keys.oracle.secret_key
    assert saved["vault_account"] == "vault.sandbox" and saved["oracle_accounts"] == ["oracle.sandbox"]
    assert saved["secret"]
    assert "vault_private_key" not in saved  # the vault key is deleted, never saved


async def test_a_deployment_never_keeps_the_public_offline_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_network(plan: Any, near: Any, root_signer: Any) -> list[tuple[str, str]]:
        return [(tx.label, "hash") for tx in plan.txs]

    monkeypatch.setattr(deploy_module, "execute_plan", no_network)
    base = Settings(network="sandbox", root_account=ROOT, secret=OFFLINE_SECRET,
                    treasury_private_key=generate_key().secret_key)
    data = await deploy(base, network="sandbox", profile="demo", credentials_path=tmp_path / "credentials.json")
    assert data["secret"] and data["secret"] != OFFLINE_SECRET
    assert json.loads((tmp_path / "credentials.json").read_text())["secret"] == data["secret"]
