"""Runtime settings, read from environment variables and an optional local credentials file.

Secrets never live in the repository. `make deploy` writes `.holdup/credentials.json`
(git-ignored) with the account ids and keys it created; environment variables win
over that file.

The ``offline`` network is the in-memory world used by the demo, the console and
the tests: its accounts and its HUSD demo token have fixed names, and its secret
is public unless ``HOLDUP_SECRET`` is set, because nothing in it is real.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .models import NEAR, AssetInfo

USDC_TESTNET = "3e2210e1184b45b64c8a434c0a7e7b23cc04ea7eb7a6c3c32520d03d4afcb8af"

NETWORKS = ("offline", "sandbox", "testnet")
OTARI_MODES = ("live", "replay", "off")

RPC_URLS = {
    "testnet": "https://rpc.testnet.near.org",
    "sandbox": "http://127.0.0.1:3030",
}
DEFAULT_ROOTS = {
    "testnet": "holdup.testnet",
    "sandbox": "sandbox",  # the root account of the nearcore sandbox and near-kit's helpers
}

OFFLINE_ROOT = "holdup.testnet"
OFFLINE_SECRET = "holdup-offline-demo-secret"
"""Derives the in-memory world's accounts. Public on purpose; never used on a real network."""

DEMO_NEAR_YOCTO = 2 * 10**24
DEMO_HUSD_UNITS = 2_000 * 10**6
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
    public_base_url: str = "http://127.0.0.1:8000"
    # Where the fictional training pages are linked from (a separate host keeps phishing reports
    # about the demo links off the bot's own host); empty means public_base_url.
    fixture_base_url: str = ""
    otari_api_key: str = field(default="", repr=False)
    otari_base_url: str = ""
    otari_mode: str = "replay"  # "live", "replay" (recorded fixtures, offline only) or "off" (rules only)
    database_path: str = ".holdup/holdup.sqlite3"
    demo_funds: dict[str, int] = field(default_factory=dict)
    user_gas_near: str = "0.05"

    # ------------------------------------------------------------------
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
        "demo_funds": {NEAR: DEMO_NEAR_YOCTO, husd: DEMO_HUSD_UNITS},
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
    channels = {
        "telegram_bot_token": pick("TELEGRAM_BOT_TOKEN"),
        "telegram_webhook_secret": pick("TELEGRAM_WEBHOOK_SECRET"),
        "public_base_url": pick("PUBLIC_BASE_URL", None, "http://127.0.0.1:8000").rstrip("/"),
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
        usdc_token=pick("HOLDUP_USDC_TOKEN", "usdc_token", USDC_TESTNET if network == "testnet" else "") or None,
        husd_token=pick("HOLDUP_HUSD_TOKEN", "husd_token", "") or None,
        database_path=pick("HOLDUP_DB", None, ".holdup/holdup.sqlite3"),
        **channels,  # type: ignore[arg-type]
    )
    funds: dict[str, int] = {NEAR: int(pick("HOLDUP_DEMO_NEAR_YOCTO", None, str(DEMO_NEAR_YOCTO)))}
    if s.husd_token:
        funds[s.husd_token] = int(pick("HOLDUP_DEMO_HUSD", None, str(DEMO_HUSD_UNITS)))
    s.demo_funds = funds
    return s
