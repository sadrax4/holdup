"""OtariClient: request shapes, header parsing, streaming, error mapping and retries (offline)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from holdup.config import Settings
from holdup.risk import otari as O
from holdup.risk.prompts.contracts import TriageResult

BACKTICK = chr(0x60)
TRIAGE_JSON = {
    "categories": ["personal"],
    "urgency": False,
    "impersonation": False,
    "secret_request": False,
    "details_changed": False,
    "injection_suspected": False,
    "impossible_returns": False,
    "money_claims": [],
    "risk": "low",
    "summary": "ok",
}
SERVED = {
    "otari-provider": "deepinfra",
    "otari-model": "openai/gpt-oss-20b",
    "otari-request-id": "req-1",
    "otari-attempt-count": "1",
    "otari-fallback": "false",
}


def completion(content: Any, *, usage: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    text = content if isinstance(content, str) else json.dumps(content)
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost_usd": "0.000006"},
        **extra,
    }


def sse(events: list[Any]) -> bytes:
    lines = [f"data: {json.dumps(e)}\n\n" for e in events]
    return ("".join(lines) + "data: [DONE]\n\n").encode()


def chunk(delta: dict[str, Any] | None = None, finish: str | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "id": "c",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
        **extra,
    }


class Gateway:
    """A scripted Otari: answers each request with the next response from a handler."""

    def __init__(self, handler: Callable[[dict[str, Any], httpx.Request], httpx.Response]) -> None:
        self.handler = handler
        self.bodies: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.bodies.append(body)
        self.requests.append(request)
        return self.handler(body, request)


def client_for(gateway: Gateway, key: str = "otk_v1_us_test") -> O.OtariClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(gateway))
    return O.OtariClient(Settings(otari_api_key=key), http=http)


def ok(content: Any = TRIAGE_JSON, headers: dict[str, str] | None = None, **extra: Any) -> httpx.Response:
    return httpx.Response(200, headers={**SERVED, **(headers or {})}, json=completion(content, **extra))


# ----------------------------------------------------------------------
# requests and records
# ----------------------------------------------------------------------


async def test_a_plain_call_returns_the_contract_and_a_step_record() -> None:
    guard = json.dumps([{"profile": "checker-injection", "mode": "monitor", "valid": False, "score": 0.91}])
    gateway = Gateway(lambda body, req: ok(headers={"otari-guardrails": guard, "otari-fallback": "true", "otari-attempt-count": "2"}))
    client = client_for(gateway)
    value, record = await client.chat(
        "triage", O.TRIAGE_MODEL, [{"role": "user", "content": "hi"}],
        output=TriageResult, metadata={"step": "triage", "hold": "3"}, prompt_sha256="ab" * 32,
    )
    assert isinstance(value, TriageResult) and value.risk == "low"
    assert record.step == "triage" and record.model == O.TRIAGE_MODEL and record.provider == "deepinfra"
    assert record.fallback is True and record.attempts == 2 and record.request_id == "req-1"
    assert record.cost_usd == pytest.approx(0.000006) and record.latency_ms is not None
    assert record.guardrail == {"profile": "checker-injection", "mode": "monitor", "valid": False, "score": 0.91, "verdict": "flagged"}
    assert record.prompt_sha256 == "ab" * 32

    body = gateway.bodies[0]
    assert body["model"] == O.TRIAGE_MODEL and body["metadata"] == {"step": "triage", "hold": "3"}
    assert body["reasoning_effort"] == "low" and body["max_tokens"] == 2000
    schema = body["response_format"]["json_schema"]
    assert body["response_format"]["type"] == "json_schema" and schema["strict"] is True
    assert schema["schema"]["additionalProperties"] is False
    assert "user" not in body and "stream" not in body
    assert gateway.requests[0].headers["authorization"] == "Bearer otk_v1_us_test"
    assert gateway.requests[0].headers["idempotency-key"].startswith("holdup-")


@pytest.mark.parametrize("model", [O.INJECTION_GUARD_MODEL, O.REVIEWER_MODEL, O.VISION_MODEL, O.SAFETY_GUARD_MODEL])
async def test_reasoning_effort_is_only_sent_where_the_gateway_accepts_it(model: str) -> None:
    gateway = Gateway(lambda body, req: ok("fine"))
    await client_for(gateway).chat("x", model, [{"role": "user", "content": "hi"}], reasoning_effort="low")
    assert "reasoning_effort" not in gateway.bodies[0]


async def test_gateway_fields_go_in_the_body_and_tool_calls_are_streamed() -> None:
    def handler(body: dict[str, Any], req: httpx.Request) -> httpx.Response:
        started = {"id": "otari_ws_1", "type": "web_search", "status": "in_progress", "query": "q"}
        ended = {**started, "status": "completed", "sources": [{"url": "https://a.example/x?y=1", "title": "A"}]}
        fetch = {"id": "otari_wf_1", "type": "web_fetch", "status": "failed", "url": "https://b.example/",
                 "error": {"provider": None, "status": None, "tag": "call_limit", "retryable": False}}
        events = [
            chunk({"role": "assistant", "content": ""}),
            chunk(otari_tool_calls=[started]),
            chunk(otari_tool_calls=[ended]),
            chunk(otari_tool_calls=[fetch]),
            chunk({"content": "{\"findings\": [], "}),
            chunk({"content": "\"brake\": false}"}, "stop"),
            {**chunk(), "choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12, "cost_usd": "0.010040"}},
        ]
        return httpx.Response(200, headers={**SERVED, "content-type": "text/event-stream"}, content=sse(events))

    gateway = Gateway(handler)
    servers = [{"name": "near-intel", "url": "https://h.example/mcp"}]
    guardrails = [{"profile": "checker-injection", "url": "https://h.example/validate", "mode": "monitor", "on": ["input"]}]
    text, record = await client_for(gateway).chat(
        "investigator", O.INVESTIGATOR_MODEL, [{"role": "user", "content": "hi"}],
        tools=[{"type": O.WEB_SEARCH, "max_uses": 3}], mcp_servers=servers, guardrails=guardrails, max_tool_iterations=8,
    )
    assert json.loads(text) == {"findings": [], "brake": False}
    body = gateway.bodies[0]
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["mcp_servers"] == servers and body["guardrails"] == guardrails and body["max_tool_iterations"] == 8
    assert body["tools"] == [{"type": "otari_web_search", "max_uses": 3}] and "response_format" not in body
    assert "idempotency-key" not in gateway.requests[0].headers  # streams ignore it
    assert record.cost_usd == pytest.approx(0.01004)
    assert record.tool_calls == [
        {"name": "otari_web_search", "id": "otari_ws_1", "status": "completed", "query": "q", "sources": ["https://a.example/x?y=1"]},
        {"name": "otari_web_fetch", "id": "otari_wf_1", "status": "failed", "url": "https://b.example/",
         "error": {"tag": "call_limit", "status": None, "provider": None, "retryable": False}},
    ]


async def test_a_contract_answer_wrapped_in_a_code_fence_still_parses() -> None:
    fenced = BACKTICK * 3 + "json\n" + json.dumps(TRIAGE_JSON) + "\n" + BACKTICK * 3
    value, _ = await client_for(Gateway(lambda b, r: ok(fenced))).chat("triage", "m", [], output=TriageResult)
    assert value.summary == "ok"


async def test_answers_that_break_the_contract_or_are_empty_raise_bad_output() -> None:
    gateway = Gateway(lambda b, r: ok({"risk": "low"}))
    with pytest.raises(O.BadModelOutput) as broken:
        await client_for(gateway).chat("triage", "m", [], output=TriageResult)
    assert broken.value.label == "bad_json" and broken.value.record.error == "bad_json"

    empty = completion("")
    empty["choices"][0]["finish_reason"] = "length"
    with pytest.raises(O.BadModelOutput, match="ran out of tokens"):
        await client_for(Gateway(lambda b, r: httpx.Response(200, json=empty))).chat("triage", "m", [])


# ----------------------------------------------------------------------
# error mapping
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "headers", "body", "kind", "label"),
    [
        (403, {"otari-error-code": "budget_exceeded", "otari-budget-scope": "api_token", "retry-after": "60"},
         {"detail": "spent", "code": "budget_exceeded"}, O.BudgetExceeded, "budget_exceeded"),
        (403, {}, {"message": "Request blocked by guardrail policy.", "code": "guardrail_violation",
                   "guardrails": [{"profile": "checker-injection", "explanation": None, "score": 1.0}]},
         O.GuardrailViolation, "guardrail_violation"),
        (502, {}, {"detail": "guardrail profile checker-injection requested but no guardrails service is configured."},
         O.GuardrailUnavailable, "guardrail_unavailable"),
        (402, {"otari-error-code": "insufficient_funds"}, {"detail": "no funds"}, O.InsufficientFunds, "insufficient_funds"),
        (403, {}, {"detail": "web search is not enabled for this workspace"}, O.ToolNotEnabled, "tool_not_enabled"),
        (400, {"otari-error-code": "all_candidates_rejected"}, {"detail": "No model accepted the request (2 attempts)."},
         O.AllCandidatesFailed, "all_candidates_rejected"),
        (400, {"otari-error-code": "invalid_model"}, {"detail": "Unknown model"}, O.ModelUnavailable, "invalid_model"),
        (424, {}, {"detail": "No credential", "code": "provider_not_configured"}, O.ModelUnavailable, "provider_not_configured"),
        (429, {"otari-error-code": "upstream_rate_limited", "retry-after": "5"}, {"detail": "slow down"}, O.RateLimited,
         "upstream_rate_limited"),
        (502, {}, {"detail": "All upstream providers failed"}, O.AllCandidatesFailed, "all_candidates_failed"),
        (504, {}, {"detail": "The provider timed out"}, O.AllCandidatesFailed, "all_candidates_failed"),
        (401, {}, {"detail": "Invalid master key"}, O.OtariRequestError, "request_refused"),
        (403, {}, {"detail": "'user' field does not match the authenticated API key's user"}, O.OtariRequestError,
         "request_refused"),
    ],
)
def test_error_responses_map_to_typed_exceptions(
    status: int, headers: dict[str, str], body: dict[str, Any], kind: type[O.OtariError], label: str
) -> None:
    error = O.error_from_response(status, {**headers, "otari-request-id": "rid"}, json.dumps(body).encode())
    assert type(error) is kind and error.label == label and error.status == status and error.request_id == "rid"


def test_error_details_carry_scope_retry_after_tools_and_guardrails() -> None:
    budget = O.error_from_response(
        403, {"Otari-Error-Code": "budget_exceeded", "Otari-Budget-Scope": "workspace", "Retry-After": "43200"}, b"{}"
    )
    assert isinstance(budget, O.BudgetExceeded) and budget.scope == "workspace" and budget.retry_after == 43200
    access = O.error_from_response(403, {}, b'{"detail": "web access is not enabled for this workspace"}')
    assert isinstance(access, O.ToolNotEnabled) and access.tools == {O.WEB_SEARCH, O.WEB_FETCH}
    code = O.error_from_response(403, {}, b'{"detail": "code execution is not enabled for this workspace"}')
    assert code.tools == {O.CODE_EXECUTION}
    blocked = O.error_from_response(403, {}, b'{"code": "guardrail_violation", "guardrails": [{"profile": "p", "score": 0.7}]}')
    assert blocked.verdict() == {"profile": "p", "mode": "block", "valid": False, "score": 0.7, "verdict": "blocked"}
    waf = O.error_from_response(403, {"content-type": "text/html"}, b"<html>403 Forbidden</html>")
    assert isinstance(waf, O.WafBlocked) and not waf.fallback_ok


def test_only_failures_another_model_could_fix_allow_a_fallback() -> None:
    assert O.AllCandidatesFailed("x").fallback_ok and O.ModelUnavailable("x").fallback_ok
    assert O.RateLimited("x").fallback_ok
    for kind in (O.GuardrailUnavailable, O.OtariRequestError, O.InsufficientFunds, O.BadModelOutput):
        assert not kind("x").fallback_ok
    assert not O.BudgetExceeded("x").fallback_ok and not O.GuardrailViolation("x").fallback_ok


async def test_a_stream_that_fails_after_it_started_raises_the_event_code() -> None:
    def handler(body: dict[str, Any], req: httpx.Request) -> httpx.Response:
        events = [chunk({"content": "partial"}), {"error": {"message": "cut", "type": "server_error", "code": "upstream_rate_limited"}}]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(events))

    with pytest.raises(O.RateLimited) as caught:
        await client_for(Gateway(handler)).chat("investigator", "m", [], stream=True)
    assert caught.value.record.error == "upstream_rate_limited"


async def test_a_stream_that_closes_without_finishing_is_a_failure() -> None:
    def handler(body: dict[str, Any], req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse([chunk({"content": "x"})]))

    with pytest.raises(O.OtariUnavailable) as caught:
        await client_for(Gateway(handler)).chat("investigator", "m", [], stream=True)
    assert caught.value.label == "provider_stream_interrupted"


async def test_timeouts_and_lost_connections_are_unavailable() -> None:
    def handler(body: dict[str, Any], req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(O.OtariUnavailable) as caught:
        await client_for(Gateway(handler)).chat("triage", "m", [])
    assert caught.value.label == "connection"


# ----------------------------------------------------------------------
# retries and fallbacks
# ----------------------------------------------------------------------


async def test_a_firewall_block_is_retried_once_with_look_alike_quotes() -> None:
    def handler(body: dict[str, Any], req: httpx.Request) -> httpx.Response:
        text = body["messages"][0]["content"]
        if any(ch in text for ch in (BACKTICK, "'", '"')):
            return httpx.Response(403, headers={"content-type": "text/html"}, content=b"<html>403 Forbidden</html>")
        return ok("fine")

    gateway = Gateway(handler)
    text = f"fix {BACKTICK}total(){BACKTICK} and 'sum(xs)' or \"x\""
    answer, record = await client_for(gateway).chat("triage", "m", [{"role": "user", "content": text}])
    assert answer == "fine" and len(gateway.bodies) == 2 and record.error is None
    retried = gateway.bodies[1]["messages"][0]["content"]
    assert retried == "fix \u02cbtotal()\u02cb and \u2019sum(xs)\u2019 or \u201dx\u201d"


async def test_a_second_firewall_block_gives_up() -> None:
    gateway = Gateway(lambda b, r: httpx.Response(403, headers={"content-type": "text/html"}, content=b"<html></html>"))
    with pytest.raises(O.WafBlocked):
        await client_for(gateway).chat("triage", "m", [{"role": "user", "content": "plain"}])
    assert len(gateway.bodies) == 2


async def test_when_every_offering_fails_the_next_family_serves() -> None:
    def handler(body: dict[str, Any], req: httpx.Request) -> httpx.Response:
        if body["model"] == O.INVESTIGATOR_MODEL:
            return httpx.Response(502, json={"detail": "The provider failed the request with HTTP 503"})
        if body["model"] == O.REVIEWER_MODEL:
            return httpx.Response(400, json={"detail": "Unknown model", "code": "invalid_model"})
        return ok("answer", headers={"otari-provider": "deepinfra", "otari-attempt-count": "1"})

    gateway = Gateway(handler)
    answer, record = await client_for(gateway).chat(
        "investigator", O.INVESTIGATOR_MODEL, [], fallbacks=O.fallbacks_for(O.INVESTIGATOR_MODEL)
    )
    assert answer == "answer"
    assert [b["model"] for b in gateway.bodies] == [O.INVESTIGATOR_MODEL, O.REVIEWER_MODEL, O.VISION_MODEL]
    assert record.model == O.VISION_MODEL and record.fallback is True and record.attempts == 3


async def test_a_budget_refusal_never_moves_to_another_model() -> None:
    gateway = Gateway(lambda b, r: httpx.Response(403, headers={"otari-error-code": "budget_exceeded"}, json={}))
    with pytest.raises(O.BudgetExceeded) as caught:
        await client_for(gateway).chat("triage", O.TRIAGE_MODEL, [], fallbacks=O.fallbacks_for(O.TRIAGE_MODEL))
    assert len(gateway.bodies) == 1
    assert caught.value.record.model == O.TRIAGE_MODEL and caught.value.record.error == "budget_exceeded"


async def test_refused_optional_fields_are_dropped_once() -> None:
    def handler(body: dict[str, Any], req: httpx.Request) -> httpx.Response:
        if "reasoning_effort" in body:
            return httpx.Response(400, json={"detail": "'reasoning_effort' is not supported for bedrock."})
        if "response_format" in body:
            return httpx.Response(400, json={"detail": "No model accepted the request (2 attempts).", "code": "all_candidates_rejected"})
        return ok(TRIAGE_JSON)

    gateway = Gateway(handler)
    value, _ = await client_for(gateway).chat("triage", O.TRIAGE_MODEL, [], output=TriageResult, reasoning_effort="low")
    assert value.risk == "low"
    assert [sorted(k for k in b if k in ("reasoning_effort", "response_format")) for b in gateway.bodies] == [
        ["reasoning_effort", "response_format"], ["response_format"], [],
    ]


async def test_an_unrelated_bad_request_is_not_retried() -> None:
    detail = "MCP server host holdup.invalid could not be resolved at validation time"
    gateway = Gateway(lambda b, r: httpx.Response(400, json={"detail": detail}))
    with pytest.raises(O.OtariRequestError):
        await client_for(gateway).chat("triage", O.TRIAGE_MODEL, [], output=TriageResult)
    assert len(gateway.bodies) == 1


def test_the_fallback_chain_crosses_families() -> None:
    assert O.fallbacks_for(O.INVESTIGATOR_MODEL) == (O.REVIEWER_MODEL, O.VISION_MODEL)
    assert O.fallbacks_for(O.REVIEWER_MODEL) == (O.VISION_MODEL,)
    assert O.fallbacks_for(O.TRIAGE_MODEL) == (O.REVIEWER_MODEL, O.VISION_MODEL)
    assert len({O.family(m) for m in O.FALLBACK_CHAIN}) == len(O.FALLBACK_CHAIN)


# ----------------------------------------------------------------------
# keys, REST and helpers
# ----------------------------------------------------------------------


async def test_per_user_keys_reuse_the_transport_and_authenticate_as_the_user() -> None:
    gateway = Gateway(lambda b, r: ok("hi"))
    client = client_for(gateway)
    user = client.with_api_key("otk_v1_us_user")
    assert client.with_api_key("otk_v1_us_user") is user and client.with_api_key("otk_v1_us_test") is client
    await user.chat("triage", "m", [])
    assert gateway.requests[0].headers["authorization"] == "Bearer otk_v1_us_user"


def test_eu_keys_use_the_eu_gateway() -> None:
    assert O.OtariClient(Settings(otari_api_key="otk_v1_eu_x")).base_url == "https://eu.api.otari.ai/api/v1"
    assert O.OtariClient(Settings(otari_api_key="otk_v1_us_x")).base_url == "https://api.otari.ai/api/v1"
    with pytest.raises(ValueError):
        O.OtariClient(Settings())


async def test_rest_calls_send_the_key_and_map_errors() -> None:
    def handler(body: dict[str, Any], req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": O.TRIAGE_MODEL}]})
        return httpx.Response(401, json={"detail": "Invalid master key"})

    gateway = Gateway(handler)
    client = client_for(gateway)
    listing = await client.rest("GET", "/models")
    assert listing["data"][0]["id"] == O.TRIAGE_MODEL
    assert str(gateway.requests[0].url) == "https://api.otari.ai/api/v1/models"
    with pytest.raises(O.OtariRequestError, match="Invalid master key"):
        await client.rest("POST", "/organizations/me/keys", json_body={"key_name": "x"})


def test_guardrail_header_parsing() -> None:
    live = '[{"profile":"prompt-injection","mode":"monitor","valid":null,"score":null}]'
    assert O.guardrail_verdict(O.parse_guardrails_header(live))["verdict"] == "inconclusive"
    passed = O.parse_guardrails_header('{"profile": "a", "valid": true, "score": 0.0}')
    assert O.guardrail_verdict(passed)["verdict"] == "passed"
    mixed = O.parse_guardrails_header('[{"profile": "a", "valid": true}, {"profile": "b", "valid": false}]')
    assert O.guardrail_verdict(mixed)["verdict"] == "flagged"
    assert O.guardrail_verdict(mixed, "a")["verdict"] == "passed"
    assert O.parse_guardrails_header("checker-injection=fail, other=pass") == [
        {"profile": "checker-injection", "valid": False}, {"profile": "other", "valid": True},
    ]
    assert O.parse_guardrails_header(None) == [] and O.guardrail_verdict([]) is None


def test_waf_safe_rewrites_text_parts_only() -> None:
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
    messages = [{"role": "user", "content": [{"type": "text", "text": "it's " + BACKTICK}, image]}]
    cleaned = O.waf_safe(messages)
    assert cleaned[0]["content"][0]["text"] == "it\u2019s \u02cb" and cleaned[0]["content"][1] == image
    assert messages[0]["content"][0]["text"] == "it's " + BACKTICK  # the original is left alone


def test_extract_json_tolerates_prose_and_fences() -> None:
    assert O.extract_json('Here you go: {"a": 1} hope it helps') == {"a": 1}
    assert O.extract_json(BACKTICK * 3 + '\n{"a": 2}\n' + BACKTICK * 3) == {"a": 2}
    with pytest.raises(ValueError):
        O.extract_json("no json at all")
