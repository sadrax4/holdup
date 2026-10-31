"""scripts/testnet_tools.py against a fake testnet RPC and a fake faucet: no network."""

from __future__ import annotations

import importlib.util
import json
import stat
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from near import AccountNotFoundError, generate_key
from near.units import Amount

from holdup.chain.deploy import UNFINISHED, DeployKeys
from holdup.chain.faucet import FAUCET_URL

NEAR = 10**24
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "testnet_tools.py"
# What the faucet answered on testnet on 2026-10-31, after six drops in five minutes.
RATE_LIMITED = {"error": "Failed to create me-testnet-1793477067191.testnet: 429 Rate limit exceeded, "
                         "retry in 10 hours."}


def load_tools() -> Any:
    spec = importlib.util.spec_from_file_location("testnet_tools", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tools = load_tools()


class FakeTestnet:
    """view_account on a fake RPC, and a faucet that funds an account with 5 NEAR per request."""

    def __init__(self, **balances: float) -> None:
        self.balances = {name.replace("_", "."): int(Decimal(str(near)) * NEAR) for name, near in balances.items()}
        self.requests: list[str] = []
        self.fund = True  # the faucet's transfer arrives
        self.answer: str = "ok"  # "ok", "timeout", "refuse", "rate limited"

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if str(request.url) == FAUCET_URL:
            account = body["accountId"]
            self.requests.append(account)
            if self.answer == "refuse":
                return httpx.Response(400, json={"error": "invalid accountId"})
            if self.answer == "rate limited":
                return httpx.Response(500, json=RATE_LIMITED)
            if self.fund:
                self.balances[account] = self.balances.get(account, 0) + 5 * NEAR
            if self.answer == "timeout":
                raise httpx.ReadTimeout("the faucet took too long", request=request)
            return httpx.Response(200, json={"success": True, "message": f"Funded {account} with 5 NEAR"})
        params = body["params"]
        assert body["method"] == "query" and params["request_type"] == "view_account"
        account = params["account_id"]
        if account not in self.balances:
            error = {"name": "HANDLER_ERROR", "cause": {"name": "UNKNOWN_ACCOUNT", "info": {}},
                     "message": "Server error"}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": error})
        view = {"amount": str(self.balances[account]), "locked": "0", "storage_usage": 182, "code_hash": "1" * 32}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": view})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))


class Sleeps(list[float]):
    def __call__(self, seconds: float) -> None:
        self.append(seconds)


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read or write the real credentials file."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOLDUP_CREDENTIALS", str(tmp_path / ".holdup" / "credentials.json"))
    monkeypatch.setattr(tools, "CREDS", tmp_path / ".holdup" / "credentials.json")


def test_topup_asks_the_faucet_until_the_minimum_is_reached() -> None:
    chain, sleeps = FakeTestnet(me_testnet=4.8), Sleeps()
    balance = tools.topup("me.testnet", 11 * NEAR, http=chain.client(), sleep=sleeps)
    assert balance == 148 * NEAR // 10 and chain.requests == ["me.testnet", "me.testnet"]
    assert sleeps == [tools.PAUSE_S]  # 20 s between the two requests; each balance changed at once


def test_topup_does_nothing_when_the_account_already_holds_enough() -> None:
    chain = FakeTestnet(me_testnet=12)
    assert tools.topup("me.testnet", 11 * NEAR, http=chain.client(), sleep=Sleeps()) == 12 * NEAR
    assert chain.requests == []


def test_topup_stops_after_four_requests(capsys: pytest.CaptureFixture[str]) -> None:
    chain, sleeps = FakeTestnet(me_testnet=1), Sleeps()
    chain.fund = False  # the faucet has run dry
    with pytest.raises(SystemExit) as info:
        tools.topup("me.testnet", 11 * NEAR, http=chain.client(), sleep=sleeps)
    assert len(chain.requests) == 4
    assert "10.0000 NEAR short after 4 faucet requests" in str(info.value)
    assert sleeps.count(tools.PAUSE_S) == 3  # 20 s apart
    assert sleeps.count(tools.SETTLE_S) == 4 * (tools.SETTLE_TRIES - 1)  # the balance re-read after each one


def test_a_request_that_timed_out_still_counts_when_the_money_arrived() -> None:
    chain = FakeTestnet(me_testnet=1)
    chain.answer = "timeout"
    assert tools.topup("me.testnet", 5 * NEAR, http=chain.client(), sleep=Sleeps()) == 6 * NEAR
    assert chain.requests == ["me.testnet"]


