"""Settings: network defaults, the offline world and Otari modes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from holdup.config import (
    OFFLINE_SECRET,
    USDC_TESTNET,
    is_public_url,
    load_settings,
    offline_settings,
    port_from_env,
    url_credentials,
    without_credentials,
)
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
    assert settings.oracle_accounts == ["oracle.me.testnet"]
    # Users are funded in HUSD; Circle's testnet USDC is a token of the vault only when configured.
    assert settings.usdc_token is None and [a.symbol for a in settings.assets.values()] == ["NEAR"]
    configured = load_settings(env={"HOLDUP_USDC_TOKEN": USDC_TESTNET}, credentials_path=str(tmp_path / "credentials.json"))
    assert configured.usdc_token == USDC_TESTNET


def test_network_defaults(tmp_path: Path) -> None:
    sandbox = load(tmp_path, HOLDUP_NETWORK="sandbox")
    assert sandbox.root_account == "sandbox" and sandbox.vault_account == "vault.sandbox"
    assert sandbox.rpc_url == "http://127.0.0.1:3030" and sandbox.usdc_token is None
    assert sandbox.database_path == ".holdup/holdup.sqlite3"
    testnet = load(tmp_path, HOLDUP_NETWORK="testnet", HOLDUP_HUSD_TOKEN="husd.me.testnet")
    assert testnet.root_account == "holdup.testnet" and testnet.rpc_url == "https://test.rpc.fastnear.com"
    # A new user on a real network gets 0.2 NEAR in the vault and 0.03 NEAR for gas; offline keeps 2 NEAR.
    assert testnet.demo_funds == {NEAR: 2 * 10**23, "husd.me.testnet": 2_000 * 10**6}
    assert testnet.user_gas_near == "0.03"
    chosen = load_settings(
        env={"HOLDUP_NETWORK": "testnet"}, credentials_path=str(tmp_path / "none"), network="sandbox"
    )
    assert chosen.network == "sandbox"


def test_otari_mode(tmp_path: Path) -> None:
    assert load(tmp_path).otari_mode == "replay"
    assert load(tmp_path, OTARI_API_KEY="otk_test").otari_mode == "live"
    assert load(tmp_path, OTARI_API_KEY="otk_test", OTARI_MODE="replay").otari_mode == "replay"
    # Recorded answers are never the default on a real network.
    for network in ("sandbox", "testnet"):
        assert load(tmp_path, HOLDUP_NETWORK=network).otari_mode == "off"
        assert load(tmp_path, HOLDUP_NETWORK=network, OTARI_API_KEY="otk_test").otari_mode == "live"


def test_training_pages_can_have_their_own_host(tmp_path: Path) -> None:
    assert load(tmp_path).fixture_base() == "http://127.0.0.1:8000"
    settings = load(tmp_path, PUBLIC_BASE_URL="https://holdup.example", FIXTURE_BASE_URL="https://fixtures.example/")
    assert (settings.fixture_base_url, settings.fixture_base()) == ("https://fixtures.example", "https://fixtures.example")
    with pytest.raises(ValueError, match="OTARI_MODE"):
        load(tmp_path, OTARI_MODE="sometimes")


def test_unknown_networks_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="HOLDUP_NETWORK"):
        load(tmp_path, HOLDUP_NETWORK="mainnet")


# ----------------------------------------------------------------------
# Telegram mode, the Bot API address, the port and public links
# ----------------------------------------------------------------------


def test_telegram_polls_by_default_when_there_is_a_token(tmp_path: Path) -> None:
    assert load(tmp_path).bot_mode() == "off"
    with_token = load(tmp_path, TELEGRAM_BOT_TOKEN="123:abc")
    assert (with_token.telegram_mode, with_token.bot_mode()) == ("", "polling")
    for value, mode in (("polling", "polling"), ("webhook", "webhook"), ("off", "off"), (" Webhook ", "webhook")):
        settings = load(tmp_path, TELEGRAM_BOT_TOKEN="123:abc", TELEGRAM_MODE=value)
        assert (settings.telegram_mode, settings.bot_mode()) == (mode, mode)
    # Without a token nothing runs, whatever the mode says.
    assert load(tmp_path, TELEGRAM_MODE="webhook").bot_mode() == "off"
    for network in ("sandbox", "testnet"):
        assert load(tmp_path, HOLDUP_NETWORK=network, TELEGRAM_BOT_TOKEN="123:abc").bot_mode() == "polling"
    with pytest.raises(ValueError, match="TELEGRAM_MODE must be one of polling, webhook, off"):
        load(tmp_path, TELEGRAM_MODE="push")


def test_settings_built_in_code_follow_the_token() -> None:
    settings = offline_settings()
    assert settings.bot_mode() == "off"
    settings.telegram_bot_token = "123:abc"  # set later, as tests and scripts do
    assert settings.bot_mode() == "polling"
    assert offline_settings(telegram_bot_token="123:abc", telegram_mode="webhook").bot_mode() == "webhook"


def test_the_bot_api_address_is_for_tests_and_self_hosted_servers(tmp_path: Path) -> None:
    assert load(tmp_path).telegram_api_base_url == ""
    settings = load(tmp_path, TELEGRAM_API_BASE_URL="http://127.0.0.1:8081/")
    assert settings.telegram_api_base_url == "http://127.0.0.1:8081"
    with pytest.raises(ValueError, match="TELEGRAM_API_BASE_URL"):
        load(tmp_path, TELEGRAM_API_BASE_URL="127.0.0.1:8081")


def test_holdup_port_is_the_default_for_local_links(tmp_path: Path) -> None:
    assert load(tmp_path, HOLDUP_PORT="47812").public_base_url == "http://127.0.0.1:47812"
    assert load(tmp_path, HOLDUP_PORT="47812", PUBLIC_BASE_URL="https://x.example/").public_base_url == "https://x.example"
    assert port_from_env({}) == 8000 and port_from_env({"HOLDUP_PORT": " 47999 "}) == 47999
    for bad in ("eighty", "-1", "65536", "8000.5"):
        with pytest.raises(ValueError, match="HOLDUP_PORT"):
            port_from_env({"HOLDUP_PORT": bad})


@pytest.mark.parametrize(
    ("url", "public"),
    [
        ("https://abc-def.trycloudflare.com", True),
        ("http://holdup.example:8080", True),
        ("https://8.8.8.8", True),
        ("http://127.0.0.1:8000", False),
        ("http://[::1]:8000", False),
        ("http://localhost:8000", False),
        ("http://holdup.localhost", False),
        ("http://192.168.1.20:8000", False),
        ("http://10.1.2.3", False),
        ("http://0.0.0.0:8000", False),
        ("http://testserver", False),
        ("http://nas.local", False),
        ("ftp://holdup.example", False),
        ("", False),
    ],
)
def test_public_urls(url: str, public: bool) -> None:
    assert is_public_url(url) is public
    assert offline_settings(public_base_url=url).web_is_public() is public


def test_secret_values_lists_what_logs_must_not_show() -> None:
    settings = offline_settings(telegram_bot_token="123:abc", otari_api_key="otk_x", treasury_private_key="")
    assert set(settings.secret_values()) == {"123:abc", "otk_x", OFFLINE_SECRET}


def test_an_api_key_in_the_rpc_url_is_a_secret_and_never_shown(tmp_path: Path) -> None:
    url = "https://test.rpc.fastnear.com/?apiKey=0123456789abcdef&v=1"
    settings = load(tmp_path, HOLDUP_NETWORK="testnet", NEAR_RPC_URL=url)
    assert "0123456789abcdef" in settings.secret_values()
    assert "1" not in settings.secret_values()  # short values are not keys; replacing them would garble errors
    assert url_credentials("https://user:pa55word@rpc.example/x") == ["pa55word"]
    assert without_credentials(url) == "https://test.rpc.fastnear.com/"
    assert without_credentials("https://user:pw@rpc.example:8443/path?apiKey=x#part") == "https://rpc.example:8443/path"
