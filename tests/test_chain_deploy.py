"""The deployment plan creates exactly the accounts and keys the security model needs."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from near import KeyPairSigner, generate_key
from near.wire import Action

from holdup import cli
from holdup.chain import deploy as deploy_module
from holdup.chain.budget import YOCTO_PER_NEAR, deploy_budget, near_text
from holdup.chain.deploy import (
    DeployKeys,
    DeployRefused,
    credentials_for,
    deploy,
    deploy_settings,
    execute_plan,
    plan_deploy,
    write_credentials,
)
from holdup.chain.profiles import (
    DEMO_PAYEES,
    FRIENDS,
    PROFILES,
    SAMPLE_PAYEES,
    config_input,
    vault_config,
)
from holdup.config import OFFLINE_SECRET, Settings

ROOT = "sandbox"


def make_plan(profile: str = "demo") -> Any:
    settings = deploy_settings(Settings(), "sandbox", ROOT)
    return plan_deploy(settings, profile=profile, vault_wasm=b"vault-wasm", token_wasm=b"token-wasm", keys=DeployKeys())


def kinds(tx: Any) -> list[str]:
    return [type(action).__name__ for action in tx.actions]


def test_a_root_too_long_for_the_accounts_under_it_is_refused_before_anything_is_sent() -> None:
    def plan_under(root: str) -> Any:
        settings = deploy_settings(Settings(network="testnet"), "testnet", root)
        return plan_deploy(settings, profile="hosted", vault_wasm=b"v", token_wasm=b"t", keys=DeployKeys())

    assert plan_under("a" * 40 + ".testnet").root == "a" * 40 + ".testnet"  # 48 characters fit
    with pytest.raises(DeployRefused) as refused:
        plan_under("a" * 41 + ".testnet")
    assert f"northwind-paper.{'a' * 41}.testnet" in str(refused.value) and "Nothing was sent" in str(refused.value)
    with pytest.raises(DeployRefused, match="ua{10}"):
        plan_under("a" * 45 + ".testnet")  # 53: the payees and every user account would be too long


def test_profiles_match_the_spec() -> None:
    demo, hosted = PROFILES["demo"], PROFILES["hosted"]
    assert (demo.new_payee_hold_ms, demo.trust_delay_ms, demo.guardian_delay_ms) == (20_000, 30_000, 30_000)
    # Judges try the hosted bot for a few minutes: new payees and trust wait five minutes.
    assert (hosted.new_payee_hold_ms, hosted.trust_delay_ms) == (300_000,) * 2
    # Long enough for a guardian to notice a change and veto it.
    assert hosted.guardian_delay_ms == 24 * 3_600_000
    assert demo.max_hold_ms == hosted.max_hold_ms == 72 * 3_600_000
    settings = Settings(oracle_accounts=["oracle.x.testnet"], husd_token="husd.x.testnet", usdc_token="usdc.testnet")
    config = vault_config(settings, "hosted")
    assert config.instant_caps == {"near": 10 * 10**24, "usdc.testnet": 100 * 10**6, "husd.x.testnet": 100 * 10**6}
    assert (config.max_active_holds, config.registration_fee) == (10, 10**22)
    as_json = config_input(config)
    assert as_json["new_payee_hold_ms"] == "300000" and as_json["trust_delay_ms"] == "300000"
    assert as_json["guardian_delay_ms"] == "86400000" and as_json["max_hold_ms"] == "259200000"
    assert as_json["max_active_holds"] == 10
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


def test_vault_registration_and_payees() -> None:
    plan = make_plan()
    registration = [t for t in plan.txs if t.label.startswith("register the vault")]
    assert [t.receiver for t in registration] == ["husd.sandbox"]
    assert json.loads(registration[0].actions[0].args) == {"account_id": "vault.sandbox", "registration_only": True}
    payees = [t for t in plan.txs if t.payee]
    assert [t.receiver for t in payees] == [f"{label}.sandbox" for label in (*DEMO_PAYEES, *FRIENDS)]
    # Keyless accounts of 100 bytes need no balance (NEP-448): the root only pays to create them.
    assert all(kinds(t) == ["CreateAccount"] and t.signer == "root" for t in payees)
    # The hosted bot creates only what its training samples pay or trust.
    # The hosted bot creates only what its training samples pay or trust, and a practice friend for
    # each tenth of the people (samples.friend_account).
    hosted = [t.receiver for t in make_plan("hosted").txs if t.payee]
    assert hosted == [f"{label}.sandbox" for label in (*SAMPLE_PAYEES, *FRIENDS)]
    assert {"safe-wallet-77.sandbox", "northwind-paper.sandbox", "northwindpaper.sandbox", "ravi.sandbox"} < set(hosted)
    assert len(FRIENDS) == 10 and {f"ravi-{n}.sandbox" for n in range(10)} < set(hosted)
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


class BalanceNear:
    """Answers ``account`` with a fixed balance; sends nothing."""

    def __init__(self, near: str) -> None:
        self.amount = int(float(near) * YOCTO_PER_NEAR)
        self.asked: list[str] = []

    async def account(self, account_id: str) -> Any:
        self.asked.append(account_id)
        return type("View", (), {"amount": self.amount, "locked": 0, "storage_usage": 182})()


async def test_a_deployment_never_keeps_the_public_offline_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_network(plan: Any, near: Any, root_signer: Any) -> list[tuple[str, str]]:
        return [(tx.label, "hash") for tx in plan.txs]

    monkeypatch.setattr(deploy_module, "execute_plan", no_network)
    base = Settings(network="sandbox", root_account=ROOT, secret=OFFLINE_SECRET,
                    treasury_private_key=generate_key().secret_key)
    data = await deploy(base, network="sandbox", profile="demo", credentials_path=tmp_path / "credentials.json",
                        near=BalanceNear("100"))  # type: ignore[arg-type]
    assert data["secret"] and data["secret"] != OFFLINE_SECRET
    assert json.loads((tmp_path / "credentials.json").read_text())["secret"] == data["secret"]


async def test_deploy_refuses_to_start_when_the_root_cannot_pay_for_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[Any] = []

    async def no_network(plan: Any, near: Any, root_signer: Any) -> list[tuple[str, str]]:
        sent.append(plan)
        return []

    monkeypatch.setattr(deploy_module, "execute_plan", no_network)
    base = Settings(network="testnet", root_account="me.testnet", secret="s" * 32,
                    treasury_private_key=generate_key().secret_key)
    near = BalanceNear("4.8")  # what bootstrap's registrar call leaves the root with
    with pytest.raises(DeployRefused) as info:
        await deploy(base, network="testnet", profile="hosted", credentials_path=tmp_path / "c.json",
                     near=near)  # type: ignore[arg-type]
    message = str(info.value)
    budget = deploy_budget(deploy_module.plan_from_res(deploy_settings(base, "testnet", "me.testnet"), profile="hosted"))
    short = budget.total - 48 * YOCTO_PER_NEAR // 10
    assert f"{near_text(short)} NEAR short" in message and "Nothing was sent" in message
    assert "scripts/testnet.sh topup me.testnet --min" in message and "create the keeper" in message
    assert near.asked == ["me.testnet"] and sent == [] and not (tmp_path / "c.json").exists()


def test_the_deploy_command_reports_a_refusal_in_a_few_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def refused(base: Settings, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["reserve_near"] is None  # the vault's own margin (0.25 NEAR) unless --reserve says otherwise
        raise DeployRefused("me.testnet holds 4.8 NEAR, but this deployment needs 11.06 NEAR: 6.26 NEAR short.")

    monkeypatch.setattr(deploy_module, "deploy", refused)
    monkeypatch.setenv("HOLDUP_CREDENTIALS", str(tmp_path / "credentials.json"))
    assert cli.main(["deploy", "--network", "testnet", "--profile", "hosted", "--root", "me.testnet"]) == 2
    err = capsys.readouterr().err
    assert "holdup deploy: me.testnet holds 4.8 NEAR" in err and "6.26 NEAR short" in err
    assert "Traceback" not in err and not (tmp_path / "credentials.json").exists()