def test_topup_refuses_accounts_the_faucet_cannot_fund() -> None:
    chain = FakeTestnet()
    with pytest.raises(SystemExit, match=".testnet account or a 64-hex"):
        tools.topup("me.near", NEAR, http=chain.client(), sleep=Sleeps())
    with pytest.raises(SystemExit, match="does not exist on testnet"):
        tools.topup("nobody.testnet", NEAR, http=chain.client(), sleep=Sleeps())
    chain.balances["me.testnet"] = 0
    chain.answer = "refuse"
    with pytest.raises(SystemExit, match="refused"):
        tools.topup("me.testnet", NEAR, http=chain.client(), sleep=Sleeps())
    assert chain.requests == ["me.testnet"]


def test_bootstrap_tops_an_existing_root_up_to_the_hosted_budget(tmp_path: Path) -> None:
    tools.CREDS.parent.mkdir()
    tools.CREDS.write_text(json.dumps({"network": "testnet", "root_account": "me.testnet",
                                       "treasury_private_key": "ed25519:not-used-here"}))
    chain = FakeTestnet(me_testnet=4.8)
    budget = tools.hosted_budget("me.testnet")
    assert 10 * NEAR < budget < 12 * NEAR
    tools.bootstrap("me.testnet", http=chain.client(), sleep=Sleeps())
    assert chain.balances["me.testnet"] >= budget and chain.requests == ["me.testnet"] * 2


