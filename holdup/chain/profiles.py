"""Vault configuration profiles, shared by deploy and the in-memory vault.

The configuration is fixed forever when the vault is initialised, so these
numbers are the whole policy: how long new payees wait, how long trust and
guardian changes take to apply, and how much may go out instantly per day.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..models import NEAR, VaultConfig

SECOND_MS = 1_000
MINUTE_MS = 60 * SECOND_MS
HOUR_MS = 60 * MINUTE_MS

YOCTO_PER_NEAR = 10**24
REGISTRATION_FEE = YOCTO_PER_NEAR // 100  # 0.01 NEAR kept by the vault as storage reserve
MAX_ACTIVE_HOLDS = 10
INSTANT_CAP_UNITS = {"NEAR": 10, "HUSD": 100, "USDC": 100}  # whole tokens per UTC day

# Payees of the training samples and the scripted demo story. Offline they are .testnet names;
# on a live network they are keyless sub-accounts of the root, which the deployment creates:
# all of them with the demo profile, and with the hosted profile only SAMPLE_PAYEES (both add FRIENDS).
DEMO_PAYEES = ("safe-wallet-77", "northwind-paper", "northwindpaper", "ravi", "mum", "attacker")
# What the training samples pay or trust (fixtures/messages/index.json). The scripted story
# also pays "mum", and the attack walkthrough "attacker".
SAMPLE_PAYEES = ("safe-wallet-77", "northwind-paper", "northwindpaper", "ravi")
# The friend of the dinner sample on a live network: each person pays one of these, chosen from
# their user key (holdup.bot.samples.friend_account). Were it one shared account, a busy day of
# people trying the sample (eight senders) or two cancels after a warning would brake everyone's
# ordinary payment to it.
FRIENDS = tuple(f"ravi-{number}" for number in range(10))


@dataclass(frozen=True)
class Profile:
    name: str
    new_payee_hold_ms: int
    trust_delay_ms: int
    guardian_delay_ms: int
    max_hold_ms: int = 72 * HOUR_MS


# The hosted bot is for people trying HoldUp for a few minutes, so a new payee waits five minutes
# and trust takes five. A guardian change still waits a day, long enough for the guardian in
# charge to hear about it (from the bot or from the vault's guardian_changed events) and veto it.
PROFILES: dict[str, Profile] = {
    "demo": Profile("demo", 20 * SECOND_MS, 30 * SECOND_MS, 30 * SECOND_MS),
    "hosted": Profile("hosted", 5 * MINUTE_MS, 5 * MINUTE_MS, 24 * HOUR_MS),
}
# The payees each profile's deployment creates.
PROFILE_PAYEES: dict[str, tuple[str, ...]] = {"demo": (*DEMO_PAYEES, *FRIENDS), "hosted": (*SAMPLE_PAYEES, *FRIENDS)}


def vault_config(settings: Settings, profile: str = "demo") -> VaultConfig:
    """The vault configuration for a profile and the assets configured in ``settings``."""
    chosen = PROFILES[profile]
    caps: dict[str, int] = {}
    for asset_id, info in settings.assets.items():
        units = INSTANT_CAP_UNITS.get(info.symbol, 0)
        caps[asset_id] = units * 10**info.decimals
    return VaultConfig(
        oracles=list(settings.oracle_accounts),
        tokens=[asset_id for asset_id in settings.assets if asset_id != NEAR],
        new_payee_hold_ms=chosen.new_payee_hold_ms,
        max_hold_ms=chosen.max_hold_ms,
        trust_delay_ms=chosen.trust_delay_ms,
        guardian_delay_ms=chosen.guardian_delay_ms,
        instant_caps=caps,
        max_active_holds=MAX_ACTIVE_HOLDS,
        registration_fee=REGISTRATION_FEE,
    )


def config_input(config: VaultConfig) -> dict[str, Any]:
    """``ConfigInput`` JSON for the contract's ``new`` (U64/U128 as strings)."""
    return {
        "oracles": list(config.oracles),
        "tokens": list(config.tokens),
        "new_payee_hold_ms": str(config.new_payee_hold_ms),
        "max_hold_ms": str(config.max_hold_ms),
        "trust_delay_ms": str(config.trust_delay_ms),
        "guardian_delay_ms": str(config.guardian_delay_ms),
        "instant_caps": {asset: str(cap) for asset, cap in config.instant_caps.items()},
        "max_active_holds": config.max_active_holds,
        "registration_fee": str(config.registration_fee),
    }
