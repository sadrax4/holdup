"""The deployment plan creates exactly the accounts and keys the security model needs."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from near import KeyPairSigner, generate_key
from near.wire import Action

from holdup.chain.deploy import credentials_for, deploy_settings, execute_plan, plan_deploy, write_credentials
from holdup.chain.profiles import PROFILES, config_input, vault_config
from holdup.config import Settings

ROOT = "sandbox"


def make_plan(profile: str = "demo") -> Any:
    settings = deploy_settings(Settings(), "sandbox", ROOT)
    keys = (generate_key(), generate_key(), generate_key())
    return plan_deploy(settings, profile=profile, vault_wasm=b"vault-wasm", token_wasm=b"token-wasm", keys=keys)


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


def test_the_vault_is_created_initialised_and_left_without_keys() -> None:
    plan = make_plan()
    by_label = {tx.label: tx for tx in plan.txs}
    vault_tx = by_label["create and initialise the vault"]
    assert vault_tx.receiver == "vault.sandbox" and vault_tx.signer == "root"
    assert kinds(vault_tx) == ["CreateAccount", "Transfer", "AddKey", "DeployContract", "FunctionCall"]
    assert vault_tx.actions[3].code == b"vault-wasm"
    init = vault_tx.actions[4]
    config = json.loads(init.args)["config"]
    assert init.method_name == "new" and config["oracles"] == ["oracle.sandbox"] and config["tokens"] == ["husd.sandbox"]

    last = plan.txs[-1]
    assert (last.label, last.signer, last.receiver) == ("delete the vault's last key", "vault", "vault.sandbox")
    (delete,) = last.actions
    assert isinstance(delete, Action.DeleteKey)
    assert delete.public_key.data == plan.vault_key.public_key.data


def test_oracle_and_keeper_get_one_narrow_key_each() -> None:
    plan = make_plan()
    for account, method, key in (("oracle.sandbox", "extend_hold", plan.oracle_key),
                                 ("keeper.sandbox", "release", plan.keeper_key)):
        (tx,) = [t for t in plan.txs if t.receiver == account]
        assert kinds(tx) == ["CreateAccount", "Transfer", "AddKey"]
        add_key = tx.actions[2]
        permission = add_key.access_key.permission
        assert (permission.receiver_id, permission.method_names, permission.allowance) == ("vault.sandbox", [method], None)
        assert add_key.public_key.data == key.public_key.data


def test_token_and_demo_payees() -> None:
    plan = make_plan()
    token = plan.txs[0]
    assert token.receiver == "husd.sandbox" and kinds(token) == ["CreateAccount", "Transfer", "DeployContract", "FunctionCall"]
    assert json.loads(token.actions[3].args) == {"owner_id": ROOT, "total_supply": str(1_000_000 * 10**6)}
    registration = [t for t in plan.txs if t.label.startswith("register the vault")]
    assert [t.receiver for t in registration] == ["husd.sandbox"]
    assert json.loads(registration[0].actions[0].args) == {"account_id": "vault.sandbox", "registration_only": True}
    payees = [t.receiver for t in plan.txs if t.label.startswith("create demo payee")]
    assert "safe-wallet-77.sandbox" in payees and "northwindpaper.sandbox" in payees
    assert all(kinds(t) == ["CreateAccount", "Transfer"] for t in plan.txs if t.label.startswith("create demo payee"))
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
    assert near.calls[-1][:2] == ("vault.sandbox", "vault.sandbox")
    assert {signer for signer, _, _ in near.calls[:-1]} == {ROOT}
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
    assert saved["oracle_private_key"] == plan.oracle_key.secret_key
    assert saved["vault_account"] == "vault.sandbox" and saved["oracle_accounts"] == ["oracle.sandbox"]
    assert saved["secret"]
    assert "vault_private_key" not in saved  # the vault key is deleted, never saved
