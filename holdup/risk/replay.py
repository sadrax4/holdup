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

Every replayed answer names its provider as ``<provider> (recorded)``, so the
steps, the /why page and the evidence hashed on-chain all say it was not a
live call. ``{base_url}`` in a recording stands for the scheme and host of the
first link the request lists, so recorded citations point where the message did.

Fixture files carry ``"synthetic": true`` when they were written by hand from
the documented shapes rather than recorded from the live gateway.
"""

from __future__ import annotations

import json
import logging
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..config import Settings
from .otari import OtariClient

log = logging.getLogger(__name__)

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "otari"
DEFAULT_SCENARIO = "benign"
REPLAY_CREATED = 1793448000  # 2026-10-31, a fixed timestamp so replays are deterministic
WAF_PAGE = (
    b"<html>\r\n<head><title>403 Forbidden</title></head>\r\n"
    b"<body>\r\n<center><h1>403 Forbidden</h1></center>\r\n</body>\r\n</html>\r\n"
)
_WAF_CHARS = ("\u0060", "'", '"')
RECORDED = "recorded"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
_LINKS_FACT = re.compile(r"^- links in the message: (.+)$", re.MULTILINE)

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
        base_url: str = "",
    ) -> None:
        self.scenario = scenario
        self.fixtures_dir = Path(fixtures_dir)
        self.default = default
        self.base_url = base_url.rstrip("/") or DEFAULT_BASE_URL  # for requests that list no link
        self.strict = strict  # refuse requests whose shape differs from what the fixture recorded
        # The latest requests and shape mismatches, bounded for long-running offline servers.
        self.requests: deque[Replayed] = deque(maxlen=500)
        self.mismatches: deque[str] = deque(maxlen=100)

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
            return self._answer(
                scenario,
                step,
                body,
                httpx.Response(404, json={"detail": f"no recorded response for step {step} in scenario {scenario}"}),
            )
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
            return self._answer(
                scenario,
                step,
                body,
                httpx.Response(404, json={"detail": f"no recorded response for {model} at {scenario}/{step}"}),
            )
        if recorded.get("waf") and any(ch in text for text in _texts(body) for ch in _WAF_CHARS):
            return self._answer(
                scenario, step, body, httpx.Response(403, headers={"content-type": "text/html"}, content=WAF_PAGE)
            )
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

    def _base_url(self, body: dict[str, Any]) -> str:
        """Scheme and host of the first link in the request's payment facts."""
        for text in _texts(body):
            if match := _LINKS_FACT.search(text):
                for link in match.group(1).split(","):
                    parts = urlsplit(link.strip())
                    if parts.scheme in ("http", "https") and parts.netloc:
                        return f"{parts.scheme}://{parts.netloc}"
        return self.base_url

    def _render(self, recorded: dict[str, Any], body: dict[str, Any], scenario: str, step: str) -> httpx.Response:
        status = int(recorded.get("status", 200))
        headers = {k.lower(): _header_value(v) for k, v in (recorded.get("headers") or {}).items()}
        provider = headers.get("otari-provider")
        headers["otari-provider"] = f"{provider} ({RECORDED})" if provider else RECORDED
        if status != 200:
            payload = recorded.get("body", {"detail": "recorded error"})
            if isinstance(payload, str):
                headers.setdefault("content-type", "text/plain")
                return httpx.Response(status, headers=headers, content=payload.encode("utf-8"))
            return httpx.Response(status, headers=headers, json=payload)
        base_url = self._base_url(body)
        content = recorded.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        content = content.replace("{base_url}", base_url)
        calls = json.loads(json.dumps(recorded.get("otari_tool_calls") or []).replace("{base_url}", base_url))
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
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish}
                ],
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
    transport = ReplayTransport(
        scenario_resolver,
        fixtures_dir=fixtures_dir or FIXTURES_DIR,
        strict=strict,
        base_url=settings.fixture_base_url or settings.public_base_url,
    )
    client = OtariClient(settings, httpx.AsyncClient(transport=transport), api_key="replay", offline=True)
    client.transport = transport
    return client
