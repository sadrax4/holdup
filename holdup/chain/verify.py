"""Checks anyone can repeat with an RPC endpoint to confirm HoldUp's guarantees.

- The vault runs exactly the published code: its code hash equals base58(sha256(res/holdvault.wasm)).
- The vault account has no access keys, so nobody can upgrade it or move its funds.
- The oracle's only key can call ``extend_hold`` on the vault and nothing else.
- The keeper's only key can call ``release`` on the vault and nothing else.
- The vault's fixed configuration names the expected oracles.

The checks work on raw RPC JSON, so the same code verifies a live deployment
and the in-memory vault behind the offline sample on the web /prove page.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import base58
from near import AsyncNear
from near.errors import NearError

from ..config import Settings, load_settings
from .fake import FakeVault
from .keys import KEEPER_METHODS, ORACLE_METHODS
from .rpc import make_client

REPO_ROOT = Path(__file__).resolve().parents[2]
WASM_PATH = REPO_ROOT / "res" / "holdvault.wasm"
CODE_HASHES_PATH = REPO_ROOT / "res" / "CODE_HASHES"


@dataclass
class Check:
    name: str
    ok: bool
    expected: str
    observed: str
    detail: str = ""


@dataclass
class VerifyReport:
    network: str
    vault_id: str
    checks: list[Check] = field(default_factory=list)
    simulated: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(check.ok for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "network": self.network,
            "vault_id": self.vault_id,
            "simulated": self.simulated,
            "ok": self.ok,
            "checks": [asdict(check) for check in self.checks],
        }


def code_hash(wasm: bytes) -> str:
    """What ``view_account`` reports as ``code_hash``: base58 of the sha256 digest."""
    return base58.b58encode(hashlib.sha256(wasm).digest()).decode()


def expected_code_hash(wasm_path: Path | None = None) -> str | None:
    """Hash of the local build, from the wasm itself or from res/CODE_HASHES."""
    path = wasm_path or WASM_PATH
    if path.exists():
        return code_hash(path.read_bytes())
    if wasm_path is None and CODE_HASHES_PATH.exists():
        return code_hash_from_listing(CODE_HASHES_PATH.read_text())
    return None


def code_hash_from_listing(text: str, name: str = "holdvault") -> str | None:
    """The base58 hash on the line naming ``name``, in either column order."""
    for line in text.splitlines():
        tokens = line.replace(":", " ").split()
        if not any(name in token for token in tokens):
            continue
        for token in tokens:
            if _is_base58_digest(token):
                return token
    return None


def _is_base58_digest(token: str) -> bool:
    try:
        return len(base58.b58decode(token)) == 32
    except ValueError:
        return False


def check_code_hash(account_view: dict[str, Any] | None, expected: str | None) -> Check:
    observed = (account_view or {}).get("code_hash", "")
    if account_view is None:
        return Check("code_hash", False, expected or "?", "account not found")
    if expected is None:
        return Check("code_hash", False, "?", observed, "no local res/holdvault.wasm to compare with")
    return Check("code_hash", observed == expected, expected, observed)


def describe_keys(keys_view: dict[str, Any] | None) -> str:
    keys = (keys_view or {}).get("keys", [])
    if not keys:
        return "no keys"
    parts = []
    for entry in keys:
        permission = entry.get("access_key", {}).get("permission")
        if permission == "FullAccess":
            parts.append("FullAccess")
        elif isinstance(permission, dict) and "FunctionCall" in permission:
            call = permission["FunctionCall"]
            methods = ",".join(call.get("method_names") or []) or "any method"
            allowance = call.get("allowance") or "unlimited"
            parts.append(f"FunctionCall({call.get('receiver_id')}: {methods}; allowance {allowance})")
        else:
            parts.append(str(permission))
    return f"{len(keys)} key{'s' if len(keys) != 1 else ''}: " + "; ".join(parts)


def check_keyless(account_id: str, keys_view: dict[str, Any] | None) -> Check:
    keys = (keys_view or {}).get("keys", [])
    return Check(
        "vault_has_no_keys",
        keys_view is not None and not keys,
        "no access keys",
        describe_keys(keys_view) if keys_view is not None else "account not found",
        f"{account_id} can only change through its own contract methods",
    )


def check_restricted_key(
    name: str,
    account_id: str,
    keys_view: dict[str, Any] | None,
    vault_id: str,
    methods: tuple[str, ...],
) -> Check:
    """Exactly one key: a function-call key on the vault limited to ``methods``."""
    keys = (keys_view or {}).get("keys", [])
    ok = False
    if len(keys) == 1:
        permission = keys[0].get("access_key", {}).get("permission")
        if isinstance(permission, dict) and "FunctionCall" in permission:
            call = permission["FunctionCall"]
            ok = call.get("receiver_id") == vault_id and sorted(call.get("method_names") or []) == sorted(methods)
    expected = f"1 key: FunctionCall({vault_id}: {','.join(methods)})"
    observed = describe_keys(keys_view) if keys_view is not None else "account not found"
    return Check(name, ok, expected, observed, account_id)


def check_config(config_view: dict[str, Any] | None, settings: Settings) -> Check:
    if not config_view:
        return Check("config", False, "initialised vault", "get_config returned nothing")
    oracles = sorted(config_view.get("oracles", []))
    expected = sorted(settings.oracle_accounts)
    return Check(
        "config",
        oracles == expected,
        f"oracles {expected}",
        f"oracles {oracles}, version {config_view.get('version', '?')}, max hold "
        f"{int(config_view.get('max_hold_ms', 0)) // 3_600_000} h",
    )


def build_report(
    settings: Settings,
    *,
    vault_account: dict[str, Any] | None,
    vault_keys: dict[str, Any] | None,
    oracle_keys: dict[str, dict[str, Any] | None],
    keeper_keys: dict[str, Any] | None,
    config_view: dict[str, Any] | None,
    expected_hash: str | None,
    simulated: bool = False,
) -> VerifyReport:
    vault_id = settings.vault_account
    checks = [check_code_hash(vault_account, expected_hash), check_keyless(vault_id, vault_keys)]
    for oracle, keys in oracle_keys.items():
        checks.append(check_restricted_key("oracle_key", oracle, keys, vault_id, ORACLE_METHODS))
    checks.append(
        check_restricted_key("keeper_key", settings.keeper_account, keeper_keys, vault_id, KEEPER_METHODS)
    )
    checks.append(check_config(config_view, settings))
    return VerifyReport(settings.network, vault_id, checks, simulated)


async def _query(near: AsyncNear, request_type: str, account_id: str) -> dict[str, Any] | None:
    try:
        return await near.rpc(
            "query", {"request_type": request_type, "account_id": account_id, "finality": "final"}
        )
    except NearError:
        return None


async def verify_deploy(
    settings: Settings | None = None,
    *,
    near: AsyncNear | None = None,
    wasm_path: Path | None = None,
) -> VerifyReport:
    """Verify a live deployment (read-only RPC calls only)."""
    settings = settings or load_settings()
    client = near or make_client(
        settings.network if settings.network in ("testnet", "sandbox") else None, settings.rpc_url or None
    )
    try:
        vault = settings.vault_account
        try:
            config_view = await client.view(vault, "get_config")
        except NearError:
            config_view = None
        return build_report(
            settings,
            vault_account=await _query(client, "view_account", vault),
            vault_keys=await _query(client, "view_access_key_list", vault),
            oracle_keys={
                oracle: await _query(client, "view_access_key_list", oracle)
                for oracle in settings.oracle_accounts
            },
            keeper_keys=await _query(client, "view_access_key_list", settings.keeper_account),
            config_view=config_view,
            expected_hash=expected_code_hash(wasm_path),
        )
    finally:
        if near is None:
            await client.aclose()


def verify_fake(fake: FakeVault, settings: Settings) -> VerifyReport:
    """The same checks on the in-memory vault: a clearly simulated sample."""
    return build_report(
        settings,
        vault_account=fake.view_account(fake.vault_id),
        vault_keys=fake.view_access_key_list(fake.vault_id),
        oracle_keys={o: fake.view_access_key_list(o) for o in settings.oracle_accounts},
        keeper_keys=fake.view_access_key_list(fake.keeper_id),
        config_view=fake.model.get_config(),
        expected_hash=fake.code_hash,
        simulated=True,
    )
