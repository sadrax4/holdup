"""Recorded Otari exchanges, replayed so the pipeline runs without a key.

``ReplayTransport`` is an httpx transport that answers chat completions from
``fixtures/otari/<scenario>.json``, keyed by scenario and step. The step comes
from the ``step`` request tag that the pipeline puts in ``metadata`` on every
call; the scenario comes from a resolver (the demo sets one per payment,
otherwise phrases in the request pick one) or falls back to ``benign``.

A recorded 200 is rendered as a JSON completion, or for a streamed request as
server-sent events in the shape the hosted gateway streams: one chunk when a
gateway web search or fetch starts and one when it ends, then the answer, then
a usage chunk with ``cost_usd``. Other statuses are replayed as recorded. A
response marked ``waf`` is answered with the firewall's HTML 403 whenever the
request text still contains a backtick or an ASCII quote, as the hosted CDN
does (otari issue #2704).

Fixture files carry ``"synthetic": true`` when they were written by hand from
the documented shapes rather than recorded from the live gateway.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings
from .otari import OtariClient

log = logging.getLogger(__name__)

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "otari"
DEFAULT_SCENARIO = "benign"
REPLAY_CREATED = 1793448000  # 2026-10-31, a fixed timestamp so replays are deterministic
WAF_PAGE = b"<html>\r\n<head><title>403 Forbidden</title></head>\r\n<body>\r\n<center><h1>403 Forbidden</h1></center>\r\n</body>\r\n</html>\r\n"
_WAF_CHARS = ("\u0060", "'", '"')

ScenarioResolver = Callable[..., str]


@dataclass
class Replayed:
    """One request the transport answered, for tests and for the console."""

    scenario: str
    step: str
    model: str
    body: dict[str, Any]
    status: int


@cache
def _load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("steps"), dict) or not isinstance(data.get("synthetic"), bool):
        raise ValueError(f"{path.name}: a fixture needs a steps object and a synthetic flag")
    return data


def available_scenarios(fixtures_dir: Path = FIXTURES_DIR) -> list[str]:
    return sorted(p.stem for p in fixtures_dir.glob("*.json"))


def _texts(body: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for message in body.get("messages") or []:
        content = message.get("content")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            out += [p["text"] for p in content if isinstance(p, dict) and isinstance(p.get("text"), str)]
    return out


def _header_value(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))


class ReplayTransport(httpx.AsyncBaseTransport):
    """Answers Otari chat completions from recorded fixtures."""

    def __init__(
        self,
        scenario: ScenarioResolver | str | None = None,
        *,
        fixtures_dir: Path = FIXTURES_DIR,
        default: str = DEFAULT_SCENARIO,
        strict: bool = False,
    ) -> None:
        self.scenario = scenario
        self.fixtures_dir = Path(fixtures_dir)
        self.default = default
        self.strict = strict  # refuse requests whose shape differs from what the fixture recorded
        self.requests: list[Replayed] = []
        self.mismatches: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raw = await request.aread()
        if request.method == "POST" and request.url.path.endswith("/chat/completions"):
            return self._chat(json.loads(raw or b"{}"))
        return httpx.Response(404, json={"detail": f"nothing recorded for {request.method} {request.url.path}"})

    # ------------------------------------------------------------------

    def _resolve(self, step: str, body: dict[str, Any]) -> str:
        if callable(self.scenario):
            return self.scenario(step, body) or self.default
        return self.scenario or self.default

    def _entry(self, scenario: str, step: str) -> dict[str, Any] | None:
        for name in dict.fromkeys((scenario, self.default)):
            path = self.fixtures_dir / f"{name}.json"
            if not path.exists():
                continue
            steps = _load(path)["steps"]
            entry = steps.get(step) or steps.get("*")
            if entry is not None:
                return entry
        return None

    def _chat(self, body: dict[str, Any]) -> httpx.Response:
        step = str((body.get("metadata") or {}).get("step") or "triage")
        scenario = self._resolve(step, body)
        model = str(body.get("model", ""))
        entry = self._entry(scenario, step)
        if entry is None:
            return self._answer(scenario, step, body, httpx.Response(
                404, json={"detail": f"no recorded response for step {step} in scenario {scenario}"}))
        problems = self._check(entry.get("expect") or {}, body)
        if problems:
            message = f"replay {scenario}/{step}: " + "; ".join(problems)
            self.mismatches.append(message)
            log.warning("%s", message)
            if self.strict:
                return self._answer(scenario, step, body, httpx.Response(400, json={"detail": message}))
        by_model = entry.get("by_model")
        recorded = (by_model.get(model) or by_model.get("*")) if isinstance(by_model, dict) else entry.get("response")
        if recorded is None:
            return self._answer(scenario, step, body, httpx.Response(
                404, json={"detail": f"no recorded response for {model} at {scenario}/{step}"}))
        if recorded.get("waf") and any(ch in text for text in _texts(body) for ch in _WAF_CHARS):
            return self._answer(scenario, step, body, httpx.Response(
                403, headers={"content-type": "text/html"}, content=WAF_PAGE))
        return self._answer(scenario, step, body, self._render(recorded, body, scenario, step))

    def _answer(self, scenario: str, step: str, body: dict[str, Any], response: httpx.Response) -> httpx.Response:
        self.requests.append(Replayed(scenario, step, str(body.get("model", "")), body, response.status_code))
        return response

    @staticmethod
    def _check(expect: dict[str, Any], body: dict[str, Any]) -> list[str]:
        problems = []
        declared = {t.get("type") for t in body.get("tools") or [] if isinstance(t, dict)}
        problems += [f"tool {t} not declared" for t in expect.get("tools", []) if t not in declared]
        servers = {s.get("name") for s in body.get("mcp_servers") or [] if isinstance(s, dict)}
        problems += [f"MCP server {s} not declared" for s in expect.get("mcp_servers", []) if s not in servers]
        guards = {(g.get("profile"), g.get("mode")) for g in body.get("guardrails") or [] if isinstance(g, dict)}
        problems += [
            f"guardrail {g['profile']} ({g['mode']}) not declared"
            for g in expect.get("guardrails", [])
            if (g["profile"], g["mode"]) not in guards
        ]
        if "stream" in expect and bool(body.get("stream")) != expect["stream"]:
            problems.append(f"expected stream={expect['stream']}")
        return problems

    @staticmethod
    def _render(recorded: dict[str, Any], body: dict[str, Any], scenario: str, step: str) -> httpx.Response:
        status = int(recorded.get("status", 200))
        headers = {k.lower(): _header_value(v) for k, v in (recorded.get("headers") or {}).items()}
        if status != 200:
            payload = recorded.get("body", {"detail": "recorded error"})
            if isinstance(payload, str):
                headers.setdefault("content-type", "text/plain")
                return httpx.Response(status, headers=headers, content=payload.encode("utf-8"))
            return httpx.Response(status, headers=headers, json=payload)
        content = recorded.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        calls = list(recorded.get("otari_tool_calls") or [])
        usage = recorded.get("usage") or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        finish = recorded.get("finish_reason", "stop")
        base = {
            "id": f"chatcmpl-replay-{scenario}-{step}",
            "created": REPLAY_CREATED,
            "model": body.get("model"),
        }
        if not body.get("stream"):
            completion = {
                **base,
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish}],
                "usage": usage,
            }
            if calls:
                completion["otari_tool_calls"] = calls
            return httpx.Response(200, headers=headers, json=completion)
        headers["content-type"] = "text/event-stream; charset=utf-8"
        return httpx.Response(200, headers=headers, content=_sse(base, content, calls, finish, usage))


def _sse(base: dict[str, Any], content: str, calls: list[dict[str, Any]], finish: str, usage: dict[str, Any]) -> bytes:
    chunk = {**base, "object": "chat.completion.chunk"}

    def event(delta: dict[str, Any], finish_reason: str | None = None, **extra: Any) -> dict[str, Any]:
        return {**chunk, "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}], **extra}

    events = [event({"role": "assistant", "content": ""})]
    for call in calls:
        started = {k: v for k, v in call.items() if k not in ("sources", "error")} | {"status": "in_progress"}
        events.append(event({}, otari_tool_calls=[started]))
        events.append(event({}, otari_tool_calls=[call]))
    events += [event({"content": content[i : i + 120]}) for i in range(0, len(content), 120)]
    events.append(event({}, finish))
    events.append({**chunk, "choices": [], "usage": usage})
    lines = [f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events]
    return ("".join(lines) + "data: [DONE]\n\n").encode("utf-8")


def replay_client(
    settings: Settings,
    scenario_resolver: ScenarioResolver | str | None = None,
    *,
    fixtures_dir: Path | None = None,
    strict: bool = False,
) -> OtariClient:
    """An ``OtariClient`` whose requests never leave the process."""
    transport = ReplayTransport(scenario_resolver, fixtures_dir=fixtures_dir or FIXTURES_DIR, strict=strict)
    client = OtariClient(settings, httpx.AsyncClient(transport=transport), api_key="replay", offline=True)
    client.transport = transport
    return client
