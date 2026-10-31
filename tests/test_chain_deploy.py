"""The deployment plan creates exactly the accounts and keys the security model needs."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
import sys
from pathlib import Path
from typing import Any

import base58
import pytest
from near import AccountNotFoundError, AsyncNear, KeyPairSigner, generate_key
from near.actions import transfer
from near.errors import AccessKeyNotFoundError, ContractPanicError, RpcError
from near.units import Amount
from near.wire import Action

from holdup import cli
from holdup.chain import deploy as deploy_module
from holdup.chain.budget import YOCTO_PER_NEAR, deploy_budget, near_text
from holdup.chain.deploy import (
    NO_CODE_HASH,
    UNFINISHED,
    DeployKeys,
    DeployRefused,
    DeployStopped,
    credentials_for,
    deploy,
    deploy_settings,
    execute_plan,
    plan_deploy,
    plan_from_res,
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
    """A chain where only the root exists, with a fixed balance; sends nothing."""

    def __init__(self, near: str, root: str) -> None:
        self.amount = int(float(near) * YOCTO_PER_NEAR)
        self.root = root
        self.asked: list[str] = []

    async def account(self, account_id: str) -> Any:
        self.asked.append(account_id)
        if account_id != self.root:
            raise AccountNotFoundError(account_id)
        return type("View", (), {"amount": self.amount, "locked": 0, "storage_usage": 182})()


async def test_a_deployment_never_keeps_the_public_offline_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_network(plan: Any, near: Any, root_signer: Any, **progress: Any) -> list[tuple[str, str]]:
        return [(tx.label, "hash") for tx in plan.txs]

    monkeypatch.setattr(deploy_module, "execute_plan", no_network)
    base = Settings(network="sandbox", root_account=ROOT, secret=OFFLINE_SECRET,
                    treasury_private_key=generate_key().secret_key)
    data = await deploy(base, network="sandbox", profile="demo", credentials_path=tmp_path / "credentials.json",
                        near=BalanceNear("100", ROOT))  # type: ignore[arg-type]
    assert data["secret"] and data["secret"] != OFFLINE_SECRET
    assert json.loads((tmp_path / "credentials.json").read_text())["secret"] == data["secret"]


async def test_deploy_refuses_to_start_when_the_root_cannot_pay_for_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[Any] = []

    async def no_network(plan: Any, near: Any, root_signer: Any, **progress: Any) -> list[tuple[str, str]]:
        sent.append(plan)
        return []

    monkeypatch.setattr(deploy_module, "execute_plan", no_network)
    base = Settings(network="testnet", root_account="me.testnet", secret="s" * 32,
                    treasury_private_key=generate_key().secret_key)
    near = BalanceNear("4.8", "me.testnet")  # what bootstrap's registrar call leaves the root with
    with pytest.raises(DeployRefused) as info:
        await deploy(base, network="testnet", profile="hosted", credentials_path=tmp_path / "c.json",
                     near=near)  # type: ignore[arg-type]
    message = str(info.value)
    budget = deploy_budget(deploy_module.plan_from_res(deploy_settings(base, "testnet", "me.testnet"), profile="hosted"))
    short = budget.total - 48 * YOCTO_PER_NEAR // 10
    assert f"{near_text(short)} NEAR short" in message and "Nothing was sent" in message
    assert "scripts/testnet.sh topup me.testnet --min" in message and "create the keeper" in message
    # Only reads: whether the token's account exists already (a fresh chain: no), then the root's balance.
    assert near.asked == ["husd.me.testnet", "me.testnet"] and sent == [] and not (tmp_path / "c.json").exists()


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


# ----------------------------------------------------------------------
# on testnet: new keys that are not final yet, and resuming a deployment
# ----------------------------------------------------------------------

TESTNET_ROOT = "me.testnet"


def key_text(wire_key: Any) -> str:
    """``ed25519:...`` for a public key inside a near-kit action."""
    return "ed25519:" + base58.b58encode(bytes(wire_key.data)).decode()


def load_server_tools() -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / "server_tools.py"
    spec = importlib.util.spec_from_file_location("server_tools", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["server_tools"] = module  # dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


class Chain:
    """Accounts, keys, code and token registrations, changed by the deployment's actions.

    It answers like near-kit over a real node. A transaction signed with a key the account
    does not have raises AccessKeyNotFoundError before anything happens. A key that a
    transaction just added stays invisible to the first ``final_lag`` nonce reads, as near-kit
    reads a new signer's nonce at final finality while it returns from each transaction as soon
    as it ran. ``fail_at`` makes the n-th transaction that reaches the node fail with an RPC
    error, either before it runs or, with ``fail_after_running``, after it ran (a lost reply).
    """

    def __init__(self, root: str = TESTNET_ROOT, near: int = 20, *, final_lag: int = 0) -> None:
        self.root = root
        self.balances: dict[str, int] = {root: near * YOCTO_PER_NEAR}
        self.keys: dict[str, set[str]] = {root: set()}
        self.code: dict[str, str] = {}
        self.registered: dict[str, set[str]] = {}
        self.final_lag = final_lag
        self.unseen: dict[str, int] = {}  # key -> nonce reads that still miss it
        self.nonce_known: set[str] = set()  # keys near-kit has a nonce for
        self.refused: dict[str, int] = {}  # account -> AccessKeyNotFoundError raised for it
        self.sent: list[tuple[str, str, list[str]]] = []
        self.fail_at: int | None = None
        self.fail_after_running = False
        self.calls = 0  # send_transaction calls
        self.attempts = 0  # transactions that reached the node

    async def account(self, account_id: str) -> Any:
        if account_id not in self.balances:
            raise AccountNotFoundError(account_id)
        return type("View", (), {"amount": self.balances[account_id], "locked": 0, "storage_usage": 182,
                                 "code_hash": self.code.get(account_id, NO_CODE_HASH)})()

    async def access_keys(self, account_id: str) -> list[Any]:
        return [type("Key", (), {"public_key": key})() for key in sorted(self.keys.get(account_id, ()))]

    async def view(self, contract_id: str, method: str, args: dict[str, Any]) -> Any:
        assert method == "storage_balance_of"
        known = args["account_id"] in self.registered.get(contract_id, set())
        return {"total": "1250000000000000000000", "available": "0"} if known else None

    async def send_transaction(self, receiver: str, actions: list[Any], *, signer: Any) -> Any:
        self.calls += 1
        key = str(signer.public_key)
        if key not in self.nonce_known:  # near-kit reads the nonce of a key new to it at final finality
            if key not in self.keys.get(signer.account_id, set()) or self.unseen.get(key, 0) > 0:
                self.unseen[key] = self.unseen.get(key, 0) - 1
                self.refused[signer.account_id] = self.refused.get(signer.account_id, 0) + 1
                raise AccessKeyNotFoundError(signer.account_id, key)
            self.nonce_known.add(key)
        self.attempts += 1
        failing = self.fail_at == self.attempts
        if failing and not self.fail_after_running:
            raise RpcError("RPC error [TIMEOUT_ERROR]: the node did not answer in time")
        if any(isinstance(a, Action.CreateAccount) for a in actions) and receiver in self.balances:
            raise ContractPanicError(f"AccountAlreadyExists (account_id: {receiver})")
        for action in actions:
            self.apply(signer.account_id, receiver, action)
        self.sent.append((signer.account_id, receiver, [type(a).__name__ for a in actions]))
        if failing:
            raise RpcError("RPC error [TIMEOUT_ERROR]: the node did not answer in time")
        return type("Result", (), {"transaction_hash": f"hash-{len(self.sent)}"})()

    def apply(self, signer: str, receiver: str, action: Any) -> None:
        if isinstance(action, Action.CreateAccount):
            self.balances[receiver], self.keys[receiver] = 0, set()
        elif isinstance(action, Action.Transfer):
            self.balances[signer] -= action.deposit
            self.balances[receiver] += action.deposit
        elif isinstance(action, Action.AddKey):
            self.keys[receiver].add(key_text(action.public_key))
            self.unseen[key_text(action.public_key)] = self.final_lag
        elif isinstance(action, Action.DeployContract):
            self.code[receiver] = base58.b58encode(hashlib.sha256(action.code).digest()).decode()
        elif isinstance(action, Action.FunctionCall) and action.method_name == "storage_deposit":
            self.balances[signer] -= action.deposit
            self.registered.setdefault(receiver, set()).add(json.loads(action.args)["account_id"])
        elif isinstance(action, Action.DeleteKey):
            self.keys[receiver].discard(key_text(action.public_key))


def base_for(root_key: Any) -> Settings:
    return Settings(network="testnet", root_account=TESTNET_ROOT, secret="s" * 32,
                    treasury_private_key=root_key.secret_key)


def labels() -> list[str]:
    plan = plan_from_res(deploy_settings(Settings(network="testnet"), "testnet", TESTNET_ROOT), profile="hosted")
    return [tx.label for tx in plan.txs]


@pytest.fixture
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deploy_module, "NEW_KEY_POLL_S", 0)


def bootstrapped(path: Path, root_key: Any) -> dict[str, Any]:
    """What scripts/testnet_tools.py bootstrap leaves in the credentials file."""
    data = {"network": "testnet", "root_account": TESTNET_ROOT, "treasury_private_key": root_key.secret_key,
            "funder_implicit_account": "ab" * 32, "funder_private_key": generate_key().secret_key}
    write_credentials(path, data)
    return data


async def test_near_kit_reads_a_new_keys_nonce_at_final_finality_before_sending_anything() -> None:
    """What the deployment relies on: AccessKeyNotFoundError for a new key means nothing was sent."""
    calls: list[tuple[str, Any]] = []

    class Node:
        async def call(self, method: str, params: Any) -> Any:
            calls.append((method, params))
            # nearcore 2.13 and later answer a missing key inside a successful result
            return {"error": "access key ed25519:x does not exist while viewing", "block_height": 1}

        async def aclose(self) -> None:
            pass

    near = AsyncNear("testnet", rpc_url="http://127.0.0.1:9")
    await near.aclose()
    near._transport = Node()  # type: ignore[assignment]  # noqa: SLF001
    signer = KeyPairSigner(account_id="husd.me.testnet", key_pair=generate_key())
    with pytest.raises(AccessKeyNotFoundError):
        await near.send_transaction("husd.me.testnet", [transfer(Amount.yocto(1))], signer=signer)
    assert [method for method, _ in calls] == ["query"]
    assert calls[0][1]["request_type"] == "view_access_key" and calls[0][1]["finality"] == "final"


async def test_the_new_token_and_vault_keys_are_waited_for_until_final(no_waiting: None) -> None:
    """On testnet the second transaction failed every time: the token's key was not final yet."""
    plan = plan_from_res(deploy_settings(Settings(network="testnet"), "testnet", TESTNET_ROOT), profile="hosted")
    root_key = generate_key()
    chain = Chain(final_lag=2)
    chain.keys[TESTNET_ROOT].add(str(root_key.public_key))
    root = KeyPairSigner(account_id=TESTNET_ROOT, key_pair=root_key)
    done = await execute_plan(plan, chain, root)  # type: ignore[arg-type]
    assert [label for label, _ in done] == labels()
    assert len(chain.sent) == len(plan.txs)  # each transaction went out once
    assert chain.refused == {"husd.me.testnet": 2, "vault.me.testnet": 2}
    assert chain.keys["vault.me.testnet"] == set() and chain.keys["husd.me.testnet"] == set()


