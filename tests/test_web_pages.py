"""Public pages over the in-memory vault and a real store, with a fake engine."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_bot_fakes import HUSD, NOW_MS, FakeEngine, make_settings

from holdup.chain.fake import FakeVault, SimClock
from holdup.chain.verify import Check, VerifyReport
from holdup.config import Settings
from holdup.models import Finding, Hold, Reason, StepRecord, UserAccount, WhyView
from holdup.store import Store
from holdup.web import pages
from holdup.web.app import create_app
from holdup.web.signing import export_token

HOUR = 3_600_000
PAYEE = "safe-wallet-77.testnet"
SENDER = UserAccount("tg:111", "usender01.holdup.testnet", "ed25519:placeholder", display_name="Priya")
PREVIEW = "PRIVATE-PREVIEW-TEXT"


def canonical(doc: dict[str, Any]) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


@dataclass
class World:
    settings: Settings
    clock: SimClock
    vault: FakeVault
    store: Store
    engine: FakeEngine
    client: TestClient

    def run(self, coro: Any) -> Any:
        return asyncio.run(coro)

    def braked_hold(self) -> Hold:
        """A payment to a new payee, braked once by the rules and once by the full check."""
        sent = self.run(self.vault.send(SENDER, PAYEE, HUSD, 50 * 10**6))
        hold = sent.hold
        self.store.record_tx(hold.id, "send", sent.tx.tx_hash)
        self.store.save_context(hold.id, {"has_text": True, "text_chars": 279, "preview": PREVIEW, "has_image": False})
        findings = [
            Finding(Reason.NEW_ACCOUNT, "created 2 h ago", "rules"),
            Finding(
                Reason.SECRET_REQUEST,
                "the linked page asks for a 12 word recovery phrase",
                "web_fetch",
                citations=["http://testserver/fixtures/coinhaven/verify"],
            ),
        ]
        steps = [
            StepRecord(
                "triage",
                "openai/gpt-oss-20b",
                "deepinfra",
                False,
                1,
                "req-triage",
                0.000067,
                812,
                guardrail={"profile": "checker-injection", "mode": "monitor", "valid": False},
                prompt_sha256="c0f2e62f40e6c23c" * 4,
            ),
            StepRecord(
                "investigator",
                "openai/gpt-oss-120b",
                "deepinfra",
                True,
                2,
                "req-inv",
                0.0215,
                9870,
                tool_calls=[{"type": "web_fetch"}, {"name": "payee_profile", "server_label": "near-intel"}],
            ),
        ]
        for index, (reasons, until) in enumerate([(findings[:1], HOUR), (findings, 24 * HOUR)]):
            doc = canonical(
                {
                    "v": 1,
                    "hold": hold.id,
                    "decided_ms": NOW_MS + 1000 * (index + 1),
                    "reasons": [f.to_dict() | {"bit": int(f.reason)} for f in reasons],
                    "steps": [{"step": s.step, "model": s.model} for s in steps] if index else [],
                }
            )
            bits = sum(int(f.reason) for f in reasons)
            tx = self.run(self.vault.extend_hold(hold.id, hold.created_ms + until, bits, doc))
            self.store.save_evidence(hold.id, doc, hashlib.sha256(doc.encode()).hexdigest())
            self.store.record_tx(hold.id, "extend", tx.tx_hash)
        self.store.record_mcp_call(hold.id, "payee_profile", {"account_id": PAYEE})
        current = self.run(self.vault.get_hold(hold.id))
        last = self.store.get_evidence(hold.id)
        self.engine.why_views[hold.id] = WhyView(
            hold=current,
            evidence_json=last.evidence_json if last else None,
            evidence_hash=current.evidence_hash,
            findings=findings,
            steps=steps,
        )
        return current

    def refresh(self, hold_id: int) -> None:
        view = self.engine.why_views[hold_id]
        view.hold = self.run(self.vault.get_hold(hold_id))


@pytest.fixture
def world() -> Iterator[World]:
    settings = make_settings()
    clock = SimClock(NOW_MS)
    vault = FakeVault.from_settings(settings, clock=clock)
    vault.create_account(PAYEE, age_ms=2 * HOUR)
    asyncio.run(vault.onboard_user(SENDER, settings.demo_funds))
    store = Store(":memory:", clock=clock)
    store.upsert_user(SENDER.user_key, SENDER.account_id, "111", SENDER.display_name)
    engine = FakeEngine(settings)
    app = create_app(settings, engine, vault, store)
    with TestClient(app) as client:
        yield World(settings, clock, vault, store, engine, client)


def visible(page: str) -> str:
    """The page's text as a person reads it."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page)))


