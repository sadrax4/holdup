"""The JSON objects that each Otari step must answer with.

Validation is forgiving where a model is merely untidy (unknown fields are
ignored, labels are normalised, long strings are cut, list items with an
unknown reason are dropped) and strict where it matters: a field that is
missing or of the wrong type fails the step, and a failed step keeps the brake
on. ``strict_schema`` turns a contract into the strict JSON schema sent as
``response_format`` to steps that run without tools.
"""

from __future__ import annotations

import copy
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict

FINDING_REASONS = (
    "NEW_ACCOUNT",
    "MULE_PATTERN",
    "IMPERSONATION",
    "URGENCY",
    "IMPOSSIBLE_RETURNS",
    "CHECKER_INJECTION",
    "DETAILS_CHANGED",
    "PAYEE_REPORTED",
    "PHISHING_LINK",
    "PUBLIC_WARNING",
    "LOOKALIKE_PAYEE",
    "AMOUNT_MISMATCH",
    "SECRET_REQUEST",
)
"""Reasons a model may report. Failure and meta reasons are set by HoldUp only."""

CATEGORIES = (
    "security_alert",
    "payment_request",
    "invoice",
    "investment_offer",
    "giveaway",
    "personal",
    "support_contact",
    "other",
)
IMAGE_KINDS = ("invoice", "chat_message", "login_page", "payment_request", "qr_code", "document", "other")
TOOLS = ("near-intel", "web_search", "web_fetch", "code_execution", "message")

FindingReason = Literal[FINDING_REASONS]
Category = Literal[CATEGORIES]
ImageKind = Literal[IMAGE_KINDS]
ToolName = Literal[TOOLS]

_TOOL_ALIASES = {
    "near_intel": "near-intel",
    "nearintel": "near-intel",
    "mcp": "near-intel",
    "payee_profile": "near-intel",
    "payee_reputation": "near-intel",
    "sender_history": "near-intel",
    "hold_status": "near-intel",
    "fetch_page": "near-intel",
    "search": "web_search",
    "fetch": "web_fetch",
    "code": "code_execution",
    "python": "code_execution",
}


# ----------------------------------------------------------------------
# small normalisers
# ----------------------------------------------------------------------


def _cut(limit: int, *, keep_lines: bool = False) -> AfterValidator:
    def cut(value: str) -> str:
        if keep_lines:
            lines = (" ".join(line.split()) for line in value.splitlines())
            return "\n".join(line for line in lines if line)[:limit]
        return " ".join(value.split())[:limit]

    return AfterValidator(cut)


def _lower(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


def _known(allowed: tuple[str, ...]) -> BeforeValidator:
    """Keep only the list entries that are one of ``allowed`` (case-insensitive)."""

    def keep(value: Any) -> Any:
        if not isinstance(value, list):
            return value
        out = []
        for item in value:
            label = _lower(item)
            if label in allowed and label not in out:
                out.append(label)
        return out

    return BeforeValidator(keep)


def _with_known_reason(value: Any) -> Any:
    """Drop list items whose ``reason`` is not one a model may report."""
    if not isinstance(value, list):
        return value
    kept = []
    for item in value:
        if isinstance(item, dict) and isinstance(item.get("reason"), str):
            reason = item["reason"].strip().upper()
            if reason in FINDING_REASONS:
                kept.append({**item, "reason": reason})
    return kept


def _tool_name(value: Any) -> Any:
    """Map the many ways a model names a tool onto ``TOOLS``; unknown names mean the message."""
    if not isinstance(value, str):
        return value
    name = value.strip().lower().replace(" ", "_").replace("-", "_").removeprefix("otari_")
    if name.startswith("near_intel"):
        return "near-intel"
    if name in TOOLS:
        return name
    return _TOOL_ALIASES.get(name, "message")


def _strings(value: Any) -> Any:
    if isinstance(value, list):
        return [str(item).strip() for item in value if isinstance(item, (str, int, float)) and str(item).strip()]
    return value


Short = Annotated[str, _cut(200)]
Label = Annotated[str, _cut(80)]
Amount = Annotated[str, _cut(40)]
Level = Annotated[Literal["low", "medium", "high"], BeforeValidator(_lower)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ----------------------------------------------------------------------
# contracts
# ----------------------------------------------------------------------


class MoneyClaim(Contract):
    label: Label
    amount: Amount
    currency: Annotated[str, _cut(16)]


class TriageResult(Contract):
    categories: Annotated[list[Category], _known(CATEGORIES)]
    urgency: bool
    impersonation: bool
    secret_request: bool
    details_changed: bool
    injection_suspected: bool
    impossible_returns: bool
    money_claims: list[MoneyClaim]
    risk: Level
    summary: Short


class VisionResult(Contract):
    transcript: Annotated[str, _cut(1500, keep_lines=True)]
    kind: Annotated[ImageKind, BeforeValidator(_lower)]
    urgency: bool
    impersonation: bool
    secret_request: bool
    details_changed: bool
    injection_suspected: bool
    money_claims: list[MoneyClaim]
    summary: Short


class AmountCheck(Contract):
    line_items: Annotated[list[Amount], BeforeValidator(_strings)]
    stated_total: Amount
    computed_total: Amount
    currency: Annotated[str, _cut(16)] = ""


class Computation(Contract):
    label: Label = ""
    code: Annotated[str, _cut(2000, keep_lines=True)] = ""
    result: Annotated[str, _cut(400, keep_lines=True)] = ""


class InvestigatorFinding(Contract):
    reason: FindingReason
    tool: Annotated[ToolName, BeforeValidator(_tool_name)] = "message"
    detail: Short = ""
    citations: Annotated[list[str], BeforeValidator(_strings)] = []
    computation: Label = ""


class InvestigatorResult(Contract):
    findings: Annotated[list[InvestigatorFinding], BeforeValidator(_with_known_reason)] = []
    computations: list[Computation] = []
    amount_check: AmountCheck | None = None
    brake: bool = False
    summary: Short = ""


class ReviewerReason(Contract):
    reason: FindingReason
    detail: Short = ""


class ReviewerResult(Contract):
    brake: bool
    reasons: Annotated[list[ReviewerReason], BeforeValidator(_with_known_reason)] = []
    amount_check: AmountCheck | None = None
    confidence: Level = "medium"
    summary: Short = ""


class PolicyVerdict(Contract):
    """What gpt-oss-safeguard answers for a written policy."""

    violation: Annotated[int, BeforeValidator(lambda v: int(v) if isinstance(v, (bool, str, int)) else v)]
    rationale: Short = ""


# ----------------------------------------------------------------------
# strict JSON schema for response_format
# ----------------------------------------------------------------------


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The contract as a self-contained strict JSON schema.

    Every object lists all of its properties as required and allows no others,
    references are inlined, and titles and defaults are dropped, which is the
    subset that structured-output decoders accept.
    """
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return walk(copy.deepcopy(defs[node["$ref"].rsplit("/", 1)[-1]]))
        out = {k: walk(v) for k, v in node.items() if k not in ("title", "default")}
        if out.get("type") == "object" and "properties" in out:
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out

    return walk(raw)


def response_format(model: type[BaseModel], name: str) -> dict[str, Any]:
    """``response_format`` for a chat completion that must answer with ``model``."""
    return {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": strict_schema(model)}}
