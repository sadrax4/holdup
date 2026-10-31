"""Training samples for /demo and the web console.

The texts live in ``fixtures/messages``: fictional brands (CoinHaven,
Northwind Paper Co.) made up for practice. On a live network the demo payees
are sub-accounts of the HoldUp root account, and the stablecoin is whichever
one this server funds new users with (the HUSD demo token unless Circle USDC
is configured), so the samples are rewritten to match. There, each person's
ordinary payments go to a friend account of their own (``friend_account``).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path

from ..chain.profiles import DEMO_PAYEES, FRIENDS
from ..config import Settings
from ..models import NEAR

log = logging.getLogger(__name__)

MESSAGES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "messages"
STABLE_SYMBOLS = ("USDC", "HUSD")
FRIEND = "ravi.testnet"  # the dinner sample's payee, as the fixtures write it


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


def friend_account(settings: Settings, user_key: str = "") -> str:
    """The account this person's ordinary practice payments go to (the dinner sample, the examples).

    Offline it is ravi.testnet. On a live network each person gets one of the deployment's
    ``FRIENDS`` (ravi-0 ... ravi-9), always the same one, so that everyone trying the samples
    does not pay a single account; without a person it is ravi under the root.
    """
    if settings.network == "offline" or not user_key:
        return demo_account(settings, FRIEND)
    number = int.from_bytes(hashlib.sha256(user_key.encode()).digest()[:4], "big") % len(FRIENDS)
    return f"{FRIENDS[number]}.{settings.root_account}"


def for_person(sample: Sample, settings: Settings, user_key: str) -> Sample:
    """``sample`` with the friend's account replaced by this person's own (see ``friend_account``)."""
    shared, own = demo_account(settings, FRIEND), friend_account(settings, user_key)
    if shared == own:
        return sample
    return replace(
        sample,
        text=sample.text.replace(shared, own),
        command=sample.command.replace(shared, own),
        trusted=tuple(own if account == shared else account for account in sample.trusted),
    )


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
