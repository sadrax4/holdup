"""Testnet account helpers used by scripts/testnet.sh.

Keys are written to .holdup/credentials.json (git-ignored, mode 600) and never printed.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx
import near
from near import Near, generate_key

FAUCET_URL = "https://docs-mcp-f18b.onrender.com/api/faucet"  # documented at docs.near.org/getting-started/faucet
CREDS = Path(".holdup/credentials.json")


def _client() -> Near:
    return Near("testnet")


def _exists(client: Near, account_id: str) -> bool:
    try:
        client.account(account_id)
        return True
    except near.AccountNotFoundError:
        return False


def _load() -> dict:
    try:
        return json.loads(CREDS.read_text())
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    CREDS.parent.mkdir(mode=0o700, exist_ok=True)
    tmp = CREDS.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    os.chmod(tmp, 0o600)
    tmp.replace(CREDS)


def faucet(account_id: str) -> None:
    if not (account_id.endswith(".testnet") or (len(account_id) == 64 and all(c in "0123456789abcdef" for c in account_id))):
        raise SystemExit("faucet: account must be a .testnet account or a 64-hex implicit account")
    r = httpx.post(FAUCET_URL, json={"accountId": account_id}, timeout=120)
    print(f"faucet {account_id[:20]}: HTTP {r.status_code} {r.text[:160]}")
    r.raise_for_status()


def _wait_balance(client: Near, account_id: str, tries: int = 30) -> None:
    for _ in range(tries):
        if _exists(client, account_id):
            print(f"{account_id[:20]} balance: {client.balance(account_id)}")
            return
        time.sleep(2)
    raise SystemExit(f"{account_id} did not appear on testnet")


def bootstrap(root: str) -> None:
    """Reserve `root` under the testnet registrar, paid with faucet funds."""
    if not root.endswith(".testnet") or root.count(".") != 1:
        raise SystemExit("bootstrap: root must be a top-level .testnet account such as holdup.testnet")
    data = _load()
    client = _client()
    if data.get("root_account") == root and data.get("treasury_private_key") and _exists(client, root):
        print(f"{root} is already set up in .holdup/credentials.json")
        return
    if _exists(client, root):
        raise SystemExit(f"{root} already exists and is not ours; pick another root name")

    funder = generate_key()
    implicit = funder.public_key.data.hex()
    data.update({"network": "testnet", "funder_implicit_account": implicit, "funder_private_key": funder.secret_key})
    _save(data)  # keep the funder key even if a later step fails
    faucet(implicit)
    _wait_balance(client, implicit)

    root_key = generate_key()
    data.update({"root_account": root, "treasury_private_key": root_key.secret_key})
    _save(data)
    signer = Near("testnet", account_id=implicit, private_key=funder.secret_key)
    result = signer.call(
        "testnet",
        "create_account",
        {"new_account_id": root, "new_public_key": str(root_key.public_key)},
        gas="100 Tgas",
        deposit="4.8 NEAR",
    )
    tx = getattr(result, "transaction_hash", None) or getattr(result, "hash", None)
    print(f"create_account tx: {tx}")
    _wait_balance(client, root)
    keys = [str(k.public_key) for k in client.access_keys(root)]
    print(f"{root} has our full-access key: {str(root_key.public_key) in keys}")


def status() -> None:
    data = _load()
    client = _client()
    names = [data.get("root_account"), data.get("vault_account"), data.get("keeper_account"), data.get("husd_token")]
    names += list(data.get("oracle_accounts") or [])
    for name in filter(None, names):
        if _exists(client, name):
            keys = client.access_keys(name)
            print(f"{name}: {client.balance(name)}, {len(keys)} access key(s)")
        else:
            print(f"{name}: not found")


def main(argv: list[str]) -> None:
    if not argv:
        raise SystemExit("usage: testnet_tools.py status | bootstrap <root> | faucet <account>")
    cmd, args = argv[0], argv[1:]
    if cmd == "status":
        status()
    elif cmd == "bootstrap" and len(args) == 1:
        bootstrap(args[0])
    elif cmd == "faucet" and len(args) == 1:
        faucet(args[0])
    else:
        raise SystemExit(f"unknown command: {' '.join(argv)}")


if __name__ == "__main__":
    main(sys.argv[1:])
