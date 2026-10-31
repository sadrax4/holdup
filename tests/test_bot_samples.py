"""Training samples for /demo and the console, adapted to the server they run on."""

from __future__ import annotations

from pathlib import Path

from test_bot_fakes import make_settings

from holdup.bot.samples import (
    demo_account,
    find_sample,
    for_person,
    friend_account,
    load_samples,
    stable_symbol,
)
from holdup.chain.profiles import FRIENDS
from holdup.models import NEAR


def test_offline_samples_use_the_demo_token_and_this_server() -> None:
    samples = load_samples(make_settings())
    assert {s.id for s in samples} >= {"hero", "invoice", "benign"}
    hero = find_sample(samples, "hero")
    assert hero is not None
    assert hero.sender == "CoinHaven Support"
    assert hero.command == "send 50 HUSD to safe-wallet-77.testnet"
    assert "http://testserver/fixtures/coinhaven/verify" in hero.text
    assert "{base_url}" not in hero.text
    invoice = find_sample(samples, "invoice")
    assert invoice is not None and invoice.trusted == ("northwindpaper.testnet",)
    benign = find_sample(samples, "benign")
    assert benign is not None and not benign.scam and benign.command == "send 12.5 HUSD to ravi.testnet"


def test_sample_links_use_the_training_page_host_when_one_is_set() -> None:
    """FIXTURE_BASE_URL was documented but unused: the phishing sample linked to the bot's own host."""
    settings = make_settings(public_base_url="https://holdup.example", fixture_base_url="https://fixtures.example")
    hero = find_sample(load_samples(settings), "hero")
    assert hero is not None and "https://fixtures.example/fixtures/coinhaven/verify" in hero.text
    assert "holdup.example" not in hero.text


def test_on_a_network_the_demo_payees_live_under_the_root() -> None:
    settings = make_settings(network="testnet", root_account="holdup.testnet")
    hero = find_sample(load_samples(settings), "hero")
    assert hero is not None
    assert hero.command == "send 50 HUSD to safe-wallet-77.holdup.testnet"
    assert "safe-wallet-77.holdup.testnet" in hero.text
    assert demo_account(settings, "northwindpaper.testnet") == "northwindpaper.holdup.testnet"
    assert demo_account(settings, "someone-else.testnet") == "someone-else.testnet"


def test_each_person_has_a_practice_friend_of_their_own_on_a_network() -> None:
    settings = make_settings(network="testnet", root_account="holdup.testnet")
    friends = {friend_account(settings, f"tg:{n}") for n in range(200)}
    assert friends == {f"{label}.holdup.testnet" for label in FRIENDS}  # all ten get people
    assert friend_account(settings, "tg:7") == friend_account(settings, "tg:7")  # always the same one
    assert friend_account(settings) == "ravi.holdup.testnet"  # nobody in particular (the landing page)
    assert friend_account(make_settings(), "tg:7") == "ravi.testnet"  # offline, the world's own ravi
    benign = find_sample(load_samples(settings), "benign")
    assert benign is not None
    own = for_person(benign, settings, "tg:7")
    assert own.command == f"send 12.5 HUSD to {friend_account(settings, 'tg:7')}"
    hero = find_sample(load_samples(settings), "hero")
    assert hero is not None and for_person(hero, settings, "tg:7") == hero  # nothing to change there


def test_stable_symbol_prefers_what_people_are_funded_with() -> None:
    assert stable_symbol(make_settings()) == "HUSD"
    usdc_only = make_settings(husd_token=None, usdc_token="usdc.fakes.testnet")
    usdc_only.demo_funds = {NEAR: 1}
    assert stable_symbol(usdc_only) == "USDC"
    near_only = make_settings(husd_token=None)
    near_only.demo_funds = {NEAR: 1}
    assert stable_symbol(near_only) == "NEAR"


def test_missing_samples_are_not_fatal(tmp_path: Path) -> None:
    assert load_samples(make_settings(), tmp_path) == []
