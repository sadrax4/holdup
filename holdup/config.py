"""Runtime settings, read from environment variables and an optional local credentials file.

Secrets never live in the repository. `make deploy` writes `.holdup/credentials.json`
(git-ignored) with the account ids and keys it created; environment variables win
over that file.

The ``offline`` network is the in-memory world used by the demo, the console and
the tests: its accounts and its HUSD demo token have fixed names, and its secret
is public unless ``HOLDUP_SECRET`` is set, because nothing in it is real.
"""

from __future__ import annotations

import ipaddress
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from .models import NEAR, AssetInfo

# Circle's USDC on NEAR testnet. Used only when HOLDUP_USDC_TOKEN names it.
USDC_TESTNET = "3e2210e1184b45b64c8a434c0a7e7b23cc04ea7eb7a6c3c32520d03d4afcb8af"

NETWORKS = ("offline", "sandbox", "testnet")
OTARI_MODES = ("live", "replay", "off")
# How the bot gets its updates: long polling (no inbound connection needed), Telegram's
# webhook (needs a public https URL), or not at all.
TELEGRAM_MODES = ("polling", "webhook", "off")
DEFAULT_PORT = 8000

# Testnet: FastNEAR's free endpoint, the first one NEAR's docs list. rpc.testnet.near.org is no
# longer listed there and was announced as rate limited. Override with NEAR_RPC_URL.
RPC_URLS = {
    "testnet": "https://test.rpc.fastnear.com",
    "sandbox": "http://127.0.0.1:3030",
}
DEFAULT_ROOTS = {
    "testnet": "holdup.testnet",
    "sandbox": "sandbox",  # the root account of the nearcore sandbox and near-kit's helpers
}

OFFLINE_ROOT = "holdup.testnet"
OFFLINE_SECRET = "holdup-offline-demo-secret"
"""Derives the in-memory world's accounts. Public on purpose; never used on a real network."""

# What the treasury gives each new user on a real network. Testnet NEAR comes from a faucet
# (5 NEAR a request), so it is little: 0.2 NEAR in the vault pays the 0.015 NEAR storage fee of
# at least ten holds with room for small NEAR payments, and 0.03 NEAR in the user's own account
# pays the gas of their calls. Payments themselves are in HUSD.
DEMO_NEAR_YOCTO = 2 * 10**23
DEMO_HUSD_UNITS = 2_000 * 10**6
DEMO_USER_GAS_NEAR = "0.03"
# The in-memory world keeps its larger NEAR amount; nothing in it is real.
DEMO_NEAR_YOCTO_OFFLINE = 2 * 10**24
EXPLORER_TX = {
    "testnet": "https://testnet.near.rocks/tx/{tx}",
    "sandbox": "",
}
EXPLORER_TX_FALLBACK = {
    "testnet": "https://testnet.nearblocks.io/txns/{tx}",
}


