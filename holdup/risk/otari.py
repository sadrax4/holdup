"""Client for the Otari gateway.

Otari serves the OpenAI Chat Completions API at ``https://api.otari.ai/api/v1``
(EU keys, prefix ``otk_v1_eu_``: ``https://eu.api.otari.ai/api/v1``) and adds
gateway features as plain request fields: built-in tools, inline MCP servers,
request guardrails and cost tags. ``OtariClient.chat`` wraps the ``openai`` SDK
with what HoldUp needs from it:

* every call returns ``(value, StepRecord)``: the validated JSON contract or the
  text, plus who served it, fallbacks, attempts, request id, cost, latency, the
  gateway tool calls and the guardrail verdict;
* refusals become typed exceptions, mapped on the stable ``Otari-Error-Code``;
* calls that use gateway tools are streamed, because the hosted CDN cuts
  non-streamed responses after about 60 s (otari issue #2692);
* a request that the CDN firewall rejects with an HTML 403 is retried once with
  look-alike quote characters (otari issue #2704);
* when every offering of a model failed, the call moves on to the next model
  family in the chain it was given.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
import openai
from pydantic import BaseModel, ValidationError

from ..config import Settings
from ..models import StepRecord
from .prompts.contracts import response_format

try:  # the openai SDK runs on httpx2 when it creates its own HTTP client
    import httpx2
except ImportError:  # pragma: no cover - httpx2 ships with openai 3.x
    httpx2 = None

log = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# models: catalog ids, all open_weights: true in the live catalog (checked 2026-10-31)
# ----------------------------------------------------------------------

TRIAGE_MODEL = "openai/gpt-oss-20b"
INVESTIGATOR_MODEL = "openai/gpt-oss-120b"
VISION_MODEL = "z-ai/glm-5.3-flash"
REVIEWER_MODEL = "deepseek/deepseek-v4.1-flash-1017"
INJECTION_GUARD_MODEL = "openai/gpt-oss-safeguard-120b"
SAFETY_GUARD_MODEL = "meta/llama-guard-4-12b"

MODEL_FAMILIES = {
    TRIAGE_MODEL: "gpt-oss",
    INVESTIGATOR_MODEL: "gpt-oss",
    INJECTION_GUARD_MODEL: "gpt-oss",
    REVIEWER_MODEL: "deepseek-flash",
    VISION_MODEL: "glm-flash",
    SAFETY_GUARD_MODEL: "llama",
}

FALLBACK_CHAIN = (INVESTIGATOR_MODEL, REVIEWER_MODEL, VISION_MODEL)
"""Cross-family order tried when every offering of a model has failed."""

REASONING_EFFORT_MODELS = frozenset({TRIAGE_MODEL, INVESTIGATOR_MODEL})
"""Models whose offerings accept ``reasoning_effort``. The safeguard model is served
on Bedrock, where the gateway refuses the field with a 400 (observed 2026-10-31)."""

WEB_SEARCH = "otari_web_search"
WEB_FETCH = "otari_web_fetch"
CODE_EXECUTION = "otari_code_execution"

PLAIN_TIMEOUT_S = 60.0
TOOL_LOOP_TIMEOUT_S = 180.0


def family(model: str) -> str:
    return MODEL_FAMILIES.get(model, model.split("/", 1)[0])


def fallbacks_for(model: str) -> tuple[str, ...]:
    """The models to try, in order, when every offering of ``model`` failed."""
    if model in FALLBACK_CHAIN:
        return FALLBACK_CHAIN[FALLBACK_CHAIN.index(model) + 1 :]
    return tuple(m for m in FALLBACK_CHAIN if family(m) != family(model))


# ----------------------------------------------------------------------
# errors
# ----------------------------------------------------------------------


class OtariError(Exception):
    """A request the gateway did not answer with a usable completion."""

    fallback_ok = False  # whether another model family could succeed where this one failed
    kind = "otari_error"  # label used when the gateway sent no error code

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id
        self.retry_after = retry_after
        self.record: StepRecord | None = None  # the step as far as it got, set by OtariClient.chat

    @property
    def label(self) -> str:
        """Short machine-readable cause, used as ``StepRecord.error``."""
        return self.code or self.kind


class OtariUnavailable(OtariError):
    """Timeouts, lost connections, gateway or provider failures."""

    fallback_ok = True
    kind = "unavailable"


class AllCandidatesFailed(OtariUnavailable):
    """Every offering of the requested model failed or rejected the request."""

    kind = "all_candidates_failed"


class RateLimited(OtariUnavailable):
    """A gateway or provider rate limit; ``retry_after`` says when to try again."""

    kind = "rate_limited"


class GuardrailUnavailable(OtariUnavailable):
    """A blocking guardrail could not be evaluated, so the gateway refused (fails closed)."""

    fallback_ok = False
    kind = "guardrail_unavailable"


class ModelUnavailable(OtariError):
    """The gateway cannot serve this model for this key; another model may work."""

    fallback_ok = True
    kind = "model_unavailable"


class OtariRequestError(OtariError):
    """The gateway refused the request itself (bad key, bad field, model not allowed)."""

    kind = "request_refused"


class BudgetExceeded(OtariError):
    """A budget or spend ceiling refused the request (403 budget_exceeded)."""

    kind = "budget_exceeded"

    def __init__(self, message: str, *, scope: str | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.scope = scope


class InsufficientFunds(OtariError):
    """The organization has no funds paid in for a deployment-paid model (402)."""

    kind = "insufficient_funds"


class GuardrailViolation(OtariError):
    """A guardrail in block mode flagged the input; the provider was never called (403)."""

    kind = "guardrail_violation"

    def __init__(self, message: str, *, guardrails: list[dict[str, Any]] | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.guardrails = guardrails or []

    def verdict(self) -> dict[str, Any]:
        first = self.guardrails[0] if self.guardrails else {}
        return {
            "profile": first.get("profile"),
            "mode": "block",
            "valid": False,
            "score": first.get("score"),
            "verdict": "blocked",
        }


class ToolNotEnabled(OtariError):
    """The workspace has a gateway tool switched off (403 without an error code)."""

    kind = "tool_not_enabled"

    def __init__(self, message: str, *, tools: frozenset[str], **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.tools = tools


class WafBlocked(OtariError):
    """The CDN firewall in front of the hosted gateway rejected the request (HTML 403)."""

    kind = "waf_blocked"


class BadModelOutput(OtariError):
    """The model answered, but not with text that fits the requested contract."""

    kind = "bad_output"


_MODEL_CODES = {
    "invalid_model",
    "model_not_found",
    "model_not_allowed",
    "model_not_serving",
    "unsupported_content",
    "provider_not_configured",
    "provider_credential_rejected",
}
_DISABLED = re.compile(r"([a-z_ ]+?) (?:is|are) not enabled for this workspace")


def _disabled_tools(detail: str) -> frozenset[str]:
    """Which tools a refusal such as 'web search is not enabled for this workspace' names."""
    match = _DISABLED.search(detail.lower())
    if not match:
        return frozenset()
    what = match.group(1)
    if "access" in what:  # the web-access policy covers search and fetch
        return frozenset({WEB_SEARCH, WEB_FETCH})
    if "search" in what:
        return frozenset({WEB_SEARCH})
    if "fetch" in what:
        return frozenset({WEB_FETCH})
    if "code" in what:
        return frozenset({CODE_EXECUTION})
    if "mcp" in what:
        return frozenset({"mcp"})
    return frozenset({what.strip()})


def _error_fields(body: Any) -> tuple[str, str | None]:
    """``detail`` and ``code`` from any of the error envelopes the gateway uses."""
    if not isinstance(body, dict):
        return "", None
    nested = body.get("error") if isinstance(body.get("error"), dict) else {}
    detail = body.get("detail")
    if isinstance(detail, list):  # schema validation errors
        detail = "; ".join(str(item.get("msg", item)) if isinstance(item, dict) else str(item) for item in detail)
    detail = detail or body.get("message") or nested.get("message") or ""
    code = body.get("code") or nested.get("code")
    return str(detail), (str(code) if code else None)


def _seconds(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None  # an HTTP date; callers treat it as unknown


def error_from_response(status: int, headers: Mapping[str, str], content: bytes | str | None) -> OtariError:
    """Map an error response to a typed exception (api-reference: Error codes)."""
    lowered = {k.lower(): v for k, v in headers.items()}
    text = content.decode("utf-8", "replace") if isinstance(content, bytes) else (content or "")
    try:
        body = json.loads(text) if text else None
    except ValueError:
        body = None
    detail, code = _error_fields(body)
    code = lowered.get("otari-error-code") or code
    request_id = lowered.get("otari-request-id") or (body.get("request_id") if isinstance(body, dict) else None)
    kwargs: dict[str, Any] = {
        "status": status,
        "code": code,
        "request_id": request_id,
        "retry_after": _seconds(lowered.get("retry-after")),
    }
    if status == 403 and "html" in lowered.get("content-type", "").lower():
        return WafBlocked("the gateway firewall rejected the request text", **kwargs)
    if code == "budget_exceeded":
        return BudgetExceeded(
            detail or "a budget refused the request", scope=lowered.get("otari-budget-scope"), **kwargs
        )
    if code == "guardrail_violation":
        guardrails = body.get("guardrails") if isinstance(body, dict) else None
        return GuardrailViolation(
            detail or "a guardrail blocked the request",
            guardrails=[g for g in guardrails or [] if isinstance(g, dict)],
            **kwargs,
        )
    if code == "insufficient_funds" or (status == 402 and code != "pricing_required"):
        return InsufficientFunds(detail or "no funds to pay for this model", **kwargs)
    if status == 403 and (tools := _disabled_tools(detail)):
        return ToolNotEnabled(detail, tools=tools, **kwargs)
    if code == "all_candidates_rejected":
        return AllCandidatesFailed(detail or "every candidate rejected the request", **kwargs)
    if code in _MODEL_CODES:
        return ModelUnavailable(detail or code, **kwargs)
    if status == 429:
        return RateLimited(detail or "rate limited", **kwargs)
    if status >= 500:
        # A blocking guardrail that cannot run answers 502 (guardrails: When the
        # guardrails service is unreachable); the detail names the guardrail.
        if "guardrail" in detail.lower() or (code or "").startswith("guardrail"):
            return GuardrailUnavailable(detail, **kwargs)
        # A catalog id falls back across its offerings before answering 5xx.
        return AllCandidatesFailed(detail or f"the gateway answered {status}", **kwargs)
    return OtariRequestError(detail or f"the gateway refused the request ({status})", **kwargs)


def error_from_stream_event(body: Any) -> OtariError:
    """Map the error event that ends a stream which had already started."""
    detail, code = _error_fields({"error": body} if isinstance(body, dict) and "error" not in body else body)
    if code in ("upstream_rate_limited", "rate_limited"):
        return RateLimited(detail or "rate limited", code=code)
    if code == "context_length_exceeded":
        return OtariRequestError(detail, code=code)
    return OtariUnavailable(detail or "the stream ended with an error", code=code or "provider_error")


# ----------------------------------------------------------------------
# response helpers
# ----------------------------------------------------------------------

_PASS_WORDS = frozenset({"pass", "passed", "valid", "true", "ok"})
_FAIL_WORDS = frozenset({"fail", "failed", "flagged", "invalid", "false", "blocked"})
_LOOKALIKES = str.maketrans({"\u0060": "\u02cb", "'": "\u2019", '"': "\u201d"})
_FENCE = "\u0060" * 3


def waf_safe(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Messages with backtick and ASCII quote characters swapped for look-alikes.

    The CDN firewall in front of the hosted gateway answers some bodies that
    contain them with an HTML 403 before Otari sees the request; the same text
    with U+02CB, U+2019 and U+201D goes through (otari issue #2704).
    """
    out = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            content = content.translate(_LOOKALIKES)
        elif isinstance(content, list):
            content = [
                {**part, "text": part["text"].translate(_LOOKALIKES)}
                if part.get("type") == "text" and isinstance(part.get("text"), str)
                else part
                for part in content
            ]
        out.append({**message, "content": content})
    return out