# ----------------------------------------------------------------------
# landing, health and headers
# ----------------------------------------------------------------------


def test_landing_page(world: World) -> None:
    response = world.client.get("/")
    assert response.status_code == 200
    text = visible(response.text)
    assert "Give AI the brakes, not the keys." in text
    assert 'href="/console"' in response.text and 'href="/prove"' in response.text
    assert "12 payments" in text and "9 brakes" in text and "3 cancelled after a warning" in text
    assert "It is not Circle USDC." in text


def test_every_page_carries_strict_security_headers(world: World) -> None:
    response = world.client.get("/")
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "<script>" not in response.text and "style=" not in response.text


def test_health_robots_and_favicon(world: World) -> None:
    health = world.client.get("/health").json()
    assert health == {
        "ok": True,
        "version": health["version"],
        "network": "offline",
        "vault": "vault.holdup.testnet",
        "telegram": False,
        "mcp": False,
        "guardrail": False,
    }
    robots = world.client.get("/robots.txt").text
    assert "Disallow: /fixtures/" in robots and "Disallow: /console" in robots
    favicon = world.client.get("/favicon.ico", follow_redirects=False)
    assert favicon.status_code == 301 and favicon.headers["location"] == "/static/favicon.svg"
    assert world.client.get("/static/style.css").status_code == 200


def test_unknown_pages_are_friendly(world: World) -> None:
    page = world.client.get("/nothing-here")
    assert page.status_code == 404
    assert "There is nothing at this address." in page.text
    assert page.headers["content-type"].startswith("text/html")
    missing = world.client.get("/h/404")
    assert missing.status_code == 404 and "There is no payment #404." in missing.text
    data = world.client.get("/h/404.json")
    assert data.status_code == 404 and data.json() == {"error": "there is no payment #404"}


# ----------------------------------------------------------------------
# the hold page
# ----------------------------------------------------------------------


def test_hold_page_explains_a_brake(world: World) -> None:
    hold = world.braked_hold()
    response = world.client.get(f"/h/{hold.id}")
    assert response.status_code == 200
    text = visible(response.text)
    assert "50 HUSD" in text and PAYEE in text and SENDER.account_id in text
    assert "Brakes on" in text
    assert "Releases at 3 Nov 09:00 UTC" in text
    assert "Hold length 24 h cap 3 days" in text
    assert "Payee account is brand new created 2 h ago [rules]" in text
    assert "[web fetch]" in text and "http://testserver/fixtures/coinhaven/verify" in text
    titles = re.findall(r'<span class="tl-title">([^<]+)</span>', response.text)
    assert titles == ["Payment held", "Quick rules check: brakes on", "Full check: brakes on", "Waiting"]
    assert "Also confirmed 1 earlier reason." in text
    assert text.count("simulated") >= 3  # every transaction id is marked simulated offline
    assert f'data-target-ms="{hold.release_at_ms}"' in response.text
    assert 'name="robots" content="noindex"' in response.text


def test_hold_page_verifies_the_evidence_hash(world: World) -> None:
    hold = world.braked_hold()
    text = visible(world.client.get(f"/h/{hold.id}").text)
    assert f"On-chain evidence_hash {hold.evidence_hash}" in text
    assert f"sha256 of the published JSON {hold.evidence_hash}" in text
    assert "They match" in text
    assert f"curl -s http://testserver/v/{hold.id}.json | sha256sum" in text