@dataclass
class Settings:
    network: str = "offline"  # "offline" (in-memory vault), "sandbox" or "testnet"
    rpc_url: str = ""
    root_account: str = "holdup.testnet"
    vault_account: str = ""
    oracle_accounts: list[str] = field(default_factory=list)
    keeper_account: str = ""
    treasury_private_key: str = field(default="", repr=False)
    oracle_private_key: str = field(default="", repr=False)
    keeper_private_key: str = field(default="", repr=False)
    secret: str = field(default="", repr=False)
    usdc_token: str | None = None
    husd_token: str | None = None
    telegram_bot_token: str = field(default="", repr=False)
    telegram_webhook_secret: str = field(default="", repr=False)
    # TELEGRAM_MODE as configured: "polling", "webhook", "off", or empty for the default.
    # bot_mode() says what actually runs.
    telegram_mode: str = ""
    # Root of the Bot API server, e.g. http://127.0.0.1:8081 (tests and a self-hosted Bot API
    # server only); empty means Telegram's own.
    telegram_api_base_url: str = ""
    public_base_url: str = "http://127.0.0.1:8000"
    # Where the fictional training pages are linked from (a separate host keeps phishing reports
    # about the demo links off the bot's own host); empty means public_base_url.
    fixture_base_url: str = ""
    otari_api_key: str = field(default="", repr=False)
    otari_base_url: str = ""
    otari_mode: str = "replay"  # "live", "replay" (recorded fixtures, offline only) or "off" (rules only)
    database_path: str = ".holdup/holdup.sqlite3"
    demo_funds: dict[str, int] = field(default_factory=dict)
    user_gas_near: str = DEMO_USER_GAS_NEAR

    # ------------------------------------------------------------------
    def bot_mode(self) -> str:
        """How the Telegram bot gets its updates: ``telegram_mode``, else polling; off without a bot token."""
        if not self.telegram_bot_token:
            return "off"
        return self.telegram_mode or "polling"

    def web_is_public(self) -> bool:
        """Whether people elsewhere can open links to ``public_base_url`` (not a loopback or LAN address)."""
        return is_public_url(self.public_base_url)

    def secret_values(self) -> list[str]:
        """The configured secrets, for keeping them out of logs (an RPC API key in NEAR_RPC_URL too)."""
        values = (
            self.telegram_bot_token,
            self.telegram_webhook_secret,
            self.otari_api_key,
            self.secret,
            self.treasury_private_key,
            self.oracle_private_key,
            self.keeper_private_key,
            *url_credentials(self.rpc_url),
        )
        return [value for value in values if value]

    @property
    def assets(self) -> dict[str, AssetInfo]:
        out = {NEAR: AssetInfo(NEAR, "NEAR", 24, "NEAR")}
        if self.usdc_token:
            out[self.usdc_token] = AssetInfo(self.usdc_token, "USDC", 6, "Circle testnet USDC")
        if self.husd_token:
            out[self.husd_token] = AssetInfo(self.husd_token, "HUSD", 6, "HoldUp demo dollar (test token)")
        return out

    def asset_by_symbol(self, symbol: str) -> AssetInfo | None:
        symbol = symbol.strip().upper()
        for info in self.assets.values():
            if info.symbol == symbol:
                return info
        return None

    def tx_link(self, tx_hash: str) -> str:
        template = EXPLORER_TX.get(self.network, "")
        return template.format(tx=tx_hash) if template and tx_hash else ""

    def tx_link_fallback(self, tx_hash: str) -> str:
        template = EXPLORER_TX_FALLBACK.get(self.network, "")
        return template.format(tx=tx_hash) if template and tx_hash else ""

    def fixture_base(self) -> str:
        """Base URL of the training pages that the sample messages link to."""
        return self.fixture_base_url or self.public_base_url or "http://127.0.0.1:8000"

    def otari_url(self) -> str:
        if self.otari_base_url:
            return self.otari_base_url.rstrip("/")
        if self.otari_api_key.startswith("otk_v1_eu_"):
            return "https://eu.api.otari.ai/api/v1"
        return "https://api.otari.ai/api/v1"


LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".invalid", ".lan", ".home.arpa")


def is_public_url(url: str) -> bool:
    """Whether people elsewhere could open ``url``: http(s) on a public host name or address."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        return False
    if host == "localhost" or host.endswith(LOCAL_SUFFIXES):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." in host  # a single-label name only resolves on a local network
    return address.is_global


def url_credentials(url: str) -> list[str]:
    """What a URL may carry that is secret: a password, and query values such as ``?apiKey=...``.

    Query values shorter than 8 characters (``?v=1``) are not keys, and replacing them in
    error texts would only garble those.
    """
    parts = urlsplit(url)
    values = [value for _, value in parse_qsl(parts.query) if len(value) >= 8]
    if parts.password:
        values.append(parts.password)
    return values


def without_credentials(url: str) -> str:
    """``url`` without user info, query or fragment, for showing on a page."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, "", ""))


def port_from_env(env: Mapping[str, str]) -> int:
    """``HOLDUP_PORT``, the default port of ``holdup serve`` (8000 when unset)."""
    value = env.get("HOLDUP_PORT", "").strip()
    if not value:
        return DEFAULT_PORT
    if not value.isdigit() or not 0 <= int(value) <= 65535:
        raise ValueError(f"HOLDUP_PORT must be a port number (got {value!r})")
    return int(value)