def extract_json(text: str) -> Any:
    """The JSON object in a model answer, tolerating code fences and stray prose."""
    text = text.strip()
    if text.startswith(_FENCE):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit(_FENCE, 1)[0]
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        return json.loads(text[start : end + 1])
    raise ValueError("no JSON object in the answer")


def parse_guardrails_header(value: str | None) -> list[dict[str, Any]]:
    """Verdicts from the ``Otari-Guardrails`` response header.

    The hosted gateway sends a JSON list, for example
    ``[{"profile": "prompt-injection", "mode": "monitor", "valid": true, "score": 0.0}]``,
    where ``valid: null`` means the check could not be evaluated. A
    ``profile=verdict`` list is accepted too.
    """
    if not value:
        return []
    try:
        data = json.loads(value)
    except ValueError:
        entries = []
        for part in value.split(","):
            profile, _, verdict = part.strip().partition("=")
            word = verdict.strip().lower()
            valid = True if word in _PASS_WORDS else False if word in _FAIL_WORDS else None
            entries.append({"profile": profile.strip(), "valid": valid})
        return [e for e in entries if e["profile"]]
    if isinstance(data, dict):
        data = [data]
    return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []


def guardrail_verdict(entries: list[dict[str, Any]], profile: str | None = None) -> dict[str, Any] | None:
    """One summary of the checks that ran: flagged, passed or inconclusive."""
    chosen = [e for e in entries if profile is None or e.get("profile") == profile] or entries
    if not chosen:
        return None
    first = chosen[0]
    if any(e.get("valid") is False for e in chosen):
        verdict = "flagged"
    elif all(e.get("valid") is True for e in chosen):
        verdict = "passed"
    else:
        verdict = "inconclusive"
    return {
        "profile": first.get("profile"),
        "mode": first.get("mode"),
        "valid": first.get("valid"),
        "score": first.get("score"),
        "verdict": verdict,
    }


