"""Deploy HoldUp to the NEAR sandbox or testnet. Run by an operator, never by tests.

Both contracts initialise only when called by their own account (``new`` is
private), so each gets a temporary key that is deleted at the end. In order:

1. ``husd.<root>``: the root creates and funds it with a temporary key; that
   key deploys ``res/demo_token.wasm`` and calls ``new`` with the root as owner,
   minting the demo supply to the treasury.
2. ``vault.<root>``: the same, with the storage reserve, ``res/holdvault.wasm``
   and the profile's configuration.
3. The root registers the vault on each token (``storage_deposit``).
4. ``oracle.<root>`` and ``keeper.<root>``: created by the root, each with exactly
   one function-call key on the vault (``extend_hold`` / ``release``, no allowance cap).
5. With the demo profile, the story's payees as keyless sub-accounts of the root.
6. The token's and the vault's temporary keys delete themselves; the vault is
   then checked to have no keys left.

Credentials go to ``.holdup/credentials.json`` (git-ignored, mode 600).
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from near import AsyncNear, Ed25519KeyPair, KeyPairSigner, generate_key, parse_key
from near.actions import (
    add_full_access_key,
    add_function_call_key,
    create_account,
    delete_key,
    deploy_contract,
    function_call,
    transfer,
)
from near.keys import load_credentials
from near.testing import sandbox_signer
from near.units import Amount, Gas

from ..config import OFFLINE_SECRET, RPC_URLS, Settings
from ..models import VaultConfig
from .keys import KEEPER_METHODS, ORACLE_METHODS
from .profiles import DEMO_PAYEES, PROFILES, config_input, vault_config

REPO_ROOT = Path(__file__).resolve().parents[2]
RES_DIR = REPO_ROOT / "res"
DEFAULT_CREDENTIALS = Path(".holdup/credentials.json")

HUSD_SUPPLY = 1_000_000 * 10**6  # 1,000,000 HUSD (6 decimals) minted to the treasury
TOKEN_ACCOUNT_NEAR = "3"
ROLE_ACCOUNT_NEAR = "1"
DEMO_PAYEE_NEAR = "0.1"
STORAGE_REGISTRATION_NEAR = "0.01"  # refunded above the token's minimum
INIT_GAS = Gas.tgas(100)


@dataclass
class DeployKeys:
    """Fresh keys for one deployment. The vault and token keys are deleted at the end."""

    vault: Ed25519KeyPair = field(default_factory=generate_key)
    token: Ed25519KeyPair = field(default_factory=generate_key)
    oracle: Ed25519KeyPair = field(default_factory=generate_key)
    keeper: Ed25519KeyPair = field(default_factory=generate_key)


@dataclass
class PlannedTx:
    label: str
    signer: str  # "root", or "vault" / "token" for that account's temporary key
    receiver: str
    actions: list[Any]


@dataclass
class DeployPlan:
    network: str
    profile: str
    root: str
    vault_id: str
    oracle_id: str
    keeper_id: str
    token_id: str
    config: VaultConfig
    keys: DeployKeys
    txs: list[PlannedTx] = field(default_factory=list)


def deploy_settings(base: Settings, network: str, root: str) -> Settings:
    """Settings naming the accounts a deployment under ``root`` creates."""
    usdc = base.usdc_token if network == "testnet" else None
    rpc_url = base.rpc_url if base.network == network and base.rpc_url else RPC_URLS.get(network, "")
    if network == "sandbox" and os.environ.get("NEAR_SANDBOX_URL"):
        rpc_url = os.environ["NEAR_SANDBOX_URL"]
    return replace(
        base,
        network=network,
        rpc_url=rpc_url,
        root_account=root,
        vault_account=f"vault.{root}",
        oracle_accounts=[f"oracle.{root}"],
        keeper_account=f"keeper.{root}",
        husd_token=f"husd.{root}",
        usdc_token=usdc,
    )


def plan_deploy(
    settings: Settings,
    *,
    profile: str,
    vault_wasm: bytes,
    token_wasm: bytes,
    reserve_near: str = "5",
    keys: DeployKeys | None = None,
) -> DeployPlan:
    """All deployment transactions for ``settings`` (from :func:`deploy_settings`)."""
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; choose one of {sorted(PROFILES)}")
    keys = keys or DeployKeys()
    config = vault_config(settings, profile)
    root, vault, token = settings.root_account, settings.vault_account, settings.husd_token or ""
    oracle, keeper = settings.oracle_accounts[0], settings.keeper_account
    plan = DeployPlan(settings.network, profile, root, vault, oracle, keeper, token, config, keys)
    add = plan.txs.append

    def self_initialised(name: str, account: str, near: str, key: Ed25519KeyPair, wasm: bytes, args: dict[str, Any]) -> None:
        add(PlannedTx(f"create {name}", "root", account,
                      [create_account(), transfer(Amount.near(near)), add_full_access_key(key.public_key)]))
        add(PlannedTx(f"deploy and initialise {name}", name, account,
                      [deploy_contract(wasm), function_call("new", args, gas=INIT_GAS)]))

    self_initialised("token", token, TOKEN_ACCOUNT_NEAR, keys.token, token_wasm,
                     {"owner_id": root, "total_supply": str(HUSD_SUPPLY)})
    self_initialised("vault", vault, reserve_near, keys.vault, vault_wasm, {"config": config_input(config)})
    for token_id in config.tokens:
        add(PlannedTx(f"register the vault on {token_id}", "root", token_id, [
            function_call("storage_deposit", {"account_id": vault, "registration_only": True},
                          deposit=Amount.near(STORAGE_REGISTRATION_NEAR)),
        ]))
    for label, account, key, methods in (
        ("oracle", oracle, keys.oracle, ORACLE_METHODS),
        ("keeper", keeper, keys.keeper, KEEPER_METHODS),
    ):
        add(PlannedTx(f"create the {label} with one {'/'.join(methods)} key", "root", account, [
            create_account(),
            transfer(Amount.near(ROLE_ACCOUNT_NEAR)),
            add_function_call_key(key.public_key, vault, methods, allowance=None),
        ]))
    if profile == "demo":
        for label in DEMO_PAYEES:
            add(PlannedTx(f"create demo payee {label}.{root}", "root", f"{label}.{root}",
                          [create_account(), transfer(Amount.near(DEMO_PAYEE_NEAR))]))
    add(PlannedTx("delete the token's last key", "token", token, [delete_key(keys.token.public_key)]))
    add(PlannedTx("delete the vault's last key", "vault", vault, [delete_key(keys.vault.public_key)]))
    return plan


async def execute_plan(
    plan: DeployPlan, near: AsyncNear, root_signer: KeyPairSigner
) -> list[tuple[str, str]]:
    """Send the planned transactions in order; returns (label, tx hash) pairs."""
    signers = {
        "root": root_signer,
        "vault": KeyPairSigner(account_id=plan.vault_id, key_pair=plan.keys.vault),
        "token": KeyPairSigner(account_id=plan.token_id, key_pair=plan.keys.token),
    }
    done: list[tuple[str, str]] = []
    for tx in plan.txs:
        result = await near.send_transaction(tx.receiver, tx.actions, signer=signers[tx.signer])
        done.append((tx.label, result.transaction_hash or ""))
    remaining = await near.access_keys(plan.vault_id)
    if remaining:
        raise RuntimeError(f"{plan.vault_id} still has {len(remaining)} access key(s)")
    return done


def root_signer_for(settings: Settings) -> KeyPairSigner:
    """The deploying root account: configured key, NEAR CLI credentials, or the sandbox root."""
    if settings.treasury_private_key:
        return KeyPairSigner(account_id=settings.root_account, key_pair=parse_key(settings.treasury_private_key))
    if settings.network == "sandbox":
        return sandbox_signer()
    return load_credentials(settings.root_account, settings.network)


def credentials_for(
    plan: DeployPlan, settings: Settings, txs: list[tuple[str, str]], root_signer: KeyPairSigner
) -> dict[str, Any]:
    return {
        "network": plan.network,
        "rpc_url": settings.rpc_url,
        "root_account": plan.root,
        "vault_account": plan.vault_id,
        "oracle_accounts": [plan.oracle_id],
        "keeper_account": plan.keeper_id,
        "husd_token": plan.token_id,
        "usdc_token": settings.usdc_token,
        "oracle_private_key": plan.keys.oracle.secret_key,
        "keeper_private_key": plan.keys.keeper.secret_key,
        "treasury_private_key": root_signer.key_pair.secret_key,
        "secret": settings.secret or secrets.token_urlsafe(32),
        "deployment": {"profile": plan.profile, "transactions": [list(t) for t in txs]},
    }


def write_credentials(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


async def deploy(
    base: Settings,
    *,
    network: str,
    profile: str,
    root: str | None = None,
    reserve_near: str = "5",
    res_dir: Path = RES_DIR,
    credentials_path: Path = DEFAULT_CREDENTIALS,
) -> dict[str, Any]:
    """Deploy everything, write the credentials file and return its contents."""
    if base.secret == OFFLINE_SECRET:  # public; a real deployment gets a fresh secret instead
        base = replace(base, secret="")
    probe = replace(base, network=network, root_account=root or base.root_account)
    root_signer = root_signer_for(probe)
    settings = deploy_settings(base, network, root or root_signer.account_id)
    plan = plan_deploy(
        settings,
        profile=profile,
        vault_wasm=(res_dir / "holdvault.wasm").read_bytes(),
        token_wasm=(res_dir / "demo_token.wasm").read_bytes(),
        reserve_near=reserve_near,
    )
    near = AsyncNear(network if network in ("testnet", "sandbox") else None, rpc_url=settings.rpc_url or None)
    try:
        txs = await execute_plan(plan, near, root_signer)
    finally:
        await near.aclose()
    data = credentials_for(plan, settings, txs, root_signer)
    write_credentials(credentials_path, data)
    return data
