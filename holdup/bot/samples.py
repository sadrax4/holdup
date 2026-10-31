"""Training samples for /demo and the web console.

The texts live in ``fixtures/messages``: fictional brands (CoinHaven,
Northwind Paper Co.) made up for practice. On a live network the demo payees
are sub-accounts of the HoldUp root account, and the stablecoin is whichever
one this server funds new users with (the HUSD demo token unless Circle USDC
is configured), so the samples are rewritten to match.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ..chain.profiles import DEMO_PAYEES
from ..config import Settings
from ..models import NEAR

log = logging.getLogger(__name__)

MESSAGES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "messages"
STABLE_SYMBOLS = ("USDC", "HUSD")


@dataclass(frozen=True)
class Sample:
    id: str
    sender: str
    label: str
    text: str
    command: str  # what to type after forwarding the sample
    trusted: tuple[str, ...] = ()
    scam: bool = True


def stable_symbol(settings: Settings) -> str:
    """The stablecoin people here actually hold: the funded one, else any configured, else NEAR."""
    assets = settings.assets
    for asset_id, amount in settings.demo_funds.items():
        info = assets.get(asset_id)
        if asset_id != NEAR and amount > 0 and info is not None and info.symbol in STABLE_SYMBOLS:
            return info.symbol
    for info in assets.values():
        if info.symbol in STABLE_SYMBOLS:
            return info.symbol
    return "NEAR"


def demo_account(settings: Settings, account: str) -> str:
    """Offline the samples' payees exist as written; on a network they live under the root account."""
    label = account.removesuffix(".testnet")
    if settings.network == "offline" or label not in DEMO_PAYEES:
        return account
    return f"{label}.{settings.root_account}"


def load_samples(settings: Settings, directory: Path | None = None) -> list[Sample]:
    """The samples listed in ``index.json``, adapted to this server; empty if they are not installed."""
    folder = directory or MESSAGES_DIR
    try:
        index = json.loads((folder / "index.json").read_text())
    except (OSError, ValueError) as exc:
        log.warning("training samples unavailable: %s", exc)
        return []
    symbol = stable_symbol(settings)
    base_url = settings.fixture_base()
    samples = []
    for entry in index.get("messages", []):
        try:
            text = (folder / entry["file"]).read_text().strip().replace("{base_url}", base_url)
            payment = entry["payment"]
        except (OSError, KeyError, TypeError) as exc:
            log.warning("skipping training sample %r: %s", entry, exc)
            continue
        accounts = {payment["payee"], *entry.get("trusted", [])}
        for account in sorted(accounts, key=len, reverse=True):
            text = text.replace(account, demo_account(settings, account))
        asset = payment.get("asset", "NEAR").upper()
        asset = symbol if asset in STABLE_SYMBOLS else asset
        samples.append(
            Sample(
                id=entry["id"],
                sender=entry.get("sender", ""),
                label=entry.get("label", entry["id"]),
                text=text,
                command=f"send {payment['amount']} {asset} to {demo_account(settings, payment['payee'])}",
                trusted=tuple(demo_account(settings, t) for t in entry.get("trusted", [])),
                scam=bool(entry.get("rules_expect")),
            )
        )
    return samples


def find_sample(samples: list[Sample], sample_id: str) -> Sample | None:
    return next((s for s in samples if s.id == sample_id), None)