def test_the_credentials_file_is_the_one_holdup_credentials_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """server.sh keeps it in ~/work/secrets; bootstrap must write where `holdup deploy` then reads."""
    path = tmp_path / "secrets" / "holdup-credentials.json"
    monkeypatch.setenv("HOLDUP_CREDENTIALS", str(path))
    module = load_tools()
    assert module.CREDS == path
    module._save({"network": "testnet", "root_account": "me.testnet"})
    assert json.loads(path.read_text()) == {"network": "testnet", "root_account": "me.testnet"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(p.name for p in path.parent.iterdir()) == ["holdup-credentials.json"]
    monkeypatch.delenv("HOLDUP_CREDENTIALS")
    assert load_tools().CREDS == Path(".holdup/credentials.json")


def test_bootstrap_refuses_a_root_that_is_not_ours() -> None:
    chain = FakeTestnet(taken_testnet=3)
    with pytest.raises(SystemExit, match="not ours"):
        tools.bootstrap("taken.testnet", http=chain.client(), sleep=Sleeps())
    with pytest.raises(SystemExit, match="top-level"):
        tools.bootstrap("a.b.testnet", http=chain.client(), sleep=Sleeps())
    assert chain.requests == []


def test_bootstrap_refuses_a_root_too_long_for_the_accounts_under_it_before_any_faucet_request() -> None:
    chain = FakeTestnet()
    root = "a" * 41 + ".testnet"  # 49 characters: northwind-paper.<root> would have 65
    with pytest.raises(SystemExit, match="choose a root account of at most 48 characters"):
        tools.bootstrap(root, http=chain.client(), sleep=Sleeps())
    assert chain.requests == [] and not tools.CREDS.exists()


def test_the_command_line(capsys: pytest.CaptureFixture[str]) -> None:
    chain = FakeTestnet(me_testnet=1)
    tools.main(["topup", "me.testnet", "--min", "6.5"], http=chain.client(), sleep=Sleeps())
    assert chain.requests == ["me.testnet"] * 2  # 1 + 5 is not enough, 1 + 10 is
    tools.main(["budget", "me.testnet", "--profile", "demo"])
    out = capsys.readouterr().out
    assert "Budget of the demo deployment under me.testnet on testnet" in out and "create 16 demo payees" in out
    with pytest.raises(SystemExit):
        tools.main(["topup", "me.testnet", "--min", "lots"], http=chain.client(), sleep=Sleeps())


def test_status_shows_each_balance_in_near_with_four_decimals(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class StatusNear:
        def __init__(self, network: str, *, rpc_url: str) -> None:
            assert network == "testnet"

        def access_keys(self, account_id: str) -> list[Any]:
            if account_id == "keeper.me.testnet":
                raise AccountNotFoundError(account_id)
            return [] if account_id == "vault.me.testnet" else [object()]

        def balance(self, account_id: str) -> Amount:
            return Amount.yocto(4_799_912_345_678_901_234_567_891)

    monkeypatch.setattr(tools, "Near", StatusNear)
    tools._save({"root_account": "me.testnet", "vault_account": "vault.me.testnet",
                 "keeper_account": "keeper.me.testnet"})
    tools.main(["status"])
    assert capsys.readouterr().out.splitlines() == [
        "me.testnet: 4.7999 NEAR, 1 access key(s)",
        "vault.me.testnet: 4.7999 NEAR, 0 access key(s)",
        "keeper.me.testnet: not found",
    ]


def test_a_rate_limited_faucet_is_asked_once_and_the_run_says_when_to_try_again(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """On testnet, topup asked four times, 20 s apart, a faucet that had said to wait 10 hours."""
    chain, sleeps = FakeTestnet(me_testnet=4.7338), Sleeps()
    chain.answer = "rate limited"
    with pytest.raises(SystemExit) as info:
        tools.topup("me.testnet", 5 * NEAR, http=chain.client(), sleep=sleeps)
    assert chain.requests == ["me.testnet"] and sleeps == []
    message = str(info.value)
    assert "rate limited" in message and "retry in 10 hours" in message and "it was not asked again" in message
    assert "Try again after " in message and " UTC on " in message
    assert "HTTP 500 Failed to create" in capsys.readouterr().out


class FakeNear:
    """near-kit's sync client as bootstrap uses it: the registrar call creates the root on the fake chain."""

    chain: FakeTestnet
    created: list[tuple[str, str]] = []

    def __init__(self, network: str, *, rpc_url: str, account_id: str = "", private_key: str = "") -> None:
        self.signer = (account_id, private_key)

    def call(self, contract: str, method: str, args: dict[str, Any], **kwargs: Any) -> Any:
        assert (contract, method) == ("testnet", "create_account")
        type(self).created.append(self.signer)
        self.chain.balances[self.signer[0]] -= 48 * NEAR // 10
        self.chain.balances[args["new_account_id"]] = 48 * NEAR // 10
        self.key = args["new_public_key"]
        type(self).keys = [type("Key", (), {"public_key": self.key})()]
        return type("Result", (), {"transaction_hash": "create-root"})()

    def access_keys(self, account_id: str) -> list[Any]:
        return type(self).keys


@pytest.fixture
def registrar(monkeypatch: pytest.MonkeyPatch) -> type[FakeNear]:
    FakeNear.created = []
    monkeypatch.setattr(tools, "Near", FakeNear)
    return FakeNear


def test_bootstrap_stops_at_once_when_the_faucet_is_rate_limited(registrar: type[FakeNear]) -> None:
    chain, sleeps = FakeTestnet(), Sleeps()
    chain.answer = "rate limited"
    with pytest.raises(SystemExit, match="rate limited .*Try again after"):
        tools.bootstrap("me.testnet", http=chain.client(), sleep=sleeps)
    assert len(chain.requests) == 1 and sleeps == []  # no minute of waiting for money that cannot come
    saved = json.loads(tools.CREDS.read_text())
    assert saved["funder_implicit_account"] == chain.requests[0] and saved["funder_private_key"]
    assert registrar.created == []


def test_bootstrap_reuses_a_funding_account_that_still_holds_its_test_near(registrar: type[FakeNear]) -> None:
    """A run that stopped after the faucet funded the funding account must not ask for it again."""
    funder = generate_key()
    implicit = funder.public_key.data.hex()
    tools._save({"network": "testnet", "funder_implicit_account": implicit, "funder_private_key": funder.secret_key})
    chain, sleeps = FakeTestnet(), Sleeps()
    chain.balances[implicit] = 5 * NEAR
    registrar.chain = chain
    tools.bootstrap("me.testnet", http=chain.client(), sleep=sleeps)
    assert registrar.created == [(implicit, funder.secret_key)]
    assert chain.requests == ["me.testnet"] * 2  # the two drops for the budget; none for a new funding account
    saved = json.loads(tools.CREDS.read_text())
    assert saved["funder_private_key"] == funder.secret_key and saved["root_account"] == "me.testnet"


def test_bootstrap_tops_up_only_what_an_unfinished_deployment_still_needs() -> None:
    """After a deployment that stopped halfway, the root needs less than the whole budget."""
    done = ["create token", "deploy and initialise token", "create vault", "deploy and initialise vault"]
    unfinished = {"network": "testnet", "profile": "hosted", "root_account": "me.testnet",
                  "keys": DeployKeys().secret_keys(), "transactions": [[label, "h"] for label in done]}
    tools._save({"network": "testnet", "root_account": "me.testnet", "treasury_private_key": "ed25519:x",
                 UNFINISHED: unfinished})
    full, rest = tools.hosted_budget("me.testnet"), tools.hosted_budget("me.testnet", done)
    assert 5 * NEAR < full - rest < 6 * NEAR  # the token's and the vault's funding
    chain = FakeTestnet(me_testnet=7)  # less than the whole budget, more than the rest
    tools.bootstrap("me.testnet", http=chain.client(), sleep=Sleeps())
    assert chain.requests == []