def _cost(usage: Mapping[str, Any]) -> float | None:
    """``usage.cost_usd``: a six-decimal USD string covering tokens and gateway tools."""
    try:
        return float(usage["cost_usd"]) if usage.get("cost_usd") is not None else None
    except (TypeError, ValueError):
        return None


def _tool_call(call: Mapping[str, Any]) -> dict[str, Any]:
    """A gateway web search or fetch, as reported in ``otari_tool_calls``."""
    kind = str(call.get("type") or "tool")
    entry: dict[str, Any] = {"name": f"otari_{kind}", "id": call.get("id"), "status": call.get("status")}
    if kind == "web_search":
        entry["query"] = call.get("query")
        entry["sources"] = [
            s["url"] for s in call.get("sources") or [] if isinstance(s, dict) and isinstance(s.get("url"), str)
        ]
    elif kind == "web_fetch":
        entry["url"] = call.get("url")
    error = call.get("error")
    if isinstance(error, dict):
        entry["error"] = {k: error.get(k) for k in ("tag", "status", "provider", "retryable")}
    return entry


@dataclass
class _Reply:
    content: str
    finish_reason: str | None
    tool_calls: list[dict[str, Any]]
    usage: dict[str, Any]
    headers: Mapping[str, str]


def _usage(usage: Any) -> dict[str, Any]:
    return usage.model_dump() if usage is not None else {}