async def test_a_key_that_never_shows_up_stops_the_deployment_after_a_while(
    tmp_path: Path, no_waiting: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deploy_module, "NEW_KEY_WAIT_S", 0.05)
    root_key = generate_key()
    path = tmp_path / "credentials.json"
    bootstrapped(path, root_key)
    chain = Chain(final_lag=10**9)
    chain.keys[TESTNET_ROOT].add(str(root_key.public_key))
    with pytest.raises(DeployStopped, match="deploy and initialise token failed: AccessKeyNotFoundError"):
        await deploy(base_for(root_key), network="testnet", profile="hosted", credentials_path=path,
                     near=chain)  # type: ignore[arg-type]
    record = json.loads(path.read_text())[UNFINISHED]
    assert [label for label, _ in record["transactions"]] == ["create token"]


async def test_a_deployment_that_stops_halfway_carries_on_where_it_stopped(tmp_path: Path, no_waiting: None) -> None:
    """On testnet the advised rerun failed with AccountAlreadyExists, and only a new root helped."""
    root_key = generate_key()
    path = tmp_path / "credentials.json"
    before = bootstrapped(path, root_key)
    chain = Chain(final_lag=1)
    chain.keys[TESTNET_ROOT].add(str(root_key.public_key))
    chain.fail_at = 7  # creating the keeper times out before it runs
    with pytest.raises(DeployStopped, match="create the keeper with one release key failed: RpcError"):
        await deploy(base_for(root_key), network="testnet", profile="hosted", credentials_path=path,
                     near=chain)  # type: ignore[arg-type]

    saved = json.loads(path.read_text())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    record = saved[UNFINISHED]
    assert [label for label, _ in record["transactions"]] == labels()[:6]
    assert set(record["keys"]) == {"vault", "token", "oracle", "keeper"}
    assert {key: saved[key] for key in before} == before  # the root's and the funding account's keys stay
    # server.sh sees an unfinished deployment, so it resumes instead of starting HoldUp on it.
    assert load_server_tools().credentials_status(path)[0] == "partial"

    chain.fail_at = None
    data = await deploy(base_for(root_key), network="testnet", profile="hosted", credentials_path=path,
                        near=chain)  # type: ignore[arg-type]
    assert len(chain.sent) == len(labels())  # nothing was sent twice
    assert [label for label, _ in data["deployment"]["transactions"]] == labels()
    assert [h for _, h in data["deployment"]["transactions"]][:6] == [h for _, h in record["transactions"]]
    text = path.read_text()
    saved = json.loads(text)
    assert UNFINISHED not in saved
    assert record["keys"]["vault"] not in text and record["keys"]["token"] not in text  # deleted on chain
    assert saved["oracle_private_key"] == record["keys"]["oracle"]
    assert saved["funder_private_key"] == before["funder_private_key"]  # the funding account's key is kept
    assert load_server_tools().credentials_status(path)[0] == "complete"
    assert chain.keys["vault.me.testnet"] == set() and chain.code["vault.me.testnet"] != NO_CODE_HASH


