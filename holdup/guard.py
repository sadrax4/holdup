"""A guardrail service that Otari can call: ``POST /validate`` and ``GET /profiles``.

Otari posts ``{"profile", "input_text", "validate_kwargs"}`` and reads back
``{"profile", "result": {"valid", "explanation", "score"}}``, where
``valid: false`` means flagged and ``valid: null`` means no verdict. Requests
must carry the per-hold token from ``guard_token`` in
``validate_kwargs.token``; anything else gets a 401, which Otari treats as an
unavailable check (fail closed in block mode).

Profiles: ``checker-injection`` (text written to steer an AI checker) and
``safety``. A model-backed classifier can be plugged in; without one,
``checker-injection`` falls back to the deterministic phrase rules and
``safety`` answers "no verdict". Verdicts are cached by text hash and each
hold is rate limited.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .models import Reason
from .risk.rules import scan_message

log = logging.getLogger(__name__)

MAX_INPUT_CHARS = 200_000
EXPLANATION_MAX = 300

Classifier = Callable[[str, str], Awaitable[Mapping[str, Any]]]
"""``async (profile, text) -> {"valid": bool | None, "explanation": str | None, "score": float | None}``."""


# ----------------------------------------------------------------------
# per-hold tokens (shared with the MCP server)
# ----------------------------------------------------------------------


def hold_token(secret: str, purpose: str, hold_id: int) -> str:
    """``<hold id>.<hex HMAC-SHA256(secret, "<purpose>:<hold id>")>``; the prefix tells a server which hold."""
    if not secret:
        raise ValueError("a secret is required to issue hold tokens")
    if hold_id < 0:
        raise ValueError("hold ids are not negative")
    digest = hmac.new(secret.encode(), f"{purpose}:{hold_id}".encode(), hashlib.sha256).hexdigest()
    return f"{hold_id}.{digest}"


def hold_from_token(secret: str, purpose: str, token: Any) -> int | None:
    """The hold a token was issued for, or ``None`` if it is not a valid ``purpose`` token."""
    if not secret or not isinstance(token, str) or len(token) > 100:
        return None
    hold_part, _, _ = token.partition(".")
    if not (hold_part.isascii() and hold_part.isdigit()):
        return None
    hold_id = int(hold_part)
    return hold_id if hmac.compare_digest(hold_token(secret, purpose, hold_id), token) else None


def guard_token(secret: str, hold_id: int) -> str:
    """The token a pipeline puts in ``validate_kwargs.token`` for checks about ``hold_id``."""
    return hold_token(secret, "guard", hold_id)


# ----------------------------------------------------------------------
# wire format
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class GuardProfile:
    name: str
    guardrail_name: str
    model_id: str
    description: str


PROFILES: dict[str, GuardProfile] = {
    p.name: p
    for p in (
        GuardProfile(
            "checker-injection",
            "holdup_injection_policy",
            "openai/gpt-oss-safeguard-120b",
            "Text written to steer an AI payment checker, such as notes to AI assistants or claims of pre-approval.",
        ),
        GuardProfile("safety", "holdup_llama_guard", "meta/llama-guard-4-12b", "General content safety."),
    )
}


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    profile: str = Field(min_length=1, max_length=128)
    input_text: str = Field(max_length=MAX_INPUT_CHARS)
    validate_kwargs: dict[str, Any] = Field(default_factory=dict)


class GuardVerdict(BaseModel):
    valid: bool | None
    explanation: str | None = None
    score: float | None = None


class ValidateResponse(BaseModel):
    profile: str
    result: GuardVerdict


def _coerce(raw: Any) -> GuardVerdict:
    """A classifier answer in the wire shape, whatever it returned."""
    if not isinstance(raw, Mapping):
        return GuardVerdict(valid=None, explanation="classifier returned no verdict")
    valid = raw.get("valid")
    explanation = raw.get("explanation")
    score = raw.get("score")
    if isinstance(score, bool) or not isinstance(score, int | float) or not math.isfinite(score):
        score = None
    return GuardVerdict(
        valid=valid if isinstance(valid, bool) else None,
        explanation=str(explanation)[:EXPLANATION_MAX] if explanation is not None else None,
        score=min(max(float(score), 0.0), 1.0) if score is not None else None,
    )


# ----------------------------------------------------------------------
# the service behind the routes
# ----------------------------------------------------------------------


class _RateLimit:
    """Sliding one-minute window per key."""

    def __init__(self, per_minute: int, clock: Callable[[], float]) -> None:
        self.per_minute = per_minute
        self._clock = clock
        self._hits: dict[Any, deque[float]] = {}

    def retry_after(self, key: Any) -> float | None:
        """Record a hit; ``None`` if allowed, else the seconds until the next one would be."""
        now = self._clock()
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - 60.0:
            hits.popleft()
        if len(hits) >= self.per_minute:
            return max(hits[0] + 60.0 - now, 0.0)
        hits.append(now)
        return None


class GuardService:
    """Token check, rate limits, cache and the classifier (or the rules) for the routes."""

    def __init__(
        self,
        secret: str,
        classifier: Classifier | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        cache_size: int = 512,
        cache_ttl_s: float = 600.0,
        per_hold_per_minute: int = 30,
        total_per_minute: int = 600,
        classifier_timeout_s: float = 25.0,
    ) -> None:
        if not secret:
            raise ValueError("HOLDUP_SECRET is required for the guardrail endpoint")
        self._secret = secret
        self.classifier = classifier
        self._clock = clock
        self._cache: OrderedDict[tuple[str, str], tuple[float, GuardVerdict]] = OrderedDict()
        self._cache_size = cache_size
        self._cache_ttl_s = cache_ttl_s
        self._per_hold = _RateLimit(per_hold_per_minute, clock)
        self._total = _RateLimit(total_per_minute, clock)
        self._timeout_s = classifier_timeout_s
        self._inflight: dict[tuple[str, str], asyncio.Future[tuple[GuardVerdict, bool]]] = {}

    def authorize(self, validate_kwargs: Mapping[str, Any]) -> int | None:
        return hold_from_token(self._secret, "guard", validate_kwargs.get("token"))

    def retry_after(self, hold_id: int) -> float | None:
        """``None`` when a check for ``hold_id`` may run now, else seconds to wait."""
        wait = self._per_hold.retry_after(hold_id)
        return wait if wait is not None else self._total.retry_after("all")

    async def check(self, profile: str, text: str) -> GuardVerdict:
        key = (profile, hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest())
        cached = self._cache.get(key)
        if cached is not None and cached[0] > self._clock():
            self._cache.move_to_end(key)
            return cached[1]
        pending = self._inflight.get(key)
        if pending is None:
            pending = asyncio.ensure_future(self._evaluate(profile, text))
            self._inflight[key] = pending
            pending.add_done_callback(lambda _: self._inflight.pop(key, None))
        verdict, cacheable = await asyncio.shield(pending)
        if cacheable:
            self._cache[key] = (self._clock() + self._cache_ttl_s, verdict)
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return verdict

    async def _evaluate(self, profile: str, text: str) -> tuple[GuardVerdict, bool]:
        """The verdict, and whether it may be cached (failures are not)."""
        if self.classifier is None:
            return await asyncio.to_thread(rules_verdict, profile, text), True
        try:
            raw = await asyncio.wait_for(self.classifier(profile, text), self._timeout_s)
        except Exception as exc:
            log.warning("guard classifier failed for %s: %r; using the rules", profile, exc)
            after = f"model check failed ({type(exc).__name__})"
            return await asyncio.to_thread(rules_verdict, profile, text, after=after), False
        return _coerce(raw), True


def rules_verdict(profile: str, text: str, *, after: str = "") -> GuardVerdict:
    """The verdict without a model: phrase rules for injection, no verdict for general safety."""
    if profile != "checker-injection":
        return GuardVerdict(valid=None, explanation=f"{after}; no verdict" if after else "no safety model configured")
    prefix = f"{after}; " if after else ""
    found = next((s for s in scan_message(text) if s.reason is Reason.CHECKER_INJECTION), None)
    if found is None:
        return GuardVerdict(valid=True, explanation=f"{prefix}rules: no checker-injection phrases", score=0.0)
    return GuardVerdict(valid=False, explanation=f"{prefix}rules: {found.detail}", score=1.0)


# ----------------------------------------------------------------------
# routes
# ----------------------------------------------------------------------


def _error(status: int, detail: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status, headers=headers)


def build_router(settings: Settings, classifier: Classifier | None = None, **options: Any) -> APIRouter:
    """Routes for ``POST /validate`` and ``GET /profiles``; ``options`` go to ``GuardService``.

    Otari's deployment-wide guardrails URL is a service root that it posts
    ``/validate`` to; for a request-level ``url`` the docs do not say whether it
    is used as given or as a root. The routes are therefore also served under
    ``/validate/``, so either reading of ``<public url>/validate`` works.
    """
    service = GuardService(settings.secret, classifier, **options)
    router = APIRouter(tags=["guardrail"])

    async def validate(body: ValidateRequest) -> ValidateResponse | JSONResponse:
        hold_id = service.authorize(body.validate_kwargs)
        if hold_id is None:
            return _error(401, "validate_kwargs.token is missing or invalid")
        if body.profile not in PROFILES:
            return _error(404, f"unknown profile {body.profile[:64]!r}")
        wait = service.retry_after(hold_id)
        if wait is not None:
            return _error(429, "too many checks", {"Retry-After": str(max(1, math.ceil(wait)))})
        verdict = await service.check(body.profile, body.input_text)
        log.info("guard %s for hold %s: valid=%s", body.profile, hold_id, verdict.valid)
        return ValidateResponse(profile=body.profile, result=verdict)

    async def profiles() -> list[dict[str, str]]:
        return [{"name": p.name, "guardrail_name": p.guardrail_name, "model_id": p.model_id} for p in PROFILES.values()]

    for prefix, visible in (("", True), ("/validate", False)):
        router.add_api_route(f"{prefix}/validate", validate, methods=["POST"], response_model=ValidateResponse,
                             include_in_schema=visible)
        router.add_api_route(f"{prefix}/profiles", profiles, methods=["GET"], include_in_schema=visible)
    return router
