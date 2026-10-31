"""Training fixture pages: fictional, labelled, not indexed, and nothing to type into."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from test_bot_fakes import FakeEngine, make_settings

from holdup.chain.fake import FakeVault
from holdup.config import Settings
from holdup.store import Store
from holdup.web.app import create_app
from holdup.web.fixtures import PAGES

FIXTURE_PATHS = ["/fixtures", "/fixtures/", *(f"/fixtures/{path}" for path in PAGES)]
FORM_ELEMENTS = re.compile(r"<\s*(form|input|textarea|select|button)\b", re.IGNORECASE)


def client_for(settings: Settings) -> TestClient:
    return TestClient(create_app(settings, FakeEngine(settings), FakeVault.from_settings(settings), Store(":memory:")))


@pytest.fixture
def client() -> Iterator[TestClient]:
    with client_for(make_settings()) as client:
        yield client


@pytest.mark.parametrize("path", FIXTURE_PATHS)
def test_fixture_pages_are_labelled_and_inert(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    page = response.text
    assert '<div class="fixture-banner" role="note">' in page
    assert "TRAINING FIXTURE - fictional" in page
    assert '<meta name="robots" content="noindex, nofollow">' in page
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert not FORM_ELEMENTS.search(page)
    assert "<script" not in page


def test_the_phishing_imitation_asks_for_a_recovery_phrase(client: TestClient) -> None:
    page = client.get("/fixtures/coinhaven/verify").text
    assert "CoinHaven" in page
    assert "Enter your 12-word recovery phrase" in page
    assert page.count('class="ch-field"') == 12
    assert "frozen in 30 minutes" in page


def test_the_supplier_pages_show_the_real_details(client: TestClient) -> None:
    home = client.get("/fixtures/northwind").text
    assert "Northwind Paper Co." in home
    assert "northwindpaper.testnet" in home
    assert "We never change bank or wallet details by email or chat." in home
    invoice = client.get("/fixtures/northwind/invoices/NP-2291").text
    assert all(amount in invoice for amount in ("600.00", "350.00", "200.00", "1,150.00"))


def test_supplier_account_follows_the_network() -> None:
    with client_for(make_settings(network="testnet", root_account="holdup.testnet")) as client:
        assert "northwindpaper.holdup.testnet" in client.get("/fixtures/northwind").text


def test_the_index_lists_every_fixture(client: TestClient) -> None:
    index = client.get("/fixtures/").text
    for path in PAGES:
        assert f'href="/fixtures/{path}"' in index
