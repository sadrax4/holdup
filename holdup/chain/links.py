"""Explorer and HoldUp page links for transactions, accounts and holds."""

from __future__ import annotations

from ..config import Settings

ACCOUNT_EXPLORER = {"testnet": "https://testnet.nearblocks.io/address/{account}"}


def tx_url(settings: Settings, tx_hash: str) -> str:
    """Primary explorer link, or "" when the network has no public explorer."""
    return settings.tx_link(tx_hash)


def tx_urls(settings: Settings, tx_hash: str) -> list[str]:
    """Primary and fallback explorer links that exist for this network."""
    return [url for url in (settings.tx_link(tx_hash), settings.tx_link_fallback(tx_hash)) if url]


def account_url(settings: Settings, account_id: str) -> str:
    template = ACCOUNT_EXPLORER.get(settings.network, "")
    return template.format(account=account_id) if template and account_id else ""


def hold_page(settings: Settings, hold_id: int) -> str:
    return f"{settings.public_base_url}/h/{hold_id}"


def evidence_url(settings: Settings, hold_id: int) -> str:
    return f"{settings.public_base_url}/v/{hold_id}.json"


def short_hash(tx_hash: str) -> str:
    return tx_hash if len(tx_hash) <= 12 else f"{tx_hash[:6]}...{tx_hash[-4:]}"


def describe_tx(settings: Settings, tx_hash: str, *, simulated: bool = False) -> str:
    """How to show a transaction to people: a link, or a short id marked simulated."""
    if not tx_hash:
        return ""
    if simulated:
        return f"tx {short_hash(tx_hash)} (simulated)"
    return tx_url(settings, tx_hash) or f"tx {tx_hash}"