def _read_completion(raw: Any) -> _Reply:
    completion = raw.parse()
    choice = completion.choices[0] if completion.choices else None
    extra = completion.model_extra or {}
    return _Reply(
        content=(choice.message.content or "") if choice else "",
        finish_reason=choice.finish_reason if choice else None,
        tool_calls=[c for c in extra.get("otari_tool_calls") or [] if isinstance(c, dict)],
        usage=_usage(completion.usage),
        headers=raw.headers,
    )


async def _read_stream(raw: Any) -> _Reply:
    """Collect a streamed completion.

    Gateway web searches and fetches arrive as whole ``otari_tool_calls``
    entries on chunks of their own, once when a call starts and once when it
    ends; the ended entry replaces the started one (api-reference: Web tool
    calls on Chat Completions). Usage, with ``cost_usd``, rides the last chunk.
    """
    parts: list[str] = []
    calls: dict[str, dict[str, Any]] = {}
    usage: dict[str, Any] = {}
    finish: str | None = None
    async for chunk in raw.parse():
        extra = chunk.model_extra or {}
        for call in extra.get("otari_tool_calls") or []:
            if isinstance(call, dict):
                calls[str(call.get("id") or len(calls))] = call
        for choice in chunk.choices or []:
            if choice.delta is not None and choice.delta.content:
                parts.append(choice.delta.content)
            if choice.finish_reason:
                finish = choice.finish_reason
        if chunk.usage is not None:
            usage = _usage(chunk.usage)
    if finish is None:
        raise OtariUnavailable("the stream ended before the answer finished", code="provider_stream_interrupted")
    return _Reply("".join(parts), finish, list(calls.values()), usage, raw.headers)


