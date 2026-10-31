"""The client against responses recorded from the hosted Otari gateway (fixtures/otari/recorded)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from holdup.config import Settings
from holdup.risk import otari as O

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "otari" / "recorded"


def load(name: str) -> dict[str, Any]:
    return json.loads((RECORDED / name).read_text())


def replay(document: dict[str, Any]) -> httpx.Response:
    """The recorded response, byte for byte where the recording kept the bytes."""
    headers = dict(document["response_headers"])
    if document.get("stream_chunks"):
        events = "".join(f"data: {json.dumps(c) if c != '[DONE]' else c}\n\n" for c in document["stream_chunks"])
        return httpx.Response(document["status"], headers=headers, content=events.encode())
    body = document["response_body"]
    if isinstance(body, str):
        return httpx.Response(document["status"], headers=headers, content=body.encode())
    headers.pop("content-length", None)
    return httpx.Response(document["status"], headers=headers, json=body)


def client_answering(document: dict[str, Any], seen: list[dict[str, Any]] | None = None) -> O.OtariClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return replay(document)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return O.OtariClient(Settings(otari_api_key="otk_v1_us_test"), http=http)


def test_every_recording_is_real_and_holds_no_key() -> None:
    files = sorted(RECORDED.glob("*.json"))
    assert len(files) >= 10
    for path in files:
        text = path.read_text()
        document = json.loads(text)
        assert document["synthetic"] is False, path.name
        assert "otk_v1_" not in text and "Bearer otk" not in text, path.name


async def test_a_real_code_execution_stream() -> None:
    document = load("tools-codeexec-chat-stream.json")
    text, record = await client_answering(document).chat("investigator", O.INVESTIGATOR_MODEL, [], stream=True)
    assert "1377.4082919660768" in text and "730.0" in text
    assert record.provider == "deepinfra" and record.fallback is False and record.attempts == 1
    assert record.cost_usd == pytest.approx(0.01004)  # tokens plus one sandbox run
    assert record.tool_calls == []  # sandbox runs are not reported on Chat Completions
    assert record.request_id == document["response_headers"]["otari-request-id"]


async def test_a_real_monitor_verdict_without_a_service_is_inconclusive() -> None:
    document = load("guardrails-monitor-prompt-injection-benign.json")
    text, record = await client_answering(document).chat("triage", O.INVESTIGATOR_MODEL, [])
    assert text == "OK"
    assert record.guardrail == {"profile": "prompt-injection", "mode": "monitor", "valid": None, "score": None,
                                "verdict": "inconclusive"}


@pytest.mark.parametrize(
    ("name", "kind", "label"),
    [
        ("guardrails-block-prompt-injection-benign.json", O.GuardrailUnavailable, "guardrail_unavailable"),
        ("tools-webfetch-chat.json", O.OtariRequestError, "request_refused"),
        ("mcp-chat-near-docs-404.json", O.AllCandidatesFailed, "all_candidates_failed"),
        ("errors-invalid-param-value.json", O.AllCandidatesFailed, "all_candidates_rejected"),
        ("org-keys-list-own.json", O.OtariRequestError, "request_refused"),
    ],
)
async def test_real_refusals_map_to_the_right_errors(name: str, kind: type[O.OtariError], label: str) -> None:
    with pytest.raises(kind) as caught:
        await client_answering(load(name)).chat("triage", O.TRIAGE_MODEL, [])
    assert caught.value.label == label


@pytest.mark.parametrize(
    ("name", "tools"),
    [
        ("tools-websearch-chat.json", {O.WEB_SEARCH}),
        ("tools-webfetch-chat-typeonly.json", {O.WEB_SEARCH, O.WEB_FETCH}),
    ],
)
async def test_real_workspace_tool_switches_name_the_tools(name: str, tools: set[str]) -> None:
    with pytest.raises(O.ToolNotEnabled) as caught:
        await client_answering(load(name)).chat("investigator", O.INVESTIGATOR_MODEL, [], tools=[{"type": O.WEB_SEARCH}])
    assert caught.value.tools == tools


async def test_the_real_firewall_page_is_retried_and_then_given_up() -> None:
    seen: list[dict[str, Any]] = []
    with pytest.raises(O.WafBlocked):
        await client_answering(load("waf-backticks-api.json"), seen).chat(
            "triage", O.TRIAGE_MODEL, [{"role": "user", "content": "x"}]
        )
    assert len(seen) == 2


async def test_a_real_reasoning_answer_cut_short_is_bad_output() -> None:
    with pytest.raises(O.BadModelOutput, match="ran out of tokens"):
        await client_answering(load("core-gpt-oss-20b.json")).chat("triage", O.TRIAGE_MODEL, [])


async def test_a_real_bedrock_refusal_of_reasoning_effort_is_retried_without_it() -> None:
    refusal = load("errors-reasoning-effort-on-bedrock.json")
    answer = load("json-schema-gpt-oss-20b.json")
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return replay(refusal if "reasoning_effort" in body else answer)

    client = O.OtariClient(Settings(otari_api_key="otk_v1_us_test"), http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    text, _ = await client.chat("triage", O.TRIAGE_MODEL, [], reasoning_effort="low")
    assert json.loads(text)["risk"] == "high"
    assert ["reasoning_effort" in body for body in seen] == [True, False]
