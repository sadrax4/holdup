"""HoldVault and the HUSD demo token on a running NEAR sandbox.

This file never starts a sandbox itself. Start one, then run:

    NEAR_SANDBOX_URL=http://127.0.0.1:3030 .venv/bin/pytest -m sandbox tests/test_contract_sandbox.py

The node must run protocol 85 or newer (nearcore 2.13+). The contracts come from res/
(scripts/build_contracts.sh). The root account defaults to the nearprotocol/sandbox image's
"sandbox" account and its deterministic test key; set NEAR_SANDBOX_ROOT_ACCOUNT and
NEAR_SANDBOX_ROOT_KEY (an "ed25519:..." secret key) for other setups. Accounts are created
under the root with a random suffix, so the tests can run repeatedly against one sandbox.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

import near
from near.testing import sandbox_signer

pytestmark = pytest.mark.sandbox

RES = Path(__file__).resolve().parents[1] / "res"
MIN_PROTOCOL = 85  # gas-key host functions imported by near-sdk 5.29

NEAR = 10**24
HUSD = 10**6
REGISTRATION_FEE = 10 * 10**21  # 0.01 NEAR
HOLD_MS = 8_000
DELAY_MS = 5_000
MAX_HOLD_MS = 120_000
CALL_GAS = "100 Tgas"


@dataclass
class Actor:
    account_id: str
    client: near.Near

    def call(self, contract: str, method: str, args: dict[str, Any], **kwargs: Any) -> near.TransactionResult:
        kwargs.setdefault("gas", CALL_GAS)
        return self.client.call(contract, method, args, **kwargs)


@dataclass
class World:
    root: Actor
    vault: str
    token: str
    oracle: Actor
    keeper: Actor
    alice: Actor
    bob: Actor
    carol: Actor
    mallory: Actor
    shop: str  # an existing account outside the vault
    ghost: str  # an account that does not exist

    def view(self, method: str, args: dict[str, Any] | None = None) -> Any:
        return self.root.client.view(self.vault, method, args or {})

    def hold(self, hold_id: int) -> dict[str, Any]:
        return self.view("get_hold", {"hold_id": str(hold_id)})

    def near_balance(self, account_id: str) -> int:
        return int(self.view("get_balance", {"account_id": account_id})["near"])

    def husd_balance(self, account_id: str) -> int:
        return int(self.view("get_balance", {"account_id": account_id})["tokens"][self.token])

    def send(self, sender: Actor, payee: str, asset: str, amount: int) -> dict[str, Any]:
        result = sender.call(self.vault, "send", {"payee": payee, "asset": asset, "amount": str(amount)})
        return result.json_value()["hold"]

    def release(self, hold_id: int) -> near.TransactionResult | None:
        """Releases a due hold. Returns None when a receipt of the payout failed on purpose
        (near-kit raises for any failed receipt; the vault settles the failure itself)."""
        wait_until_due(self.root.client, int(self.hold(hold_id)["release_at_ms"]))
        try:
            return self.keeper.call(self.vault, "release", {"hold_id": str(hold_id)})
        except near.ContractPanicError as error:
            assert not re.search(r"\bE_[A-Z_]+:", error.panic), f"the vault refused: {error.panic}"
            return None


def events(result: near.TransactionResult) -> list[tuple[str, dict[str, Any]]]:
    found = []
    for log in result.logs:
        if log.startswith("EVENT_JSON:"):
            event = json.loads(log.removeprefix("EVENT_JSON:"))
            assert (event["standard"], event["version"]) == ("holdup", "1.0.0")
            found.append((event["event"], event["data"][0]))
    return found


def chain_now_ms(client: near.Near) -> int:
    return int(client.rpc("block", {"finality": "final"})["header"]["timestamp"]) // 1_000_000


def wait_until_due(client: near.Near, release_at_ms: int, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while chain_now_ms(client) <= release_at_ms:
        if time.monotonic() > deadline:
            raise TimeoutError(f"the sandbox clock did not pass {release_at_ms}")
        time.sleep(0.25)


def published_code_hash(file_name: str) -> str:
    for line in (RES / "CODE_HASHES").read_text().splitlines():
        code_hash, name = line.split()
        if name == file_name:
            return code_hash
    raise KeyError(file_name)


def create_account(root: Actor, account_id: str, balance: str, code: bytes | None = None,
                   init: tuple[str, dict[str, Any]] | None = None) -> Actor:
    """Creates a sub-account of the root with a fresh full-access key, optionally deploying and
    initializing a contract in the same transaction (as the production deploy does)."""
    key = near.generate_key()
    actions: list[Any] = [
        near.create_account(),
        near.transfer(balance),
        near.add_full_access_key(key.public_key),
    ]
    if code is not None:
        actions.append(near.deploy_contract(code))
    if init is not None:
        method, args = init
        actions.append(near.function_call(method, args, gas=CALL_GAS))
    root.client.send_transaction(account_id, actions)
    signer = near.KeyPairSigner(account_id=account_id, key_pair=key)
    return Actor(account_id, root.client.with_signer(signer))


@pytest.fixture(scope="module")
def root() -> Iterator[Actor]:
    url = os.environ.get("NEAR_SANDBOX_URL")
    if not url:
        pytest.skip("set NEAR_SANDBOX_URL to a running NEAR sandbox")
    try:
        status = httpx.get(f"{url.rstrip('/')}/status", timeout=5.0).json()
    except (httpx.HTTPError, ValueError) as error:
        pytest.skip(f"no NEAR node answers at {url}: {error}")
    protocol = int(status.get("protocol_version", 0))
    if protocol < MIN_PROTOCOL:
        pytest.fail(f"res/holdvault.wasm needs protocol {MIN_PROTOCOL}+, the sandbox runs {protocol}")
    account_id = os.environ.get("NEAR_SANDBOX_ROOT_ACCOUNT", "sandbox")
    secret = os.environ.get("NEAR_SANDBOX_ROOT_KEY")
    signer = (
        near.KeyPairSigner(account_id=account_id, key_pair=near.parse_key(secret))
        if secret
        else sandbox_signer(account_id)
    )
    client = near.Near(rpc_url=url, signer=signer)
    yield Actor(account_id, client)
    client.close()


@pytest.fixture(scope="module")
def world(root: Actor) -> World:
    run = secrets.token_hex(3)

    def name(label: str) -> str:
        return f"{label}-{run}.{root.account_id}"

    token = name("husd")
    oracle = create_account(root, name("oracle"), "2 NEAR")
    vault_id = name("vault")
    config = {
        "oracles": [oracle.account_id],
        "tokens": [token],
        "new_payee_hold_ms": str(HOLD_MS),
        "max_hold_ms": str(MAX_HOLD_MS),
        "trust_delay_ms": str(DELAY_MS),
        "guardian_delay_ms": str(DELAY_MS),
        "instant_caps": {"near": str(10 * NEAR), token: str(100 * HUSD)},
        "max_active_holds": 10,
        "registration_fee": str(REGISTRATION_FEE),
    }
    supply = {"owner_id": root.account_id, "total_supply": str(1_000_000 * HUSD)}
    create_account(root, token, "5 NEAR", (RES / "demo_token.wasm").read_bytes(), ("new", supply))
    vault = create_account(root, vault_id, "12 NEAR", (RES / "holdvault.wasm").read_bytes(), ("new", {"config": config}))
    # Like the production deploy: the vault ends up with no access key at all.
    vault.client.send_transaction(vault_id, [near.delete_key(vault.client.signer.public_key)])

    bounds = root.client.view(token, "storage_balance_bounds")
    root.call(token, "storage_deposit", {"account_id": vault_id, "registration_only": True},
              deposit=near.Amount.yocto(int(bounds["min"])))

    users = {label: create_account(root, name(label), "3 NEAR") for label in ("alice", "bob", "carol")}
    for label, near_balance in (("alice", 20 * NEAR), ("bob", 0), ("carol", 0)):
        root.call(vault_id, "deposit_near_for", {"account_id": users[label].account_id},
                  deposit=near.Amount.yocto(REGISTRATION_FEE + near_balance))
    root.call(token, "ft_transfer_call",
              {"receiver_id": vault_id, "amount": str(500 * HUSD),
               "msg": json.dumps({"credit_to": users["alice"].account_id})},
              deposit=near.ONE_YOCTO)
    shop = name("shop")
    root.client.send_transaction(shop, [near.create_account(), near.transfer("1 NEAR")])
    return World(
        root=root,
        vault=vault_id,
        token=token,
        oracle=oracle,
        keeper=create_account(root, name("keeper"), "2 NEAR"),
        alice=users["alice"],
        bob=users["bob"],
        carol=users["carol"],
        mallory=create_account(root, name("mallory"), "2 NEAR"),
        shop=shop,
        ghost=name("ghost"),
    )


def test_vault_is_keyless_and_runs_the_published_code(world: World) -> None:
    client = world.root.client
    assert client.access_keys(world.vault) == []
    assert client.account(world.vault).code_hash == published_code_hash("holdvault.wasm")
    assert client.account(world.token).code_hash == published_code_hash("demo_token.wasm")
    config = world.view("get_config")
    assert config.pop("version") == "1.0.0"
    assert config["oracles"] == [world.oracle.account_id]
    with pytest.raises(near.ContractPanicError, match="already been initialized"):
        world.mallory.call(world.vault, "new", {"config": config | {"oracles": [world.mallory.account_id]}})
    metadata = client.view(world.token, "ft_metadata")
    assert (metadata["symbol"], metadata["decimals"], metadata["name"]) == ("HUSD", 6, "HoldUp Demo Dollar")


def test_onboarding_credited_near_and_tokens(world: World) -> None:
    assert world.view("is_registered", {"account_id": world.alice.account_id})
    assert world.near_balance(world.alice.account_id) == 20 * NEAR
    assert world.husd_balance(world.alice.account_id) == 500 * HUSD
    assert world.view("get_user", {"account_id": world.mallory.account_id}) is None
    # Strangers cannot register other accounts, only deposit for registered ones.
    with pytest.raises(near.ContractPanicError, match="E_NOT_REGISTERED"):
        world.mallory.call(world.vault, "deposit_near_for", {"account_id": world.shop},
                           deposit=near.Amount.yocto(REGISTRATION_FEE))


def test_held_payment_is_braked_by_the_oracle_and_cancelled(world: World) -> None:
    alice, vault = world.alice, world.vault
    before = world.near_balance(alice.account_id)
    result = alice.call(vault, "send", {"payee": world.shop, "asset": "near", "amount": str(NEAR)})
    hold = result.json_value()["hold"]
    assert hold["status"] == "held" and not hold["instant"]
    assert [name for name, _ in events(result)] == ["hold_created"]
    hold_id = hold["id"]

    with pytest.raises(near.ContractPanicError, match="E_NOT_DUE"):
        world.keeper.call(vault, "release", {"hold_id": hold_id})
    evidence = json.dumps({"v": 1, "hold": int(hold_id), "reasons": [{"bit": 1}]}, separators=(",", ":"))
    extend = {"hold_id": hold_id, "release_at_ms": str(int(hold["release_at_ms"]) + 60_000),
              "reason_bits": 1, "evidence": evidence}
    with pytest.raises(near.ContractPanicError, match="E_NOT_ORACLE"):
        world.mallory.call(vault, "extend_hold", extend)
    braked = world.oracle.call(vault, "extend_hold", extend).json_value()
    assert braked["flagged"] and braked["reason_bits"] == 1
    assert braked["evidence_hash"] == hashlib.sha256(evidence.encode()).hexdigest()
    with pytest.raises(near.ContractPanicError, match="E_ONLY_LONGER"):
        world.oracle.call(vault, "extend_hold", extend)
    too_far = extend | {"release_at_ms": str(int(hold["created_ms"]) + MAX_HOLD_MS + 1)}
    with pytest.raises(near.ContractPanicError, match="E_ABOVE_MAX_HOLD"):
        world.oracle.call(vault, "extend_hold", too_far)

    with pytest.raises(near.ContractPanicError, match="E_NOT_SENDER"):
        world.mallory.call(vault, "cancel", {"hold_id": hold_id})
    cancelled = alice.call(vault, "cancel", {"hold_id": hold_id})
    assert cancelled.json_value()["status"] == "cancelled"
    assert world.near_balance(alice.account_id) == before
    stats = world.view("get_payee_stats", {"account_id": world.shop})
    assert stats["cancelled_after_flag"] >= 1


def test_due_near_payment_reaches_an_outside_account(world: World) -> None:
    shop_before = int(world.root.client.account(world.shop).amount)
    hold = world.send(world.alice, world.shop, "near", NEAR // 2)
    result = world.release(int(hold["id"]))
    assert result is not None and result.json_value()["status"] == "released"
    assert int(world.root.client.account(world.shop).amount) == shop_before + NEAR // 2
    assert "hold_released" in [name for name, _ in events(result)]


def test_due_token_payment_registers_the_payee_on_the_token(world: World) -> None:
    hold = world.send(world.alice, world.shop, world.token, 25 * HUSD)
    result = world.release(int(hold["id"]))
    assert result is not None and result.json_value()["status"] == "released"
    assert int(world.root.client.view(world.token, "ft_balance_of", {"account_id": world.shop})) == 25 * HUSD


def test_failed_payout_returns_the_money_to_the_sender(world: World) -> None:
    before = world.near_balance(world.alice.account_id)
    hold = world.send(world.alice, world.ghost, "near", NEAR // 4)
    assert world.release(int(hold["id"])) is None  # the transfer to a missing account fails
    settled = world.hold(int(hold["id"]))
    assert settled["status"] == "failed"
    assert world.near_balance(world.alice.account_id) == before


def test_guardian_cancels_for_the_sender(world: World) -> None:
    alice, carol = world.alice, world.carol
    world.alice.call(world.vault, "set_guardian", {"guardian": carol.account_id})
    hold = world.send(alice, world.shop, "near", NEAR // 10)
    with pytest.raises(near.ContractPanicError, match="E_NOT_GUARDIAN"):
        world.mallory.call(world.vault, "guardian_cancel", {"hold_id": hold["id"]})
    cancelled = carol.call(world.vault, "guardian_cancel", {"hold_id": hold["id"]}).json_value()
    assert (cancelled["status"], cancelled["cancelled_by"]) == ("cancelled", carol.account_id)


def test_trusted_vault_user_is_paid_instantly_inside_the_vault(world: World) -> None:
    alice, bob = world.alice, world.bob
    trusted = alice.call(world.vault, "add_trusted", {"payee": bob.account_id}).json_value()
    wait_until_due(world.root.client, int(trusted["effective_ms"]))
    before = world.near_balance(bob.account_id)
    hold = world.send(alice, bob.account_id, "near", NEAR)
    assert hold["instant"] and hold["status"] == "released"
    assert world.near_balance(bob.account_id) == before + NEAR


def test_callbacks_and_deposits_are_locked_down(world: World) -> None:
    with pytest.raises(near.ContractPanicError, match="Method on_payout is private"):
        world.mallory.call(world.vault, "on_payout", {"hold_id": "0"})
    with pytest.raises(near.ContractPanicError, match="Method send doesn't accept deposit"):
        world.alice.call(world.vault, "send", {"payee": world.shop, "asset": "near", "amount": "1"},
                         deposit=near.ONE_YOCTO)
    with pytest.raises(near.ContractPanicError, match="E_NOT_TOKEN"):
        world.mallory.call(world.vault, "ft_on_transfer",
                           {"sender_id": world.mallory.account_id, "amount": "1", "msg": ""})


def test_vault_stays_solvent(world: World) -> None:
    solvency = world.view("get_solvency")
    available = int(solvency["account_balance"]) + int(solvency["locked"])
    required = sum(int(solvency[key]) for key in ("near_liabilities", "settlement_reserve", "storage_cost"))
    assert available >= required
    account = world.root.client.account(world.vault)
    assert int(solvency["storage_usage"]) == account.storage_usage