async def test_a_transaction_that_ran_just_before_the_run_stopped_is_not_sent_again(
    tmp_path: Path, no_waiting: None
) -> None:
    root_key = generate_key()
    path = tmp_path / "credentials.json"
    bootstrapped(path, root_key)
    chain = Chain()
    chain.keys[TESTNET_ROOT].add(str(root_key.public_key))
    chain.fail_at, chain.fail_after_running = 4, True  # the vault is deployed, but its answer is lost
    with pytest.raises(DeployStopped, match="deploy and initialise vault"):
        await deploy(base_for(root_key), network="testnet", profile="hosted", credentials_path=path,
                     near=chain)  # type: ignore[arg-type]
    assert len(json.loads(path.read_text())[UNFINISHED]["transactions"]) == 3
    chain.fail_at = None
    data = await deploy(base_for(root_key), network="testnet", profile="hosted", credentials_path=path,
                        near=chain)  # type: ignore[arg-type]
    assert len(chain.sent) == len(labels())
    assert dict(data["deployment"]["transactions"])["deploy and initialise vault"] == ""  # its hash never came back


async def test_a_lost_reply_to_the_very_first_transaction_is_carried_on_from_the_saved_keys(
    tmp_path: Path, no_waiting: None
) -> None:
    """On testnet the first attempt stranded husd.<root>: its key was only in memory when the run stopped."""
    root_key = generate_key()
    path = tmp_path / "credentials.json"
    bootstrapped(path, root_key)
    chain = Chain()
    chain.keys[TESTNET_ROOT].add(str(root_key.public_key))
    chain.fail_at, chain.fail_after_running = 1, True  # husd.<root> is created, but its answer is lost
    with pytest.raises(DeployStopped, match="create token failed: RpcError"):
        await deploy(base_for(root_key), network="testnet", profile="hosted", credentials_path=path,
                     near=chain)  # type: ignore[arg-type]
    record = json.loads(path.read_text())[UNFINISHED]
    assert record["transactions"] == []
    token_key = str(DeployKeys.from_secret_keys(record["keys"]).token.public_key)
    assert chain.keys["husd.me.testnet"] == {token_key}

    chain.fail_at = None
    data = await deploy(base_for(root_key), network="testnet", profile="hosted", credentials_path=path,
                        near=chain)  # type: ignore[arg-type]
    assert len(chain.sent) == len(labels())
    assert [label for label, _ in data["deployment"]["transactions"]] == labels()
    assert chain.keys["husd.me.testnet"] == set() and chain.keys["vault.me.testnet"] == set()