def _load_credentials(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def offline_settings(public_base_url: str = "", **overrides: object) -> Settings:
    """Settings of the in-memory world: fixed accounts, the HUSD demo token, recorded AI answers.

    Nothing here comes from the environment, so tests and the scripted demo always
    see the same world. ``load_settings(network="offline")`` starts from these.
    """
    husd = f"husd.{OFFLINE_ROOT}"
    values: dict[str, object] = {
        "network": "offline",
        "root_account": OFFLINE_ROOT,
        "vault_account": f"vault.{OFFLINE_ROOT}",
        "oracle_accounts": [f"oracle.{OFFLINE_ROOT}"],
        "keeper_account": f"keeper.{OFFLINE_ROOT}",
        "secret": OFFLINE_SECRET,
        "husd_token": husd,
        "otari_mode": "replay",
        "database_path": ":memory:",
        "public_base_url": public_base_url.rstrip("/"),
        "demo_funds": {NEAR: DEMO_NEAR_YOCTO_OFFLINE, husd: DEMO_HUSD_UNITS},
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def load_settings(
    env: dict[str, str] | None = None, credentials_path: str | None = None, *, network: str | None = None
) -> Settings:
    """Settings for ``network``: the argument, else HOLDUP_NETWORK, else the credentials file, else offline.

    For the offline world the credentials file and the NEAR variables are ignored
    (its accounts are fixed); the web, Telegram, Otari and HOLDUP_SECRET variables
    still apply.
    """
    env = dict(os.environ if env is None else env)
    creds_file = Path(credentials_path or env.get("HOLDUP_CREDENTIALS", ".holdup/credentials.json"))
    creds = _load_credentials(creds_file)

    def pick(name: str, key: str | None = None, default: str = "") -> str:
        if env.get(name):
            return env[name]
        if key and creds.get(key):
            return str(creds[key])
        return default

    network = network or pick("HOLDUP_NETWORK", "network", "offline")
    if network not in NETWORKS:
        raise ValueError(f"HOLDUP_NETWORK must be one of {', '.join(NETWORKS)} (got {network!r})")
    # Without a key the offline world replays recorded answers. On a real network that would
    # attach recorded findings to real payments, so the checks there run on the rules alone.
    default_mode = "live" if env.get("OTARI_API_KEY") else ("replay" if network == "offline" else "off")
    otari_mode = pick("OTARI_MODE", None, default_mode)
    if otari_mode not in OTARI_MODES:
        raise ValueError(f"OTARI_MODE must be one of {', '.join(OTARI_MODES)} (got {otari_mode!r})")
    telegram_mode = pick("TELEGRAM_MODE").strip().lower()
    if telegram_mode and telegram_mode not in TELEGRAM_MODES:
        raise ValueError(f"TELEGRAM_MODE must be one of {', '.join(TELEGRAM_MODES)} (got {telegram_mode!r})")
    telegram_api = pick("TELEGRAM_API_BASE_URL").strip().rstrip("/")
    if telegram_api and urlsplit(telegram_api).scheme not in ("http", "https"):
        raise ValueError(f"TELEGRAM_API_BASE_URL must be an http or https URL (got {telegram_api!r})")
    channels = {
        "telegram_bot_token": pick("TELEGRAM_BOT_TOKEN"),
        "telegram_webhook_secret": pick("TELEGRAM_WEBHOOK_SECRET"),
        "telegram_mode": telegram_mode,
        "telegram_api_base_url": telegram_api,
        "public_base_url": pick("PUBLIC_BASE_URL", None, f"http://127.0.0.1:{port_from_env(env)}").rstrip("/"),
        "fixture_base_url": pick("FIXTURE_BASE_URL").rstrip("/"),
        "otari_api_key": pick("OTARI_API_KEY"),
        "otari_base_url": pick("OTARI_BASE_URL"),
        "otari_mode": otari_mode,
    }
    if network == "offline":
        return offline_settings(secret=env.get("HOLDUP_SECRET") or OFFLINE_SECRET, **channels)

    root = pick("HOLDUP_ROOT_ACCOUNT", "root_account", DEFAULT_ROOTS[network])
    oracles = pick("HOLDUP_ORACLE_ACCOUNTS", None, "")
    oracle_list = (
        [a.strip() for a in oracles.split(",") if a.strip()]
        or list(creds.get("oracle_accounts", []))
        or [f"oracle.{root}"]
    )
    s = Settings(
        network=network,
        rpc_url=pick("NEAR_RPC_URL", "rpc_url", RPC_URLS.get(network, "")),
        root_account=root,
        vault_account=pick("HOLDUP_VAULT_ACCOUNT", "vault_account", f"vault.{root}"),
        oracle_accounts=oracle_list,
        keeper_account=pick("HOLDUP_KEEPER_ACCOUNT", "keeper_account", f"keeper.{root}"),
        treasury_private_key=pick("HOLDUP_TREASURY_KEY", "treasury_private_key"),
        oracle_private_key=pick("HOLDUP_ORACLE_KEY", "oracle_private_key"),
        keeper_private_key=pick("HOLDUP_KEEPER_KEY", "keeper_private_key"),
        secret=pick("HOLDUP_SECRET", "secret"),
        # Circle's testnet USDC only when configured (HOLDUP_USDC_TOKEN=USDC_TESTNET): new users are
        # funded in HUSD, and a USDC nobody holds would only turn "send 50 USDC" into a refusal.
        usdc_token=pick("HOLDUP_USDC_TOKEN", "usdc_token", "") or None,
        husd_token=pick("HOLDUP_HUSD_TOKEN", "husd_token", "") or None,
        database_path=pick("HOLDUP_DB", None, ".holdup/holdup.sqlite3"),
        **channels,  # type: ignore[arg-type]
    )
    funds: dict[str, int] = {NEAR: int(pick("HOLDUP_DEMO_NEAR_YOCTO", None, str(DEMO_NEAR_YOCTO)))}
    if s.husd_token:
        funds[s.husd_token] = int(pick("HOLDUP_DEMO_HUSD", None, str(DEMO_HUSD_UNITS)))
    s.demo_funds = funds
    return s
