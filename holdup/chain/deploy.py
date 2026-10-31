"""Deploy HoldUp to the NEAR sandbox or testnet. Run by an operator, never by tests.

Both contracts initialise only when called by their own account (``new`` is
private), so each gets a temporary key that is deleted at the end. In order:

1. ``husd.<root>``: the root creates and funds it with a temporary key; that
   key deploys ``res/demo_token.wasm`` and calls ``new`` with the root as owner,
   minting the demo supply to the treasury.
2. ``vault.<root>``: the same, with ``res/holdvault.wasm`` and the profile's
   configuration.
3. The root registers the vault on each token (``storage_deposit``).
4. ``oracle.<root>`` and ``keeper.<root>``: created by the root, each with exactly
   one function-call key on the vault (``extend_hold`` / ``release``, no allowance cap).
5. The payees of the training samples (and, with the demo profile, of the
   scripted story) as keyless sub-accounts of the root.
6. The token's and the vault's temporary keys delete themselves; the vault is
   then checked to have no keys left.

Every amount comes from :mod:`holdup.chain.budget`: each account gets what it
needs plus a stated margin, and :func:`deploy` refuses to start, printing the
shortfall, when the root holds less than the whole budget, so a deployment
cannot run out of NEAR halfway.

Credentials go to ``.holdup/credentials.json`` (git-ignored, mode 600).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from near import (
    AsyncNear,
    Ed25519KeyPair,
    KeyPairSigner,
    generate_key,
    is_valid_account_id,
    parse_key,
)
from near.actions import (
    add_full_access_key,
    add_function_call_key,
    create_account,
    delete_key,
    deploy_contract,
    function_call,
    transfer,
)
from near.errors import NearError
from near.keys import load_credentials
from near.testing import sandbox_signer
from near.units import Amount, Gas

from ..config import OFFLINE_SECRET, RPC_URLS, Settings
from ..models import VaultConfig
from .budget import (
    ACCOUNT_RECORD_BYTES,
    KEEPER_CALL_GAS,
    KEEPER_CONCURRENT_CALLS,
    ORACLE_CALL_GAS,
    ORACLE_CONCURRENT_CALLS,
    ROLE_CALLS,
    TOKEN_MARGIN,
    TOKEN_STATE_BYTES,
    TREASURY_FLOAT,
    VAULT_MARGIN,
    VAULT_STATE_BYTES,
    DeployBudget,
    available_balance,
    contract_account_funding,
    deploy_budget,
    function_call_key_bytes,
    largest_extension_args,
    near_text,
    role_account_funding,
)
from .keys import KEEPER_METHODS, NAME_CHARS, ORACLE_METHODS
from .profiles import PROFILE_PAYEES, PROFILES, config_input, vault_config
from .rpc import make_client

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
RES_DIR = REPO_ROOT / "res"
DEFAULT_CREDENTIALS = Path(".holdup/credentials.json")

HUSD_SUPPLY = 1_000_000 * 10**6  # 1,000,000 HUSD (6 decimals) minted to the treasury
STORAGE_REGISTRATION_NEAR = "0.01"  # refunded above the token's minimum (0.00125 NEAR)
# `new` reads its arguments and writes a few hundred bytes of state: about 3 Tgas. The account
# itself buys this gas up front (0.05 NEAR), and what it does not use stays with it as reserve.
INIT_GAS = Gas.tgas(50)


class DeployRefused(RuntimeError):
    """The root account cannot pay for the whole deployment; nothing was sent."""


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
    note: str = ""  # why a transfer is that much
    payee: bool = False  # a keyless payee for the training samples or the story


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
    treasury_float: int = TREASURY_FLOAT


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
    reserve_near: str | None = None,
    keys: DeployKeys | None = None,
) -> DeployPlan:
    """All deployment transactions for ``settings`` (from :func:`deploy_settings`).

    ``reserve_near`` is what the vault keeps above its storage and deploy gas (default 0.25 NEAR).
    """
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; choose one of {sorted(PROFILES)}")
    keys = keys or DeployKeys()
    config = vault_config(settings, profile)
    root, vault, token = settings.root_account, settings.vault_account, settings.husd_token or ""
    oracle, keeper = settings.oracle_accounts[0], settings.keeper_account
    plan = DeployPlan(settings.network, profile, root, vault, oracle, keeper, token, config, keys)
    add = plan.txs.append
    vault_margin = Amount.near(reserve_near) if reserve_near is not None else VAULT_MARGIN

    def self_initialised(
        name: str, account: str, key: Ed25519KeyPair, wasm: bytes, args: dict[str, Any], state_bytes: int, margin: int
    ) -> PlannedTx:
        init = [deploy_contract(wasm), function_call("new", args, gas=INIT_GAS)]
        delete = [delete_key(key.public_key)]
        funding = contract_account_funding(
            account, code_bytes=len(wasm), state_bytes=state_bytes, own_txs=[init, delete], margin=margin
        )
        add(PlannedTx(f"create {name}", "root", account,
                      [create_account(), transfer(Amount.yocto(funding.yocto)), add_full_access_key(key.public_key)],
                      note=funding.note))
        add(PlannedTx(f"deploy and initialise {name}", name, account, init))
        return PlannedTx(f"delete the {name}'s last key", name, account, delete)

    delete_token_key = self_initialised("token", token, keys.token, token_wasm,
                                        {"owner_id": root, "total_supply": str(HUSD_SUPPLY)},
                                        TOKEN_STATE_BYTES, TOKEN_MARGIN)
    delete_vault_key = self_initialised("vault", vault, keys.vault, vault_wasm, {"config": config_input(config)},
                                        VAULT_STATE_BYTES, vault_margin)
    for token_id in config.tokens:
        add(PlannedTx(f"register the vault on {token_id}", "root", token_id, [
            function_call("storage_deposit", {"account_id": vault, "registration_only": True},
                          deposit=Amount.near(STORAGE_REGISTRATION_NEAR)),
        ], note="NEP-145 storage deposit; the token keeps its minimum (0.00125 NEAR) and returns the rest"))
    for label, account, key, methods, gas_per_call, concurrent, args in (
        ("oracle", oracle, keys.oracle, ORACLE_METHODS, ORACLE_CALL_GAS, ORACLE_CONCURRENT_CALLS,
         largest_extension_args()),
        ("keeper", keeper, keys.keeper, KEEPER_METHODS, KEEPER_CALL_GAS, KEEPER_CONCURRENT_CALLS, None),
    ):
        funding = role_account_funding(
            account, method=methods[0], calls=ROLE_CALLS, gas_per_call=gas_per_call, concurrent=concurrent,
            account_bytes=ACCOUNT_RECORD_BYTES + function_call_key_bytes(vault, methods), args=args,
        )
        add(PlannedTx(f"create the {label} with one {'/'.join(methods)} key", "root", account, [
            create_account(),
            transfer(Amount.yocto(funding.yocto)),
            add_function_call_key(key.public_key, vault, methods, allowance=None),
        ], note=funding.note))
    # Keyless accounts of 100 bytes need no balance (NEP-448), so the payees get none.
    for label in PROFILE_PAYEES[profile]:
        add(PlannedTx(f"create payee {label}.{root}", "root", f"{label}.{root}", [create_account()], payee=True))
    add(delete_token_key)
    add(delete_vault_key)
    check_account_ids(plan)
    return plan


# Every user's account is u<10 characters>.<root> (holdup.chain.keys.account_label).
LONGEST_USER_LABEL = "u" + "a" * NAME_CHARS


def check_account_ids(plan: DeployPlan) -> None:
    """Refuse a plan with an account id NEAR would reject, before anything is sent.

    Names are checked whole: an id has at most 64 characters, so a long root can make
    ``northwind-paper.<root>`` or the users' ``u<10 characters>.<root>`` too long.
    """
    names = {tx.receiver for tx in plan.txs} | {plan.root, f"{LONGEST_USER_LABEL}.{plan.root}"}
    bad = sorted(name for name in names if not is_valid_account_id(name))
    if bad:
        raise DeployRefused(
            f"under {plan.root} these account ids would be invalid (more than 64 characters, or not a NEAR name): "
            f"{', '.join(bad)}. Nothing was sent; choose a root account of at most 48 characters."
        )


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


async def root_balance(near: AsyncNear, root: str) -> int:
    """What the root account can spend, in yoctoNEAR."""
    view = await near.account(root)
    return available_balance(int(view.amount), int(view.locked), view.storage_usage)


async def preflight(plan: DeployPlan, near: AsyncNear) -> DeployBudget:
    """Refuse to deploy when the root cannot pay for every step; return the budget otherwise."""
    budget = deploy_budget(plan)
    try:
        available = await root_balance(near, plan.root)
    except NearError as exc:
        raise DeployRefused(f"could not read the balance of {plan.root}: {exc}") from exc
    short = budget.shortfall(available)
    if short:
        hint = (
            f"\nTop it up first: scripts/testnet.sh topup {plan.root} --min {near_text(budget.total, 2)}"
            if plan.network == "testnet" else ""
        )
        raise DeployRefused(
            f"{plan.root} holds {near_text(available, up=False)} NEAR, but this deployment needs "
            f"{near_text(budget.total)} NEAR: {near_text(short)} NEAR short. Nothing was sent.\n"
            f"{budget.describe()}{hint}"
        )
    log.info("%s holds %s NEAR; deploying.\n%s", plan.root, near_text(available, up=False), budget.describe())
    return budget


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
        "deployment": {
            "profile": plan.profile,
            "budget_near": near_text(deploy_budget(plan).total),
            "transactions": [list(t) for t in txs],
        },
    }


def write_credentials(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def plan_from_res(
    settings: Settings, *, profile: str, res_dir: Path = RES_DIR, reserve_near: str | None = None
) -> DeployPlan:
    """The plan for ``settings`` with the contracts built into ``res_dir``."""
    return plan_deploy(
        settings,
        profile=profile,
        vault_wasm=(res_dir / "holdvault.wasm").read_bytes(),
        token_wasm=(res_dir / "demo_token.wasm").read_bytes(),
        reserve_near=reserve_near,
    )


def budget_for(
    base: Settings, *, network: str, profile: str, root: str, res_dir: Path = RES_DIR, reserve_near: str | None = None
) -> DeployBudget:
    """What deploying ``profile`` under ``root`` takes from the root, without touching the network."""
    plan = plan_from_res(deploy_settings(base, network, root), profile=profile, res_dir=res_dir,
                         reserve_near=reserve_near)
    return deploy_budget(plan)


async def deploy(
    base: Settings,
    *,
    network: str,
    profile: str,
    root: str | None = None,
    reserve_near: str | None = None,
    res_dir: Path = RES_DIR,
    credentials_path: Path = DEFAULT_CREDENTIALS,
    near: AsyncNear | None = None,
) -> dict[str, Any]:
    """Deploy everything, write the credentials file and return its contents.

    Raises :class:`DeployRefused` before sending anything when the root holds less than
    the deploy budget.
    """
    if base.secret == OFFLINE_SECRET:  # public; a real deployment gets a fresh secret instead
        base = replace(base, secret="")
    probe = replace(base, network=network, root_account=root or base.root_account)
    root_signer = root_signer_for(probe)
    settings = deploy_settings(base, network, root or root_signer.account_id)
    plan = plan_from_res(settings, profile=profile, res_dir=res_dir, reserve_near=reserve_near)
    client = near or make_client(network if network in ("testnet", "sandbox") else None, settings.rpc_url or None)
    try:
        await preflight(plan, client)
        txs = await execute_plan(plan, client, root_signer)
    finally:
        if near is None:
            await client.aclose()
    data = credentials_for(plan, settings, txs, root_signer)
    write_credentials(credentials_path, data)
    return data