def _refused_optional_field(exc: OtariError, params: Mapping[str, Any]) -> str | None:
    """The optional request field a 400 refuses, when the request can do without it."""
    if exc.status != 400:
        return None
    detail = str(exc).lower()
    if "reasoning_effort" in params and "reasoning" in detail:
        return "reasoning_effort"
    rejected = exc.code == "all_candidates_rejected" or "rejected the request as invalid" in detail
    if "response_format" in params and (rejected or "response_format" in detail or "schema" in detail):
        return "response_format"
    return None


_TRANSPORT_ERRORS: tuple[type[Exception], ...] = (httpx.HTTPError,) + ((httpx2.HTTPError,) if httpx2 else ())

T = TypeVar("T", bound=BaseModel)


# ----------------------------------------------------------------------
# client
# ----------------------------------------------------------------------


class OtariClient:
    """Chat completions and REST calls against the Otari gateway."""

    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient | None = None,
        *,
        api_key: str | None = None,
        offline: bool = False,
    ) -> None:
        key = api_key or settings.otari_api_key
        if not key:
            if not offline:
                raise ValueError("OTARI_API_KEY is not set")
            key = "offline"
        self.settings = settings
        self.offline = offline  # True when requests are answered by recorded fixtures
        self.base_url = dataclasses.replace(settings, otari_api_key=key).otari_url()
        self.transport: Any = None  # the replay transport, when offline
        self._key = key
        self._http = http
        self._own_http: httpx.AsyncClient | None = None
        self._children: dict[str, OtariClient] = {}
        self._sdk = openai.AsyncOpenAI(base_url=self.base_url, api_key=key, http_client=http, max_retries=0)

    def with_api_key(self, api_key: str) -> OtariClient:
        """The same client, authenticating as another key (a per-user key with its own spend ceiling)."""
        if api_key == self._key:
            return self
        child = self._children.get(api_key)
        if child is None:
            child = OtariClient(self.settings, self._http, api_key=api_key, offline=self.offline)
            child.transport = self.transport
            self._children[api_key] = child
        return child

    async def aclose(self) -> None:
        """Close the HTTP clients this object created; one passed in stays open."""
        for child in self._children.values():
            await child.aclose()
        if self._http is None:
            await self._sdk.close()
        if self._own_http is not None:
            await self._own_http.aclose()

    # ------------------------------------------------------------------
    # chat completions
    # ------------------------------------------------------------------

    async def chat(
        self,
        step: str,
        model: str,
        messages: list[dict[str, Any]],
        *,
        output: type[T] | None = None,
        tools: list[dict[str, Any]] | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        guardrails: list[dict[str, Any]] | None = None,
        metadata: dict[str, str] | None = None,
        max_tokens: int = 2_000,
        reasoning_effort: str | None = "low",
        max_tool_iterations: int | None = None,
        fallbacks: Sequence[str] = (),
        prompt_sha256: str | None = None,
        stream: bool | None = None,
    ) -> tuple[Any, StepRecord]:
        """Run one completion; returns ``(value, StepRecord)``.

        ``value`` is an instance of ``output`` when given (validated JSON),
        otherwise the answer text. Errors are raised as ``OtariError``
        subclasses carrying the step record in ``record``.
        """
        started = time.monotonic()
        streaming = bool(tools or mcp_servers) if stream is None else stream
        record = StepRecord(step=step, model=model, attempts=0, prompt_sha256=prompt_sha256)
        chain = [model, *(m for m in fallbacks if m != model)]
        for index, candidate in enumerate(chain):
            params = self._params(
                candidate,
                messages,
                output=output,
                tools=tools,
                mcp_servers=mcp_servers,
                guardrails=guardrails,
                metadata=metadata,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                max_tool_iterations=max_tool_iterations,
            )
            try:
                reply = await self._attempt(params, streaming)
            except OtariError as exc:
                record.attempts = (record.attempts or 0) + 1
                record.request_id = exc.request_id or record.request_id
                if exc.fallback_ok and index + 1 < len(chain):
                    log.warning("%s: %s failed (%s); trying %s", step, candidate, exc.label, chain[index + 1])
                    continue
                record.model = candidate
                record.fallback = index > 0
                record.error = exc.label
                record.latency_ms = round((time.monotonic() - started) * 1000)
                if isinstance(exc, GuardrailViolation):
                    record.guardrail = exc.verdict()
                exc.record = record
                raise
            self._fill(record, reply, candidate, fell_back=index > 0, started=started)
            try:
                return self._value(reply, output), record
            except BadModelOutput as exc:
                record.error = exc.label
                exc.record = record
                raise
        raise AssertionError("unreachable: the chain always has a model")  # pragma: no cover

    def _params(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        output: type[BaseModel] | None,
        tools: list[dict[str, Any]] | None,
        mcp_servers: list[dict[str, Any]] | None,
        guardrails: list[dict[str, Any]] | None,
        metadata: dict[str, str] | None,
        max_tokens: int,
        reasoning_effort: str | None,
        max_tool_iterations: int | None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if metadata:
            # Request tags: up to 16 string pairs, recorded on every usage row
            # and never forwarded to the provider (api-reference: Request tags).
            params["metadata"] = metadata
        if reasoning_effort and model in REASONING_EFFORT_MODELS:
            params["reasoning_effort"] = reasoning_effort
        if tools:
            params["tools"] = tools
        elif output is not None and not mcp_servers:
            # Structured output only without tools: a schema-constrained decoder
            # could not emit the tool calls of a gateway tool loop.
            params["response_format"] = response_format(output, output.__name__)
        extra: dict[str, Any] = {}
        if mcp_servers:
            # Inline MCP servers are a top-level field, not a tools entry (mcp: Inline MCP servers).
            extra["mcp_servers"] = mcp_servers
        if guardrails:
            # Request-level guardrails, a sibling of tools (guardrails: Using a guardrail).
            extra["guardrails"] = guardrails
        if max_tool_iterations:
            extra["max_tool_iterations"] = max_tool_iterations
        if extra:
            params["extra_body"] = extra
        return params

    async def _attempt(self, params: dict[str, Any], streaming: bool) -> _Reply:
        """One model, with at most one firewall retry and one retry per optional field it refused."""
        waf_retried = False
        while True:
            try:
                return await self._send(params, streaming)
            except WafBlocked:
                if waf_retried:
                    raise
                waf_retried = True
                log.info("%s: the gateway firewall rejected the text; retrying with look-alike quotes", params["model"])
                params = {**params, "messages": waf_safe(params["messages"])}
            except OtariError as exc:
                optional = _refused_optional_field(exc, params)
                if optional is None:
                    raise
                log.info("%s refused %s; retrying without it", params["model"], optional)
                params = {k: v for k, v in params.items() if k != optional}

    async def _send(self, params: dict[str, Any], streaming: bool) -> _Reply:
        body = dict(params)
        extra_body = body.pop("extra_body", None)
        create = self._sdk.chat.completions.with_raw_response.create
        try:
            if streaming:
                raw = await create(
                    **body,
                    stream=True,
                    stream_options={"include_usage": True},
                    extra_body=extra_body,
                    timeout=TOOL_LOOP_TIMEOUT_S,
                )
                return await _read_stream(raw)
            raw = await create(**body, extra_body=extra_body, timeout=PLAIN_TIMEOUT_S)
            return _read_completion(raw)
        except openai.APIStatusError as exc:
            raise error_from_response(exc.status_code, exc.response.headers, exc.response.content) from exc
        except openai.APITimeoutError as exc:
            raise OtariUnavailable("the gateway did not answer in time", code="timeout") from exc
        except openai.APIConnectionError as exc:
            raise OtariUnavailable("could not reach the gateway", code="connection") from exc
        except openai.APIError as exc:  # an error event inside a stream that had started
            raise error_from_stream_event(exc.body) from exc
        except _TRANSPORT_ERRORS as exc:
            raise OtariUnavailable("the connection to the gateway broke", code="connection") from exc

    @staticmethod
    def _fill(record: StepRecord, reply: _Reply, model: str, *, fell_back: bool, started: float) -> None:
        headers = reply.headers
        try:
            attempts = int(headers.get("otari-attempt-count") or 1)
        except ValueError:
            attempts = 1
        record.model = model
        record.provider = headers.get("otari-provider")
        record.fallback = fell_back or headers.get("otari-fallback") == "true"
        record.attempts = (record.attempts or 0) + attempts
        record.request_id = headers.get("otari-request-id") or record.request_id
        record.cost_usd = _cost(reply.usage)
        record.latency_ms = round((time.monotonic() - started) * 1000)
        record.tool_calls = [_tool_call(call) for call in reply.tool_calls]
        record.guardrail = guardrail_verdict(parse_guardrails_header(headers.get("otari-guardrails")))

    @staticmethod
    def _value(reply: _Reply, output: type[T] | None) -> Any:
        content = reply.content.strip()
        if not content:
            why = "ran out of tokens while reasoning" if reply.finish_reason == "length" else "answered with no text"
            raise BadModelOutput(f"the model {why}", code="empty_answer")
        if output is None:
            return content
        try:
            return output.model_validate(extract_json(content))
        except (ValueError, ValidationError) as exc:
            raise BadModelOutput(f"the answer does not match {output.__name__}", code="bad_json") from exc

    # ------------------------------------------------------------------
    # plain REST calls (models, tools, organization keys and budgets)
    # ------------------------------------------------------------------

    async def rest(self, method: str, path: str, *, json_body: Any = None, timeout: float = 30.0) -> Any:
        """Call ``{base_url}{path}`` with this key; returns the decoded JSON body."""
        http = self._http
        if http is None:
            http = self._own_http = self._own_http or httpx.AsyncClient()
        try:
            response = await http.request(
                method,
                f"{self.base_url}{path}",
                json=json_body,
                timeout=timeout,
                headers={"Authorization": f"Bearer {self._key}"},
            )
        except httpx.HTTPError as exc:
            raise OtariUnavailable(f"could not reach the gateway: {type(exc).__name__}", code="connection") from exc
        if response.status_code >= 400:
            raise error_from_response(response.status_code, response.headers, response.content)
        return response.json() if response.content else None
