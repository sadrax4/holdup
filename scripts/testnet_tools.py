"""Testnet account helpers used by scripts/testnet.sh.

Keys are written to the credentials file, HOLDUP_CREDENTIALS or .holdup/credentials.json
(git-ignored, mode 600), the same file `holdup deploy` reads, and never printed. Every call goes
to NEAR testnet: the RPC is NEAR_RPC_URL or HoldUp's testnet default, and the faucet is NEAR's
documented faucet API (5 testnet NEAR per request).

    status                             the configured accounts, their balances and keys
    budget [root] [--profile P]        what deploying profile P (default hosted) under root takes
    bootstrap <root>                   create root with faucet money, then top it up to the hosted budget
    topup <account> --min <NEAR>       ask the faucet until the account holds that much
    faucet <account>                   one faucet request
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx
from near import AccountNotFoundError, Near, generate_key

from holdup.chain.budget import available_balance, near_text, near_to_yocto
from holdup.chain.deploy import DeployRefused, budget_for
from holdup.chain.faucet import FAUCET_URL, faucet_account_ok, request_drop
from holdup.config import RPC_URLS, load_settings

CREDS = Path(os.environ.get("HOLDUP_CREDENTIALS") or ".holdup/credentials.json")
RPC_URL = os.environ.get("NEAR_RPC_URL") or RPC_URLS["testnet"]
MAX_REQUESTS = 4  # faucet requests per topup run
PAUSE_S = 20.0  # between two faucet requests
SETTLE_TRIES, SETTLE_S = 5, 2.0  # re-reading the balance after a request
ROOT_DEPOSIT = "4.8"  # what the registrar call gives a new root out of the funder's 5 NEAR

Sleep = Callable[[float], None]


# ----------------------------------------------------------------------
# reading testnet
# ----------------------------------------------------------------------


def rpc(http: httpx.Client, method: str, params: dict[str, Any], *, url: str = RPC_URL) -> dict[str, Any]:
    """One JSON-RPC call; the JSON-RPC ``error`` object comes back as ``{"error": ...}``."""
    response = http.post(url, json={"jsonrpc": "2.0", "id": "holdup", "method": method, "params": params},
                         timeout=30)
    if response.status_code == 429 or response.status_code >= 500:
        raise SystemExit(f"the RPC endpoint is busy (HTTP {response.status_code}); try again in a minute")
    try:
        payload = response.json()
    except ValueError as exc:
        raise SystemExit(f"the RPC endpoint answered HTTP {response.status_code} without JSON") from exc
    if payload.get("error"):
        return {"error": payload["error"]}
    return {"result": payload.get("result")}


def balance_of(http: httpx.Client, account_id: str, *, url: str = RPC_URL) -> int | None:
    """What ``account_id`` can spend in yoctoNEAR, or None if it does not exist."""
    answer = rpc(http, "query", {"request_type": "view_account", "finality": "final", "account_id": account_id},
                 url=url)
    if "error" in answer:
        cause = (answer["error"].get("cause") or {}).get("name")
        if cause == "UNKNOWN_ACCOUNT":
            return None
        raise SystemExit(f"view_account {account_id} failed: {answer['error'].get('message') or cause}")
    view = answer["result"]
    return available_balance(int(view["amount"]), int(view.get("locked", 0)), int(view.get("storage_usage", 0)))


def _settled_balance(http: httpx.Client, account_id: str, before: int, sleep: Sleep, *, url: str) -> int:
    """Re-read the balance until it changes: the faucet's transfer may land a block or two later."""
    balance = before
    for attempt in range(SETTLE_TRIES):
        if attempt:
            sleep(SETTLE_S)
        balance = balance_of(http, account_id, url=url) or 0
        if balance > before:
            break
    return balance


# ----------------------------------------------------------------------
# the faucet
# ----------------------------------------------------------------------


def topup(
    account_id: str,
    minimum: int,
    *,
    http: httpx.Client,
    url: str = RPC_URL,
    faucet_url: str = FAUCET_URL,
    sleep: Sleep = time.sleep,
    max_requests: int = MAX_REQUESTS,
) -> int:
    """Ask the faucet until ``account_id`` holds ``minimum`` yoctoNEAR; return what it holds.

    At most ``max_requests`` requests, 20 s apart. The balance is re-read after each one,
    because a request that timed out may still have funded the account.
    """
    if not faucet_account_ok(account_id):
        raise SystemExit("topup: the account must be a .testnet account or a 64-hex implicit account")
    found = balance_of(http, account_id, url=url)
    if found is None and len(account_id) != 64:
        raise SystemExit(f"{account_id} does not exist on testnet; the faucet funds existing accounts")
    balance = found or 0
    print(f"{account_id}: {near_text(balance, up=False)} NEAR, target {near_text(minimum)} NEAR")
    requests = 0
    while balance < minimum and requests < max_requests:
        if requests:
            sleep(PAUSE_S)
        requests += 1
        reply = request_drop(http, account_id, url=faucet_url)
        print(f"faucet request {requests}/{max_requests}: HTTP {reply.status or 'no answer'} {reply.message}")
        if reply.status == 400:
            raise SystemExit(f"the faucet refused {account_id}")
        balance = _settled_balance(http, account_id, balance, sleep, url=url)
        print(f"{account_id}: {near_text(balance, up=False)} NEAR")
    if balance < minimum:
        raise SystemExit(
            f"{account_id} holds {near_text(balance, up=False)} NEAR, {near_text(minimum - balance)} NEAR short "
            f"after {requests} faucet requests; run topup again later"
        )
    return balance