@pytest.mark.parametrize("after_running", [False, True])
async def test_a_stop_at_the_vaults_key_deletion_is_finished_by_running_again(
    tmp_path: Path, no_waiting: None, after_running: bool
) -> None:
    """By then the token, and maybe the vault, have code and no keys: still this deployment's accounts."""
    root_key = generate_key()
    path = tmp_path / "credentials.json"
    bootstrapped(path, root_key)
    chain = Chain()
    chain.keys[TESTNET_ROOT].add(str(root_key.public_key))
    chain.fail_at, chain.fail_after_running = len(labels()), after_running
    with pytest.raises(DeployStopped, match="delete the vault's last key failed: RpcError"):
        await deploy(base_for(root_key), network="testnet", profile="hosted", credentials_path=path,
                     near=chain)  # type: ignore[arg-type]
    assert chain.keys["husd.me.testnet"] == set()
    assert len(chain.keys["vault.me.testnet"]) == (0 if after_running else 1)

    chain.fail_at = None
    data = await deploy(base_for(root_key), network="testnet", profile="hosted", credentials_path=path,
                        near=chain)  # type: ignore[arg-type]
    assert chain.keys["vault.me.testnet"] == set() and len(chain.sent) == len(labels())
    assert UNFINISHED not in json.loads(path.read_text()) and data["vault_account"] == "vault.me.testnet"