def test_hold_page_never_shows_the_forwarded_message(world: World) -> None:
    hold = world.braked_hold()
    page = world.client.get(f"/h/{hold.id}").text
    assert "The sender shared a message (279 characters)" in visible(page)
    assert PREVIEW not in page
    assert PREVIEW not in world.client.get(f"/why/{hold.id}").text
    assert PREVIEW not in world.client.get(f"/v/{hold.id}.json").text


def test_hold_page_after_a_cancel(world: World) -> None:
    hold = world.braked_hold()
    tx = world.run(world.vault.cancel(SENDER, hold.id))
    world.store.record_tx(hold.id, "cancel", tx.tx_hash)
    world.refresh(hold.id)
    page = world.client.get(f"/h/{hold.id}").text
    text = visible(page)
    assert "Cancelled" in text
    assert "Cancelled by the sender. 50 HUSD went back to the sender's vault balance." in text
    assert "data-countdown" not in page and "data-active" not in page


def test_hold_json_signature_changes_with_the_hold(world: World) -> None:
    hold = world.braked_hold()
    page = world.client.get(f"/h/{hold.id}").text
    first = world.client.get(f"/h/{hold.id}.json").json()
    assert f'data-signature="{first["signature"]}"' in page
    assert first["hold"]["status"] == "held" and first["now_ms"] == NOW_MS
    world.run(world.vault.cancel(SENDER, hold.id))
    assert world.client.get(f"/h/{hold.id}.json").json()["signature"] != first["signature"]


# ----------------------------------------------------------------------
# evidence and AI steps
# ----------------------------------------------------------------------


def test_evidence_is_served_exactly_as_hashed(world: World) -> None:
    hold = world.braked_hold()
    response = world.client.get(f"/v/{hold.id}.json")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    digest = hashlib.sha256(response.content).hexdigest()
    assert digest == hold.evidence_hash
    assert response.headers["x-evidence-sha256"] == digest
    assert response.headers["x-onchain-evidence-hash"] == hold.evidence_hash


def test_earlier_evidence_by_hash(world: World) -> None:
    hold = world.braked_hold()
    first = world.store.list_evidence(hold.id)[0]
    response = world.client.get(f"/v/{hold.id}.json", params={"sha256": first.evidence_hash})
    assert response.content.decode() == first.evidence_json
    assert world.client.get(f"/v/{hold.id}.json", params={"sha256": "00" * 32}).status_code == 404
    assert world.client.get("/v/999.json").json() == {"error": "no evidence recorded for payment #999"}


def test_why_page_lists_every_step(world: World) -> None:
    hold = world.braked_hold()
    text = visible(world.client.get(f"/why/{hold.id}").text)
    assert "openai/gpt-oss-20b" in text and "openai/gpt-oss-120b" in text
    assert "Provider deepinfra" in text
    assert "Guardrail checker-injection, monitor: flagged" in text
    assert "Tools web_fetch, payee_profile (near-intel)" in text
    assert "Attempts 2" in text and "Latency 9,870 ms" in text and "Cost $0.0215" in text
    assert "2 model calls" in text and "1 fallback" in text
    assert f"payee_profile({PAYEE})" in text
    assert "c0f2e62f40e6c23c" in text


# ----------------------------------------------------------------------
# proof, stats and export
# ----------------------------------------------------------------------


def test_prove_page_offline_is_a_labelled_simulation(world: World) -> None:
    text = visible(world.client.get("/prove").text)
    assert "Simulated sample." in text
    assert "All checks pass" in text
    assert "The vault has no access keys" in text and "The AI's oracle can only add time" in text
    for error in ("MethodNameMismatch", "RequiresFullAccess", "ReceiverMismatch", "DepositWithFunctionCall"):
        assert error in text
    assert "not refused" not in text
    proof = world.client.get("/prove.json").json()
    assert proof["simulated"] is True and proof["ok"] is True
    assert {c["title"] for c in proof["checks"]} >= {"The vault runs the published code", "The rules are fixed"}


