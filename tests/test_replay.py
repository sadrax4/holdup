"""The replay transport and the recorded scenarios behind the offline demo."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from holdup.config import Settings
from holdup.risk.otari import INVESTIGATOR_MODEL, OtariClient, OtariRequestError
from holdup.risk.prompts.contracts import InvestigatorResult, ReviewerResult, TriageResult, VisionResult
from holdup.risk.replay import FIXTURES_DIR, ReplayTransport, available_scenarios, replay_client
from holdup.runtime import ScenarioResolver

REQUIRED = {
    "hero",
    "invoice",
    "benign",
    "injection-only",
    "budget_exceeded",
    "guardrail_violation",
    "guardrail_unavailable",
    "all_candidates_failed",
    "waf_html_403",
}
CONTRACTS = {
    "triage": TriageResult,
    "vision": VisionResult,
    "investigator": InvestigatorResult,
    "reviewer": ReviewerResult,
}


def body(step: str, text: str = "hello", **extra: Any) -> dict[str, Any]:
    return {"model": "m", "messages": [{"role": "user", "content": text}], "metadata": {"step": step}, **extra}


async def post(transport: ReplayTransport, payload: dict[str, Any]) -> httpx.Response:
    async with httpx.AsyncClient(transport=transport) as http:
        return await http.post("https://api.otari.ai/api/v1/chat/completions", json=payload)


def recorded_responses() -> list[tuple[str, str, dict[str, Any]]]:
    out = []
    for name in sorted(REQUIRED):
        steps = json.loads((FIXTURES_DIR / f"{name}.json").read_text())["steps"]
        for step, entry in steps.items():
            answers = entry.get("by_model", {}).values() if "by_model" in entry else [entry["response"]]
            out += [(name, step, answer) for answer in answers]
    return out


def test_every_required_scenario_is_recorded_and_marked() -> None:
    assert set(available_scenarios()) >= REQUIRED
    for path in FIXTURES_DIR.glob("*.json"):
        text = path.read_text()
        document = json.loads(text)
        assert document["synthetic"] is True and document["description"] and document["source"], path.name
        assert "otk_v1_" not in text and "Bearer " not in text, path.name


@pytest.mark.parametrize(("scenario", "step", "answer"), recorded_responses())
def test_recorded_answers_follow_the_step_contracts(scenario: str, step: str, answer: dict[str, Any]) -> None:
    if answer.get("status", 200) != 200 or step not in CONTRACTS:
        return
    CONTRACTS[step].model_validate(answer["content"])
    if "otari-guardrails" in answer.get("headers", {}):
        assert isinstance(answer["headers"]["otari-guardrails"], list)


async def test_requests_are_routed_by_step_and_scenario() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    def resolver(step: str, payload: dict[str, Any]) -> str:
        seen.append((step, payload))
        return "invoice"

    transport = ReplayTransport(resolver)
    response = await post(transport, body("triage", "invoice please"))
    content = json.loads(response.json()["choices"][0]["message"]["content"])
    assert content["details_changed"] is True and response.headers["otari-request-id"] == "replay-invoice-triage"
    assert seen[0][0] == "triage" and seen[0][1]["messages"][0]["content"] == "invoice please"
    assert transport.requests[0].scenario == "invoice" and transport.requests[0].status == 200


async def test_missing_steps_fall_back_to_the_benign_answers_and_wildcards_cover_all_steps() -> None:
    hero = ReplayTransport("hero")
    vision = await post(hero, body("vision"))
    assert vision.headers["otari-request-id"] == "replay-benign-vision"
    budget = ReplayTransport("budget_exceeded")
    for step in ("triage", "investigator", "reviewer"):
        refused = await post(budget, body(step))
        assert refused.status_code == 403 and refused.headers["otari-error-code"] == "budget_exceeded"


async def test_answers_can_differ_by_model() -> None:
    transport = ReplayTransport("all_candidates_failed")
    first = await post(transport, body("investigator", model=INVESTIGATOR_MODEL))
    last = await post(transport, body("investigator", model="z-ai/glm-5.3-flash"))
    assert first.status_code == 502 and last.status_code == 200


async def test_unknown_steps_answer_404(tmp_path: Path) -> None:
    (tmp_path / "only.json").write_text(json.dumps({"scenario": "only", "synthetic": True, "steps": {}}))
    transport = ReplayTransport("only", fixtures_dir=tmp_path, default="only")
    response = await post(transport, body("triage"))
    assert response.status_code == 404 and "no recorded response" in response.json()["detail"]


async def test_strict_replay_refuses_requests_shaped_unlike_the_recording() -> None:
    lenient = ReplayTransport("hero")
    assert (await post(lenient, body("investigator"))).status_code == 200
    assert lenient.mismatches and "tool otari_web_search not declared" in lenient.mismatches[0]
    strict = ReplayTransport("hero", strict=True)
    refused = await post(strict, body("investigator"))
    assert refused.status_code == 400 and "MCP server near-intel not declared" in refused.json()["detail"]


async def test_the_firewall_rule_blocks_quotes_until_they_are_replaced() -> None:
    transport = ReplayTransport("waf_html_403")
    blocked = await post(transport, body("triage", "it's here"))
    assert blocked.status_code == 403 and blocked.headers["content-type"] == "text/html"
    passed = await post(transport, body("triage", "it\u2019s here"))
    assert passed.status_code == 200


async def test_streamed_replays_carry_tool_calls_and_cost_through_the_client() -> None:
    client = replay_client(Settings(), "hero")
    text, record = await client.chat(
        "investigator",
        INVESTIGATOR_MODEL,
        [{"role": "user", "content": "x"}],
        metadata={"step": "investigator"},
        stream=True,
    )
    assert json.loads(text)["brake"] is True
    assert [c["name"] for c in record.tool_calls] == ["otari_web_search", "otari_web_search", "otari_web_fetch"]
    assert record.tool_calls[0]["sources"] and record.cost_usd == pytest.approx(0.021523)
    assert client.offline and client.transport is not None


async def test_other_routes_are_not_recorded() -> None:
    client = replay_client(Settings(), "benign")
    with pytest.raises(OtariRequestError):
        await client.rest("GET", "/models")


async def test_the_demo_resolver_picks_scenarios_from_the_payee() -> None:
    resolver = ScenarioResolver()
    transport = ReplayTransport(resolver)
    await post(transport, body("triage", "- payee account: safe-wallet-77.testnet\nForwarded from CoinHaven Support"))
    await post(transport, body("triage", "- payee account: northwind-paper.testnet\nHello from Northwind Paper Co."))
    await post(transport, body("triage", "- payee account: ravi.testnet\nThe Northwind order, and CoinHaven"))
    resolver.current = "injection-only"
    await post(transport, body("triage", "- payee account: ravi.testnet"))
    assert [r.scenario for r in transport.requests] == ["hero", "invoice", "benign", "injection-only"]


async def test_replayed_answers_say_they_are_recorded() -> None:
    transport = ReplayTransport("hero")
    response = await post(transport, body("triage"))
    assert response.headers["otari-provider"] == "deepinfra (recorded)"
    client = replay_client(Settings(), "hero")
    _, record = await client.chat("triage", "openai/gpt-oss-20b", [{"role": "user", "content": "hi"}])
    assert record.provider == "deepinfra (recorded)"


async def test_recorded_citations_point_at_the_link_in_the_message() -> None:
    """The hero recording cited http://127.0.0.1:8000 whatever the message linked (review finding)."""
    transport = ReplayTransport("hero", base_url="https://holdup.example")
    facts = "- links in the message: https://fixtures.example/fixtures/coinhaven/verify, https://b.example/x"
    linked = await post(transport, body("investigator", facts))
    assert "https://fixtures.example/fixtures/coinhaven/verify" in linked.text
    assert "127.0.0.1" not in linked.text and "{base_url}" not in linked.text
    unlinked = await post(transport, body("investigator", "- links in the message: none"))
    assert "https://holdup.example/fixtures/coinhaven/verify" in unlinked.text


def test_a_fixture_without_the_synthetic_flag_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text(json.dumps({"scenario": "bad", "steps": {}}))
    transport = ReplayTransport("bad", fixtures_dir=tmp_path, default="bad")
    with pytest.raises(ValueError, match="synthetic"):
        transport._entry("bad", "triage")


def test_replay_clients_never_need_a_key() -> None:
    client = replay_client(Settings(), "benign")
    assert isinstance(client, OtariClient) and client.base_url == "https://api.otari.ai/api/v1"