async def test_a_transaction_the_root_signs_is_not_retried_when_its_key_is_missing(
    tmp_path: Path, no_waiting: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the keys added moments ago are waited for: a missing root key is the wrong key, not a late one."""
    monkeypatch.setattr(deploy_module, "NEW_KEY_WAIT_S", 0.05)
    root_key = generate_key()
    path = tmp_path / "credentials.json"
    bootstrapped(path, root_key)
    chain = Chain()
    chain.keys[TESTNET_ROOT].add(str(generate_key().public_key))
    with pytest.raises(DeployStopped, match="create token failed: AccessKeyNotFoundError"):
        await deploy(base_for(root_key), network="testnet", profile="hosted", credentials_path=path,
                     near=chain)  # type: ignore[arg-type]
    assert chain.calls == 1 and chain.sent == []


async def test_an_account_left_by_an_attempt_whose_keys_were_lost_is_refused_before_anything_is_sent(
    tmp_path: Path,
) -> None:
    """The first testnet attempt left husd.<root> with a key that only lived in memory."""
    root_key = generate_key()
    path = tmp_path / "credentials.json"
    before = bootstrapped(path, root_key)
    chain = Chain()
    chain.keys[TESTNET_ROOT].add(str(root_key.public_key))
    chain.balances["husd.me.testnet"], chain.keys["husd.me.testnet"] = YOCTO_PER_NEAR, {str(generate_key().public_key)}
    with pytest.raises(DeployRefused, match="husd.me.testnet already exists, but without this deployment's key"):
        await deploy(base_for(root_key), network="testnet", profile="hosted", credentials_path=path,
                     near=chain)  # type: ignore[arg-type]
    assert chain.calls == 0 and json.loads(path.read_text()) == before


async def test_credentials_of_another_root_or_deployment_are_never_replaced(tmp_path: Path) -> None:
    root_key = generate_key()
    path = tmp_path / "credentials.json"
    chain = Chain()
    chain.keys[TESTNET_ROOT].add(str(root_key.public_key))
    write_credentials(path, {"network": "testnet", "root_account": "other.testnet",
                             "treasury_private_key": generate_key().secret_key})
    with pytest.raises(DeployRefused, match="holds the key of another root account, other.testnet"):
        await deploy(base_for(root_key), network="testnet", profile="hosted", credentials_path=path,
                     near=chain)  # type: ignore[arg-type]
    unfinished = {"network": "testnet", "profile": "demo", "root_account": TESTNET_ROOT,
                  "keys": DeployKeys().secret_keys(), "transactions": [["create token", "h"]]}
    write_credentials(path, {"root_account": TESTNET_ROOT, "treasury_private_key": root_key.secret_key,
                             UNFINISHED: unfinished})
    with pytest.raises(DeployRefused, match="unfinished demo deployment under me.testnet"):
        await deploy(base_for(root_key), network="testnet", profile="hosted", credentials_path=path,
                     near=chain)  # type: ignore[arg-type]
    path.write_text("{not json")
    with pytest.raises(DeployRefused, match="not a JSON object"):
        await deploy(base_for(root_key), network="testnet", profile="hosted", credentials_path=path,
                     near=chain)  # type: ignore[arg-type]
    assert chain.calls == 0


def test_the_deploy_command_says_how_to_carry_on_after_a_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def stopped(base: Settings, **kwargs: Any) -> dict[str, Any]:
        raise DeployStopped("create the keeper with one release key failed: RpcError: timeout")

    monkeypatch.setattr(deploy_module, "deploy", stopped)
    monkeypatch.setenv("HOLDUP_CREDENTIALS", str(tmp_path / "credentials.json"))
    assert cli.main(["deploy", "--network", "testnet", "--profile", "hosted", "--root", "me.testnet"]) == 1
    err = capsys.readouterr().err
    assert "create the keeper with one release key failed" in err and "run the same command again" in err
    assert "Traceback" not in err