def faucet(account_id: str, *, http: httpx.Client) -> None:
    if not faucet_account_ok(account_id):
        raise SystemExit("faucet: account must be a .testnet account or a 64-hex implicit account")
    reply = request_drop(http, account_id)
    print(f"faucet {account_id[:20]}: HTTP {reply.status or 'no answer'} {reply.message}")
    if not reply.ok:
        raise SystemExit(1)


# ----------------------------------------------------------------------
# credentials and the root account
# ----------------------------------------------------------------------


def _load() -> dict:
    try:
        return json.loads(CREDS.read_text())
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    CREDS.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = CREDS.with_name(CREDS.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # private from the start
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(data, indent=1, sort_keys=True))
    os.chmod(tmp, 0o600)
    tmp.replace(CREDS)


def hosted_budget(root: str) -> int:
    """What deploying the hosted profile under ``root`` takes, in yoctoNEAR.

    Planning it also checks every account id the deployment would create under ``root``.
    """
    settings = load_settings(network="testnet")
    try:
        return budget_for(settings, network="testnet", profile="hosted", root=root).total
    except DeployRefused as exc:
        raise SystemExit(f"bootstrap: {exc}") from None


def _wait_for(http: httpx.Client, account_id: str, sleep: Sleep, tries: int = 30) -> int:
    for _ in range(tries):
        balance = balance_of(http, account_id)
        if balance:
            return balance
        sleep(2)
    raise SystemExit(f"{account_id} did not appear on testnet")


def bootstrap(root: str, *, http: httpx.Client, sleep: Sleep = time.sleep) -> None:
    """Reserve ``root`` under the testnet registrar with faucet money, then top it up to the hosted budget."""
    if not root.endswith(".testnet") or root.count(".") != 1:
        raise SystemExit("bootstrap: root must be a top-level .testnet account such as holdup.testnet")
    budget = hosted_budget(root)  # refuses a root whose accounts would not be valid, before any faucet request
    data = _load()
    exists = balance_of(http, root) is not None
    if exists and not (data.get("root_account") == root and data.get("treasury_private_key")):
        raise SystemExit(f"{root} already exists and is not ours; pick another root name")
    if not exists:
        funder = generate_key()
        implicit = funder.public_key.data.hex()
        data.update({"network": "testnet", "funder_implicit_account": implicit,
                     "funder_private_key": funder.secret_key})
        _save(data)  # keep the funder key even if a later step fails
        reply = request_drop(http, implicit)
        print(f"faucet {implicit[:20]}: HTTP {reply.status or 'no answer'} {reply.message}")
        if not reply.maybe_funded:
            raise SystemExit("the faucet refused the funding account; try again later")
        _wait_for(http, implicit, sleep)

        root_key = generate_key()
        data.update({"root_account": root, "treasury_private_key": root_key.secret_key})
        _save(data)
        signer = Near("testnet", rpc_url=RPC_URL, account_id=implicit, private_key=funder.secret_key)
        result = signer.call(
            "testnet",
            "create_account",
            {"new_account_id": root, "new_public_key": str(root_key.public_key)},
            gas="100 Tgas",
            deposit=f"{ROOT_DEPOSIT} NEAR",
        )
        tx = getattr(result, "transaction_hash", None) or getattr(result, "hash", None)
        print(f"create_account tx: {tx}")
        _wait_for(http, root, sleep)
        keys = [str(k.public_key) for k in Near("testnet", rpc_url=RPC_URL).access_keys(root)]
        print(f"{root} has our full-access key: {str(root_key.public_key) in keys}")
    else:
        print(f"{root} is already set up in {CREDS}")
    print(f"The hosted deployment needs {near_text(budget)} NEAR (scripts/testnet.sh budget shows the details).")
    topup(root, budget, http=http, sleep=sleep)


def status() -> None:
    data = _load()
    client = Near("testnet", rpc_url=RPC_URL)
    names = [data.get("root_account"), data.get("vault_account"), data.get("keeper_account"), data.get("husd_token")]
    names += list(data.get("oracle_accounts") or [])
    for name in filter(None, names):
        try:
            keys = client.access_keys(name)
        except AccountNotFoundError:
            print(f"{name}: not found")
            continue
        print(f"{name}: {client.balance(name)}, {len(keys)} access key(s)")


def budget(root: str, profile: str) -> None:
    settings = load_settings(network="testnet")
    print(budget_for(settings, network="testnet", profile=profile, root=root).describe())


# ----------------------------------------------------------------------
# command line
# ----------------------------------------------------------------------


def _near_amount(text: str) -> int:
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"not an amount of NEAR: {text!r}") from exc
    if value <= 0 or value > 100:
        raise argparse.ArgumentTypeError("the minimum must be above 0 and at most 100 NEAR")
    return near_to_yocto(text)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="testnet_tools.py", description="NEAR testnet helpers for HoldUp.")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    b = sub.add_parser("budget")
    b.add_argument("root", nargs="?")
    b.add_argument("--profile", choices=("hosted", "demo"), default="hosted")
    sub.add_parser("bootstrap").add_argument("root")
    t = sub.add_parser("topup")
    t.add_argument("account")
    t.add_argument("--min", dest="minimum", type=_near_amount, required=True, help="NEAR the account should hold")
    sub.add_parser("faucet").add_argument("account")
    return p


def main(argv: list[str], *, http: httpx.Client | None = None, sleep: Sleep = time.sleep) -> None:
    args = parser().parse_args(argv)
    if args.command == "status":
        status()
        return
    if args.command == "budget":
        budget(args.root or _load().get("root_account") or "holdup.testnet", args.profile)
        return
    with http or httpx.Client() as client:
        if args.command == "bootstrap":
            bootstrap(args.root, http=client, sleep=sleep)
        elif args.command == "topup":
            topup(args.account, args.minimum, http=client, sleep=sleep)
        else:
            faucet(args.account, http=client)


if __name__ == "__main__":
    main(sys.argv[1:])