class LiveVault:
    """Just enough of a network vault for the proof page."""

    simulated = False
    vault_id = "vault.holdup.testnet"


def live_world(monkeypatch: pytest.MonkeyPatch, outcome: VerifyReport | Exception) -> tuple[TestClient, list[int]]:
    calls: list[int] = []

    async def fake_verify(settings: Settings) -> VerifyReport:
        calls.append(1)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(pages, "verify_deploy", fake_verify)
    settings = make_settings(network="testnet", rpc_url="https://rpc.testnet.near.org")
    app = create_app(settings, FakeEngine(settings), LiveVault(), Store(":memory:"))  # type: ignore[arg-type]
    return TestClient(app), calls


def test_prove_page_live_checks_are_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    report = VerifyReport(
        "testnet",
        "vault.holdup.testnet",
        [Check("vault_has_no_keys", True, "no access keys", "no keys"), Check("oracle_key", False, "1 key", "2 keys")],
    )
    client, calls = live_world(monkeypatch, report)
    with client:
        text = visible(client.get("/prove").text)
        client.get("/prove.json")
    assert calls == [1]
    assert "Simulated sample" not in text
    assert "Some checks fail" in text
    assert "holdup prove signs each of these" in text
    assert "view_access_key_list" in text and "https://rpc.testnet.near.org" in text


def test_prove_page_reports_an_unreachable_node(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = live_world(monkeypatch, ConnectionError("down"))
    with client:
        response = client.get("/prove")
    assert response.status_code == 200
    assert "Could not reach the NEAR RPC node (ConnectionError)" in response.text


def test_stats_page_and_json(world: World) -> None:
    text = visible(world.client.get("/stats").text)
    assert "3 Cancelled after a warning counted on-chain against the payee" in text
    assert "$0.0213 Total AI cost" in text
    assert world.client.get("/stats.json").json() == world.engine.stats_value


def test_signed_export_link(world: World) -> None:
    token = export_token(world.settings.secret, SENDER.account_id, 2**62)
    response = world.client.get(f"/export/{token}.csv")
    assert response.status_code == 200
    assert response.text == world.engine.csv
    assert response.headers["content-type"].startswith("text/csv")
    assert f'filename="holdup-{SENDER.account_id}.csv"' in response.headers["content-disposition"]
    assert world.engine.called("export_csv") == [(SENDER.user_key,)]


@pytest.mark.parametrize(
    "token",
    [
        "garbage",
        export_token("test-secret", SENDER.account_id, 1),  # expired
        export_token("another-secret", SENDER.account_id, 2**62),  # signed with another key
        export_token("test-secret", "stranger.holdup.testnet", 2**62),  # nobody we know
    ],
)
def test_bad_export_links_are_refused(world: World, token: str) -> None:
    response = world.client.get(f"/export/{token}.csv")
    assert response.status_code == 404
    assert "invalid or has expired" in response.json()["error"]
    assert world.engine.called("export_csv") == []


def test_unexpected_errors_get_a_friendly_answer(world: World, caplog: pytest.LogCaptureFixture) -> None:
    world.engine.fail["why"] = RuntimeError("rpc exploded")
    world.engine.fail["stats"] = RuntimeError("store exploded")
    client = TestClient(world.client.app, raise_server_exceptions=False)
    page = client.get("/h/1")
    assert page.status_code == 500
    assert "Something went wrong on our side." in page.text and "exploded" not in page.text
    data = client.get("/stats.json")
    assert data.status_code == 500
    assert data.json() == {"error": "Something went wrong on our side. Please try again in a moment."}
    assert "GET /h/1 failed" in caplog.text
