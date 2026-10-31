"""Deployment checks: code hash, keyless vault, restricted oracle and keeper keys."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import base58
import httpx
import respx

from holdup.chain.fake import SimClock
from holdup.chain.verify import (
    build_report,
    code_hash,
    code_hash_from_listing,
    expected_code_hash,
    verify_deploy,
    verify_fake,
)
from holdup.config import Settings
from holdup.config import offline_settings
from holdup.runtime import offline_vault

VAULT = "vault.holdup.testnet"
ORACLE = "oracle.holdup.testnet"
KEEPER = "keeper.holdup.testnet"
WASM = b"\x00asm\x01\x00\x00\x00holdvault"
SETTINGS = Settings(network="testnet", rpc_url="http://rpc.test", vault_account=VAULT,
                    oracle_accounts=[ORACLE], keeper_account=KEEPER)


def function_key(methods: list[str], receiver: str = VAULT) -> dict[str, Any]:
    permission = {"FunctionCall": {"allowance": None, "receiver_id": receiver, "method_names": methods}}
    return {"public_key": "ed25519:abc", "access_key": {"nonce": 3, "permission": permission}}


FULL_ACCESS = {"public_key": "ed25519:def", "access_key": {"nonce": 1, "permission": "FullAccess"}}
CONFIG = {"oracles": [ORACLE], "version": "1.0.0", "max_hold_ms": "259200000"}


def report(**overrides: Any) -> dict[str, bool]:
    parts: dict[str, Any] = {
        "vault_account": {"code_hash": code_hash(WASM)},
        "vault_keys": {"keys": []},
        "oracle_keys": {ORACLE: {"keys": [function_key(["extend_hold"])]}},
        "keeper_keys": {"keys": [function_key(["release"])]},
        "config_view": CONFIG,
        "expected_hash": code_hash(WASM),
    }
    parts.update(overrides)
    result = build_report(SETTINGS, **parts)
    return {check.name: check.ok for check in result.checks} | {"ok": result.ok}


def test_code_hash_is_base58_of_sha256() -> None:
    assert code_hash(WASM) == base58.b58encode(hashlib.sha256(WASM).digest()).decode()


def test_expected_hash_reads_the_local_build(tmp_path: Path) -> None:
    wasm = tmp_path / "holdvault.wasm"
    wasm.write_bytes(WASM)
    assert expected_code_hash(wasm) == code_hash(WASM)
    assert expected_code_hash(tmp_path / "missing.wasm") is None


def test_code_hash_listing_in_either_column_order() -> None:
    digest = code_hash(WASM)
    assert code_hash_from_listing(f"{digest}  holdvault.wasm\nother  demo_token.wasm") == digest
    assert code_hash_from_listing(f"demo_token.wasm abc\nholdvault.wasm: {digest}") == digest
    assert code_hash_from_listing("holdvault.wasm not-a-hash") is None


def test_a_correct_deployment_passes() -> None:
    assert report()["ok"] is True


def test_each_weakness_is_caught() -> None:
    assert report(vault_keys={"keys": [FULL_ACCESS]})["vault_has_no_keys"] is False
    assert report(vault_account={"code_hash": code_hash(b"other")})["code_hash"] is False
    assert report(expected_hash=None)["code_hash"] is False
    assert report(oracle_keys={ORACLE: {"keys": [FULL_ACCESS]}})["oracle_key"] is False
    two_keys = {"keys": [function_key(["extend_hold"]), function_key(["extend_hold"])]}
    assert report(oracle_keys={ORACLE: two_keys})["oracle_key"] is False
    assert report(oracle_keys={ORACLE: {"keys": [function_key(["extend_hold", "release"])]}})["oracle_key"] is False
    assert report(oracle_keys={ORACLE: {"keys": [function_key(["extend_hold"], "elsewhere.testnet")]}})["oracle_key"] is False
    assert report(keeper_keys={"keys": [function_key([])]})["keeper_key"] is False  # empty list means any method
    assert report(config_view={"oracles": ["mallory.testnet"]})["config"] is False
    assert report(vault_account=None, vault_keys=None)["ok"] is False


def test_the_offline_sample_is_marked_simulated() -> None:
    settings = offline_settings()
    result = verify_fake(offline_vault(settings, SimClock()), settings)
    assert result.ok and result.simulated
    assert result.to_dict()["simulated"] is True


async def test_live_verification_uses_read_only_rpc_calls(tmp_path: Path) -> None:
    wasm = tmp_path / "holdvault.wasm"
    wasm.write_bytes(WASM)
    keys = {VAULT: [], ORACLE: [function_key(["extend_hold"])], KEEPER: [function_key(["release"])]}
    methods: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        params = body["params"]
        methods.append(body["method"] + ":" + params.get("request_type", ""))
        kind = params.get("request_type")
        if kind == "view_account":
            result: Any = {"amount": "1", "locked": "0", "code_hash": code_hash(WASM), "storage_usage": 1,
                           "block_height": 1, "block_hash": "x"}
        elif kind == "view_access_key_list":
            result = {"keys": keys[params["account_id"]], "block_height": 1, "block_hash": "x"}
        else:
            assert params["method_name"] == "get_config" and base64.b64decode(params["args_base64"]) == b"{}"
            result = {"result": list(json.dumps(CONFIG).encode()), "logs": [], "block_height": 1, "block_hash": "x"}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    with respx.mock(assert_all_called=False) as router:
        router.post("http://rpc.test").mock(side_effect=handle)
        result = await verify_deploy(SETTINGS, wasm_path=wasm)
    assert result.ok and not result.simulated
    assert not any(m.startswith("send_tx") for m in methods)
