"""Settings: network defaults, the offline world and Otari modes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from holdup.config import OFFLINE_SECRET, USDC_TESTNET, load_settings, offline_settings
from holdup.models import NEAR


def load(tmp_path: Path, **env: str):  # type: ignore[no-untyped-def]
    return load_settings(env=env, credentials_path=str(tmp_path / "credentials.json"))


def test_offline_is_the_default_and_a_fixed_world(tmp_path: Path) -> None:
    settings = load(tmp_path)
    assert settings == offline_settings(public_base_url="http://127.0.0.1:8000")
    assert settings.network == "offline" and settings.root_account == "holdup.testnet"
    assert settings.vault_account == "vault.holdup.testnet" and settings.husd_token == "husd.holdup.testnet"
    assert settings.secret == OFFLINE_SECRET and settings.database_path == ":memory:"
    assert settings.otari_mode == "replay" and settings.usdc_token is None
    assert settings.demo_funds == {NEAR: 2 * 10**24, "husd.holdup.testnet": 2_000 * 10**6}


def test_offline_ignores_a_deployment_but_keeps_channel_settings(tmp_path: Path) -> None:
    (tmp_path / "credentials.json").write_text(
        json.dumps({"network": "testnet", "root_account": "me.testnet", "vault_account": "vault.me.testnet"})
    )
    settings = load_settings(
        env={"HOLDUP_SECRET": "x" * 32, "PUBLIC_BASE_URL": "https://holdup.example/", "OTARI_MODE": "off"},
        credentials_path=str(tmp_path / "credentials.json"),
        network="offline",
    )
    assert settings.vault_account == "vault.holdup.testnet"
    assert settings.secret == "x" * 32 and settings.public_base_url == "https://holdup.example"
    assert settings.otari_mode == "off"


def test_the_credentials_file_picks_the_network(tmp_path: Path) -> None:
    (tmp_path / "credentials.json").write_text(json.dumps({"network": "testnet", "root_account": "me.testnet"}))
    settings = load_settings(env={}, credentials_path=str(tmp_path / "credentials.json"))
    assert settings.network == "testnet" and settings.vault_account == "vault.me.testnet"
    assert settings.oracle_accounts == ["oracle.me.testnet"] and settings.usdc_token == USDC_TESTNET


def test_network_defaults(tmp_path: Path) -> None:
    sandbox = load(tmp_path, HOLDUP_NETWORK="sandbox")
    assert sandbox.root_account == "sandbox" and sandbox.vault_account == "vault.sandbox"
    assert sandbox.rpc_url == "http://127.0.0.1:3030" and sandbox.usdc_token is None
    assert sandbox.database_path == ".holdup/holdup.sqlite3"
    testnet = load(tmp_path, HOLDUP_NETWORK="testnet", HOLDUP_HUSD_TOKEN="husd.me.testnet")
    assert testnet.root_account == "holdup.testnet" and testnet.rpc_url == "https://rpc.testnet.near.org"
    assert testnet.demo_funds == {NEAR: 2 * 10**24, "husd.me.testnet": 2_000 * 10**6}
    chosen = load_settings(
        env={"HOLDUP_NETWORK": "testnet"}, credentials_path=str(tmp_path / "none"), network="sandbox"
    )
    assert chosen.network == "sandbox"


def test_otari_mode(tmp_path: Path) -> None:
    assert load(tmp_path).otari_mode == "replay"
    assert load(tmp_path, OTARI_API_KEY="otk_test").otari_mode == "live"
    assert load(tmp_path, OTARI_API_KEY="otk_test", OTARI_MODE="replay").otari_mode == "replay"
    with pytest.raises(ValueError, match="OTARI_MODE"):
        load(tmp_path, OTARI_MODE="sometimes")


def test_unknown_networks_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="HOLDUP_NETWORK"):
        load(tmp_path, HOLDUP_NETWORK="mainnet")
