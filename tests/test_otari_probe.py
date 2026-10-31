"""The live probe runner, exercised against a fake gateway: results, recordings without the key, spend cap."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from holdup.config import Settings
from holdup.risk import probe
from holdup.risk.otari import CODE_EXECUTION, WEB_FETCH, WEB_SEARCH

KEY = "otk_v1_us_probe-secret-value"


def answer(text: str = "Hello there", cost: str = "0.000010", headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"otari-provider": "deepinfra", **(headers or {})},
        json={
            "id": "x",
            "object": "chat.completion",
            "created": 1,
            "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10, "cost_usd": cost},
        },
    )


def stream(text: str, calls: list[dict[str, Any]]) -> httpx.Response:
    base = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m"}
    events = [
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": None}], "otari_tool_calls": [c]} for c in calls
    ]
    events.append({**base, "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": "stop"}]})
    events.append(
        {
            **base,
            "choices": [],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10, "cost_usd": "0.010000"},
        }
    )
    data = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
    return httpx.Response(
        200, headers={"content-type": "text/event-stream", "otari-provider": "deepinfra"}, content=data.encode()
    )


def gateway(request: httpx.Request) -> httpx.Response:
    path = request.url.path.removeprefix("/api/v1")
    assert request.headers.get("authorization") in (f"Bearer {KEY}", "Bearer otk_v1_us_minted")
    if path == "/models":
        return httpx.Response(200, json={"data": [{"id": m} for m in probe.PLANNED_MODELS]})
    if path == "/tools":
        return httpx.Response(
            200, json={"data": [{"id": t, "available": True} for t in (WEB_SEARCH, WEB_FETCH, CODE_EXECUTION)]}
        )
    if path == "/organizations/me/keys" and request.method == "POST":
        return httpx.Response(200, json={"id": "key-9", "key": "otk_v1_us_minted"})
    if path == "/organizations/me/budgets":
        return httpx.Response(200, json={"budget_id": "b-9"})
    if path == "/organizations/me/spend-ceilings":
        return httpx.Response(200, json={"id": "c-9"})
    if request.method == "DELETE":
        return httpx.Response(200, json={})
    body = json.loads(request.content)
    text = json.dumps(body["messages"])
    if body["model"] == "holdup/no-such-model":
        if "\\u0060" in text or chr(0x60) in text:
            return httpx.Response(403, headers={"content-type": "text/html"}, content=b"<html>403</html>")
        return httpx.Response(400, json={"detail": "Unknown model", "code": "invalid_model"})
    if body.get("guardrails"):
        if body["guardrails"][0]["mode"] == "block":
            return httpx.Response(
                403, json={"message": "Request blocked by guardrail policy.", "code": "guardrail_violation"}
            )
        flagged = json.dumps([{"profile": "checker-injection", "mode": "monitor", "valid": False, "score": 0.9}])
        return answer("ok", headers={"otari-guardrails": flagged})
    tools = [t["type"] for t in body.get("tools") or []]
    if WEB_SEARCH in tools:
        return stream(
            "https://docs.near.org",
            [
                {
                    "id": "s1",
                    "type": "web_search",
                    "status": "completed",
                    "query": "near docs",
                    "sources": [{"url": "https://docs.near.org"}],
                }
            ],
        )
    if WEB_FETCH in tools:
        return stream(
            "Example Domain", [{"id": "f1", "type": "web_fetch", "status": "completed", "url": "https://example.com"}]
        )
    if CODE_EXECUTION in tools or body.get("mcp_servers"):
        return stream("1150", [])
    return answer()


def run(tmp_path: Path, **kwargs: Any) -> list[probe.ProbeResult]:
    import asyncio

    settings = Settings(otari_api_key=KEY, secret="probe-secret")
    kwargs.setdefault("public_url", "https://holdup.example.org")
    return asyncio.run(
        probe.run_probes(settings, record_dir=tmp_path / "recorded", transport=httpx.MockTransport(gateway), **kwargs)
    )


def test_every_probe_passes_against_a_healthy_gateway(tmp_path: Path) -> None:
    results = run(tmp_path, mint=True)
    outcomes = {r.name: r.outcome for r in results}
    assert set(outcomes) == set(probe.PROBES)
    assert all(outcome == "ok" for outcome in outcomes.values()), [r for r in results if r.outcome != "ok"]
    waf = next(r for r in results if r.name == "waf")
    assert waf.detail.startswith("3 of 4 samples blocked")
    recordings = list((tmp_path / "recorded").glob("*.json"))
    assert len(recordings) == len(probe.PROBES)  # one file per probe that sent something
    for path in recordings:
        text = path.read_text()
        assert json.loads(text)["synthetic"] is False
        assert KEY not in text and "otk_v1_us_minted" not in text  # minted keys are secrets too
        assert "probe-secret" not in text and '"authorization_token": "0.' not in text
    report = probe.render_markdown(results, "https://api.otari.ai/api/v1")
    assert report.startswith("# Otari probes") and "| guardrail_block | ok | 403 |" in report


def test_probes_that_need_the_public_url_or_minting_are_skipped(tmp_path: Path) -> None:
    results = run(tmp_path, public_url=None, names=["mcp", "guardrail_monitor", "guardrail_block", "budgets"])
    assert [r.outcome for r in results] == ["skipped"] * 4


def test_the_spend_cap_stops_the_remaining_probes(tmp_path: Path) -> None:
    names = ["web_search", "web_fetch", "code_execution", "image"]
    results = run(tmp_path, names=names, max_usd=0.015)
    assert [r.outcome for r in results] == ["ok", "ok", "skipped", "skipped"]


def test_the_command_lists_probes_and_needs_a_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert probe.main(["--list"]) == 0
    assert "guardrail_block" in capsys.readouterr().out
    monkeypatch.delenv("OTARI_API_KEY", raising=False)
    monkeypatch.setenv("HOLDUP_CREDENTIALS", "/nonexistent/credentials.json")
    assert probe.main([]) == 2
