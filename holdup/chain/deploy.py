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

Credentials go to ``.holdup/credentials.json`` (git-ignored, mode 600). The
deployment's fresh keys go there before its first transaction, under
``unfinished_deployment`` together with the transactions sent so far, so a
deployment that stops halfway (a network error, an interrupted command) is
finished by running the same command again: it checks on chain which steps are
done and carries on from the first one that is not. The token's and the vault's
temporary keys leave the file once they are deleted. Whatever else the file
holds (the faucet funding account of ``scripts/testnet_tools.py bootstrap``,
for one) is kept.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from near import (
    AccountNotFoundError,
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
from near.errors import AccessKeyNotFoundError, NearError
from near.keys import load_credentials
from near.testing import sandbox_signer
from near.units import Amount, Gas
from near.wire import Action

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
# near-kit returns from a transaction once it ran (optimistic finality), but reads the nonce of a
# key it has not used yet at final finality, a few blocks of about a second later. The token's and
# the vault's keys are first used right after the transaction that added them.
NEW_KEY_WAIT_S = 60.0
NEW_KEY_POLL_S = 0.5
UNFINISHED = "unfinished_deployment"  # in the credentials file until the deployment is complete
NO_CODE_HASH = "11111111111111111111111111111111"  # view_account's code_hash of an account without code


class DeployRefused(RuntimeError):
    """The deployment cannot go ahead (the root cannot pay, an account is in the way); nothing was sent."""


class DeployStopped(RuntimeError):
    """A transaction failed halfway through; running the deployment again carries on from it."""


KEY_ROLES = ("vault", "token", "oracle", "keeper")


@dataclass
class DeployKeys:
    """Fresh keys for one deployment. The vault and token keys are deleted at the end."""

    vault: Ed25519KeyPair = field(default_factory=generate_key)
    token: Ed25519KeyPair = field(default_factory=generate_key)
    oracle: Ed25519KeyPair = field(default_factory=generate_key)
    keeper: Ed25519KeyPair = field(default_factory=generate_key)

    def secret_keys(self) -> dict[str, str]:
        return {name: getattr(self, name).secret_key for name in KEY_ROLES}

    @classmethod
    def from_secret_keys(cls, keys: Mapping[str, str]) -> DeployKeys:
        return cls(**{name: parse_key(keys[name]) for name in KEY_ROLES})


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


async def send_with_new_key(near: AsyncNear, receiver: str, actions: list[Any], signer: KeyPairSigner) -> Any:
    """Send a transaction signed with a key that a transaction moments ago added.

    Until that key is final, near-kit's nonce read raises AccessKeyNotFoundError. The read
    comes before anything is signed or sent, so trying again is safe.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + NEW_KEY_WAIT_S
    while True:
        try:
            return await near.send_transaction(receiver, actions, signer=signer)
        except AccessKeyNotFoundError:
            if loop.time() >= deadline:
                raise
            log.info("the new key of %s is not final yet; trying again in %g s", signer.account_id, NEW_KEY_POLL_S)
            await asyncio.sleep(NEW_KEY_POLL_S)


async def execute_plan(
    plan: DeployPlan,
    near: AsyncNear,
    root_signer: KeyPairSigner,
    *,
    start: int = 0,
    sent: Callable[[str, str], None] | None = None,
) -> list[tuple[str, str]]:
    """Send the planned transactions from ``start`` on, in order; returns (label, tx hash) pairs.

    ``sent`` hears of each transaction as soon as it ran, so that the progress can be saved.
    """
    temporary = {
        "vault": KeyPairSigner(account_id=plan.vault_id, key_pair=plan.keys.vault),
        "token": KeyPairSigner(account_id=plan.token_id, key_pair=plan.keys.token),
    }
    done: list[tuple[str, str]] = []
    for tx in plan.txs[start:]:
        try:
            if tx.signer == "root":
                result = await near.send_transaction(tx.receiver, tx.actions, signer=root_signer)
            else:
                result = await send_with_new_key(near, tx.receiver, tx.actions, temporary[tx.signer])
        except NearError as exc:
            raise DeployStopped(f"{tx.label} failed: {type(exc).__name__}: {exc}") from exc
        done.append((tx.label, result.transaction_hash or ""))
        if sent is not None:
            sent(tx.label, result.transaction_hash or "")
    remaining = await near.access_keys(plan.vault_id)
    if remaining:
        raise RuntimeError(f"{plan.vault_id} still has {len(remaining)} access key(s)")
    return done


def _own_key(plan: DeployPlan, account_id: str) -> str | None:
    """The public key this deployment gives ``account_id``; None for a keyless payee."""
    pairs = {plan.token_id: plan.keys.token, plan.vault_id: plan.keys.vault,
             plan.oracle_id: plan.keys.oracle, plan.keeper_id: plan.keys.keeper}
    pair = pairs.get(account_id)
    return str(pair.public_key) if pair is not None else None


async def _key_set(near: AsyncNear, account_id: str) -> set[str]:
    return {entry.public_key for entry in await near.access_keys(account_id)}


async def step_done(near: AsyncNear, plan: DeployPlan, tx: PlannedTx) -> bool:
    """Whether ``tx`` is on chain already, assuming every step before it is.

    Raises :class:`DeployRefused` for an account that exists without this deployment's key:
    an earlier attempt whose keys were not saved created it, so this one cannot finish it.
    """
    action = tx.actions[0]
    own_key = _own_key(plan, tx.receiver)
    if isinstance(action, Action.CreateAccount):
        try:
            view = await near.account(tx.receiver)
        except AccountNotFoundError:
            return False
        if own_key is None:
            return True
        keys = await _key_set(near, tx.receiver)
        if own_key in keys or (not keys and view.code_hash != NO_CODE_HASH):
            return True  # ours; a contract's temporary key is deleted at the very end
        raise DeployRefused(
            f"{tx.receiver} already exists, but without this deployment's key: an earlier attempt "
            "whose keys were not saved created it, and this deployment cannot finish it. Nothing was "
            "sent. Deploy under a new root account (bash scripts/server.sh testnet --force-new does that)."
        )
    if isinstance(action, Action.DeployContract):
        return (await near.account(tx.receiver)).code_hash != NO_CODE_HASH
    if isinstance(action, Action.DeleteKey):
        return own_key not in await _key_set(near, tx.receiver)
    if isinstance(action, Action.FunctionCall) and action.method_name == "storage_deposit":
        return await near.view(tx.receiver, "storage_balance_of", {"account_id": plan.vault_id}) is not None
    raise ValueError(f"no way to tell whether {tx.label!r} is done")


async def steps_done(near: AsyncNear, plan: DeployPlan) -> int:
    """How many of the plan's transactions an earlier run already made.

    They run one after another, so they are the first ones of the plan. Each is checked by its
    effect on chain rather than by the saved list, which misses a transaction that was sent
    just before the run stopped.
    """
    for index, tx in enumerate(plan.txs):
        if not await step_done(near, plan, tx):
            return index
    return len(plan.txs)


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
    """Replace ``path`` with ``data`` in one step, private (mode 600) from the start."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_credentials(path: Path) -> dict[str, Any]:
    """What the credentials file holds; empty when there is none yet."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise DeployRefused(
            f"{path} is not a JSON object; move it somewhere safe (it may hold keys). Nothing was sent."
        )
    return data


def recorded_hashes(stored: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(label, tx hash) of each transaction the unfinished deployment in ``stored`` saved as sent."""
    record = stored.get(UNFINISHED)
    entries = record.get("transactions") if isinstance(record, dict) else None
    return [(str(entry[0]), str(entry[1])) for entry in entries or [] if isinstance(entry, list) and len(entry) == 2]


def recorded_steps(stored: Mapping[str, Any], *, network: str, profile: str, root: str) -> list[str]:
    """Labels of the transactions an unfinished deployment of ``profile`` under ``root`` saved as sent."""
    record = stored.get(UNFINISHED)
    if not isinstance(record, dict) or _identity(record) != (network, profile, root):
        return []
    return [label for label, _ in recorded_hashes(stored)]


def _identity(record: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    return record.get("network"), record.get("profile"), record.get("root_account")


def unfinished_keys(
    stored: Mapping[str, Any], *, network: str, profile: str, root: str, path: Path
) -> DeployKeys | None:
    """The keys of the deployment an earlier run left unfinished in ``stored``; None if there is none.

    Refuses when the file belongs to another root account or holds an unfinished deployment of
    something else: going on would replace keys that are still needed.
    """
    owner = stored.get("root_account")
    if owner and owner != root and stored.get("treasury_private_key"):
        raise DeployRefused(
            f"{path} holds the key of another root account, {owner}; deploying under {root} would replace it. "
            "Choose another file with HOLDUP_CREDENTIALS, or move this one away. Nothing was sent."
        )
    record = stored.get(UNFINISHED)
    if record is None:
        return None
    if not isinstance(record, dict):
        raise DeployRefused(f"{UNFINISHED} in {path} is not readable; move the file somewhere safe. Nothing was sent.")
    if _identity(record) != (network, profile, root):
        other_network, other_profile, other_root = _identity(record)
        raise DeployRefused(
            f"{path} holds an unfinished {other_profile} deployment under {other_root} on {other_network}. "
            f"Finish it first (holdup deploy --network {other_network} --profile {other_profile} --root {other_root}), "
            "or move the file away. Nothing was sent."
        )
    try:
        return DeployKeys.from_secret_keys(record["keys"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DeployRefused(f"the keys of the unfinished deployment in {path} are not readable: {exc}") from exc


@dataclass
class Progress:
    """An unfinished deployment as the credentials file keeps it: its keys and what was sent."""

    path: Path
    stored: dict[str, Any]  # the whole file; everything else in it is kept
    plan: DeployPlan
    transactions: list[tuple[str, str]]

    def save(self) -> None:
        self.stored[UNFINISHED] = {
            "network": self.plan.network,
            "profile": self.plan.profile,
            "root_account": self.plan.root,
            "keys": self.plan.keys.secret_keys(),
            "transactions": [list(t) for t in self.transactions],
        }
        write_credentials(self.path, self.stored)

    def add(self, label: str, tx_hash: str) -> None:
        self.transactions.append((label, tx_hash))
        self.save()

    def finish(self, credentials: dict[str, Any]) -> dict[str, Any]:
        """Write the complete deployment's credentials and return the file; the temporary keys leave it."""
        data = {key: value for key, value in self.stored.items() if key != UNFINISHED}
        data.update(credentials)
        write_credentials(self.path, data)
        return data


def plan_from_res(
    settings: Settings,
    *,
    profile: str,
    res_dir: Path = RES_DIR,
    reserve_near: str | None = None,
    keys: DeployKeys | None = None,
) -> DeployPlan:
    """The plan for ``settings`` with the contracts built into ``res_dir``."""
    return plan_deploy(
        settings,
        profile=profile,
        vault_wasm=(res_dir / "holdvault.wasm").read_bytes(),
        token_wasm=(res_dir / "demo_token.wasm").read_bytes(),
        reserve_near=reserve_near,
        keys=keys,
    )


def budget_for(
    base: Settings,
    *,
    network: str,
    profile: str,
    root: str,
    res_dir: Path = RES_DIR,
    reserve_near: str | None = None,
    done: Collection[str] = (),
) -> DeployBudget:
    """What deploying ``profile`` under ``root`` takes from the root, without touching the network.

    ``done`` names transactions an unfinished deployment already sent; they are left out.
    """
    plan = plan_from_res(deploy_settings(base, network, root), profile=profile, res_dir=res_dir,
                         reserve_near=reserve_near)
    return deploy_budget(replace(plan, txs=[tx for tx in plan.txs if tx.label not in done]))


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

    A deployment an earlier run left unfinished in ``credentials_path`` is carried on from its
    first step that is not on chain yet, with the keys it saved. Raises :class:`DeployRefused`
    before sending anything when the root holds less than what is left to do, or when an
    account under the root is in the way. Raises :class:`DeployStopped` when a transaction
    fails halfway; what was sent is saved, and the same call carries on from there.
    """
    if base.secret == OFFLINE_SECRET:  # public; a real deployment gets a fresh secret instead
        base = replace(base, secret="")
    probe = replace(base, network=network, root_account=root or base.root_account)
    root_signer = root_signer_for(probe)
    settings = deploy_settings(base, network, root or root_signer.account_id)
    stored = read_credentials(credentials_path)
    keys = unfinished_keys(stored, network=network, profile=profile, root=settings.root_account,
                           path=credentials_path)
    plan = plan_from_res(settings, profile=profile, res_dir=res_dir, reserve_near=reserve_near, keys=keys)
    saved = dict(recorded_hashes(stored)) if keys is not None else {}
    client = near or make_client(network if network in ("testnet", "sandbox") else None, settings.rpc_url or None)
    try:
        try:
            start = await steps_done(client, plan)
        except NearError as exc:
            raise DeployRefused(f"could not read what is on chain under {plan.root}: {exc}. Nothing was sent.") from exc
        if start:
            log.info("%d of the %d transactions were made by an earlier run; carrying on from there.",
                     start, len(plan.txs))
        if start < len(plan.txs):
            await preflight(replace(plan, txs=plan.txs[start:]), client)
        earlier = [(tx.label, saved.get(tx.label, "")) for tx in plan.txs[:start]]
        progress = Progress(credentials_path, stored, plan, list(earlier))
        progress.save()  # the keys are on disk before the first transaction that uses them
        txs = earlier + await execute_plan(plan, client, root_signer, start=start, sent=progress.add)
    finally:
        if near is None:
            await client.aclose()
    return progress.finish(credentials_for(plan, settings, txs, root_signer))
