"""Canonical evidence JSON: the document whose sha256 the oracle records on-chain.

The same bytes go into the ``extend_hold`` transaction and are served by the
web app, so anyone can re-hash them and compare with the hold's
``evidence_hash``. The document carries reason codes, short details (scrubbed
of identifiers and quotations), citations reduced to ``scheme://host/path``,
and the model steps; never the forwarded text, chat ids or Telegram ids. It is
at most 4096 bytes: when a verdict is too large, citations, details and step
details are dropped in a fixed order until it fits.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ..models import Finding, Hold, StepRecord, Verdict, reasons_from_bits
from .reasons import note_for

__all__ = [
    "EVIDENCE_VERSION",
    "MAX_EVIDENCE_BYTES",
    "build_evidence",
    "canonical_json",
    "clean_citation",
    "clean_detail",
    "evidence_hash",
    "note_for",
]

EVIDENCE_VERSION = 1
MAX_EVIDENCE_BYTES = 4096
DETAIL_MAX = 160
MAX_CITATIONS = 2
MAX_STEPS = 12
MAX_TOOLS = 8
_GUARDRAIL_KEYS = ("profile", "mode", "stage", "valid", "flagged", "verdict", "action", "status", "score")


def canonical_json(value: Any) -> str:
    """Sorted keys, no whitespace, UTF-8 text; NaN and infinity are refused."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def evidence_hash(evidence_json: str) -> str:
    """sha256 hex of the evidence bytes: what ``extend_hold`` stores as ``evidence_hash``."""
    return hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------
# scrubbing free text
# ----------------------------------------------------------------------

_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff\U000e0000-\U000e007f]")
_TELEGRAM_LINK = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/\S*|\btg://\S+", re.IGNORECASE)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_HANDLE = re.compile(r"(?<![\w@.])@[A-Za-z][A-Za-z0-9_]{3,31}\b")
_LONG_NUMBER = re.compile(r"(?<![\w.,])[-+]?\d{7,}(?![\w.,]?\d)")
_PHONE = re.compile(r"(?<!\w)\+\d[\d\s().-]{6,}\d")
_QUOTED = re.compile(
    '"[^"]*"'
    '|\u201c[^\u201d]*\u201d'
    '|\u00ab[^\u00bb]*\u00bb'
    r"|(?<!\w)'[^'\n]{3,200}'(?!\w)"
)


def _redact_quote(match: re.Match[str]) -> str:
    inner = match.group(0)[1:-1]
    return "[quote]" if len(inner.split()) >= 3 or len(inner) > 24 else match.group(0)


def clean_detail(text: str | None, limit: int = DETAIL_MAX) -> str:
    """A finding detail made safe for a public record.

    Removes control and invisible characters, redacts Telegram links and
    handles, e-mail addresses, phone numbers and long digit runs (chat and user
    ids), replaces quotations of three or more words with ``[quote]``, and
    clips to ``limit`` characters.
    """
    if not text or limit <= 0:
        return ""
    text = _CONTROL.sub(" ", text)
    text = _TELEGRAM_LINK.sub("[link]", text)
    text = _EMAIL.sub("[email]", text)
    text = _HANDLE.sub("[handle]", text)
    text = _PHONE.sub("[number]", text)
    text = _LONG_NUMBER.sub("[number]", text)
    text = _QUOTED.sub(_redact_quote, text)
    return _clip(re.sub(r"\s+", " ", text).strip(), limit)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..." if limit > 3 else text[:limit]


def _short(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    return _clip(_CONTROL.sub("", str(value)).strip(), limit)


def clean_citation(citation: Any) -> str | None:
    """``scheme://host/path`` of a web citation (no query, fragment or credentials), or a short tool label."""
    if not isinstance(citation, str):
        return None
    citation = citation.strip()
    if re.fullmatch(r"[a-z][a-z0-9_.:-]{0,63}", citation) and "//" not in citation:
        return citation  # e.g. "mcp:payee_profile"
    try:
        parts = urlsplit(citation)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host or _TELEGRAM_LINK.match(f"{host}/"):
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return _clip(f"{parts.scheme}://{netloc}{parts.path or '/'}", 200)


# ----------------------------------------------------------------------
# entries
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Reason:
    bit: int
    reason: str
    source: str
    model: str | None
    detail: str
    citations: tuple[str, ...]

    def sort_key(self) -> tuple[Any, ...]:
        return (self.bit, self.source, self.model or "", self.detail, self.citations)


def _reason_entries(findings: Iterable[Finding]) -> list[_Reason]:
    """One entry per reason bit of each finding, deduplicated and in canonical order."""
    entries: set[_Reason] = set()
    for finding in findings:
        citations = tuple(dict.fromkeys(c for c in map(clean_citation, finding.citations) if c))[:MAX_CITATIONS]
        for reason in reasons_from_bits(int(finding.reason)):
            entries.add(_Reason(
                bit=int(reason),
                reason=reason.name or str(int(reason)),
                source=_short(finding.source, 32) or "unknown",
                model=_short(finding.model, 64),
                detail=clean_detail(finding.detail),
                citations=citations,
            ))
    return sorted(entries, key=_Reason.sort_key)


def _merged(entries: list[_Reason]) -> list[_Reason]:
    """One entry per bit: sources joined, the model kept only if all agree."""
    by_bit: dict[int, list[_Reason]] = {}
    for entry in entries:
        by_bit.setdefault(entry.bit, []).append(entry)
    merged = []
    for bit, group in sorted(by_bit.items()):
        models = {e.model for e in group}
        merged.append(_Reason(
            bit=bit,
            reason=group[0].reason,
            source=_clip("+".join(sorted({e.source for e in group})), 64),
            model=models.pop() if len(models) == 1 else None,
            detail=next((e.detail for e in group if e.detail), ""),
            citations=(),
        ))
    return merged


def _number(value: Any, digits: int | None = None) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return round(value, digits) if digits is not None else int(value)


def _guardrail_entry(value: Mapping[str, Any]) -> dict[str, Any] | None:
    flat = {**value, **value["result"]} if isinstance(value.get("result"), Mapping) else dict(value)
    summary: dict[str, Any] = {}
    for key in _GUARDRAIL_KEYS:
        item = flat.get(key)
        if isinstance(item, bool) or (key == "valid" and key in flat and item is None):
            summary[key] = item  # valid: null means the check gave no verdict
        elif isinstance(item, int | float):
            if (number := _number(item, 4)) is not None:
                summary[key] = number
        elif isinstance(item, str):
            summary[key] = _short(item, 32)
    return summary or None


def _guardrail(value: Any) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Verdict fields of a guardrail record, or of each entry of an ``Otari-Guardrails`` list.

    Explanations and any other text are left out.
    """
    if isinstance(value, Mapping):
        return _guardrail_entry(value)
    if isinstance(value, list | tuple):
        entries = [e for e in (_guardrail_entry(v) for v in value[:4] if isinstance(v, Mapping)) if e]
        return entries or None
    return None


def _tool_names(calls: Iterable[Any]) -> list[str]:
    names = set()
    for call in calls:
        if isinstance(call, Mapping):
            name = call.get("name") or call.get("type") or call.get("tool")
            if name:
                names.add(_short(name, 48) or "")
    return sorted(n for n in names if n)[:MAX_TOOLS]


def _step_entry(step: StepRecord, *, detailed: bool) -> dict[str, Any]:
    return {
        "step": _short(step.step, 32),
        "model": _short(step.model, 64),
        "provider": _short(step.provider, 48),
        "fallback": step.fallback if isinstance(step.fallback, bool) else None,
        "attempts": _number(step.attempts),
        "request_id": _short(step.request_id, 64),
        "cost_usd": _number(step.cost_usd, 6),
        "latency_ms": _number(step.latency_ms),
        "tools": _tool_names(step.tool_calls) if detailed else [],
        "guardrail": _guardrail(step.guardrail) if detailed else None,
    }


# ----------------------------------------------------------------------
# the document
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Level:
    """How much detail a rendering keeps; the levels are tried in order until one fits."""

    citations: int
    detail: int
    step_detail: bool = True
    merge: bool = False
    steps: bool = True
    prompts: bool = True
    bare: bool = False  # reason code and a short source only; always fits (at most 17 reasons)


_LEVELS = (
    _Level(citations=2, detail=DETAIL_MAX),
    _Level(citations=1, detail=DETAIL_MAX),
    _Level(citations=0, detail=DETAIL_MAX),
    _Level(citations=0, detail=80),
    _Level(citations=0, detail=80, step_detail=False),
    _Level(citations=0, detail=40, step_detail=False, merge=True),
    _Level(citations=0, detail=0, step_detail=False, merge=True),
    _Level(citations=0, detail=0, step_detail=False, merge=True, steps=False),
    _Level(citations=0, detail=0, step_detail=False, merge=True, steps=False, prompts=False),
    _Level(citations=0, detail=0, step_detail=False, merge=True, steps=False, prompts=False, bare=True),
)


def _render_reason(entry: _Reason, level: _Level) -> dict[str, Any]:
    return {
        "bit": entry.bit,
        "reason": entry.reason,
        "source": _clip(entry.source, 16) if level.bare else entry.source,
        "model": None if level.bare else entry.model,
        "detail": _clip(entry.detail, level.detail) if level.detail else "",
        "citations": list(entry.citations[: level.citations]),
    }


def build_evidence(
    hold: Hold,
    verdict: Verdict,
    *,
    network: str,
    vault_id: str,
    prompt_versions: Mapping[str, str],
    decided_ms: int | None = None,
) -> str:
    """The canonical evidence JSON for ``verdict`` on ``hold`` (UTF-8, at most 4096 bytes).

    ``prompt_versions`` maps a step to its prompt's sha256 (kept to 16 hex
    characters). ``decided_ms`` defaults to the current time.
    """
    entries = _reason_entries(verdict.findings)
    merged = _merged(entries)
    steps = verdict.steps[:MAX_STEPS]
    prompts = {
        _short(step, 32): _CONTROL.sub("", str(digest))[:16]
        for step, digest in sorted(prompt_versions.items())[:MAX_STEPS]
    }
    base = {
        "v": EVIDENCE_VERSION,
        "hold": int(hold.id),
        "vault": _short(vault_id, 64),
        "network": _short(network, 32),
        "payee": _short(hold.payee, 64),
        "asset": _short(hold.asset, 64),
        "amount": str(int(hold.amount)),
        "decided_ms": int(time.time() * 1000) if decided_ms is None else int(decided_ms),
    }
    for level in _LEVELS:
        document = {
            **base,
            "reasons": [_render_reason(e, level) for e in (merged if level.merge else entries)],
            "steps": [_step_entry(s, detailed=level.step_detail) for s in steps] if level.steps else [],
            "prompts": prompts if level.prompts else {},
        }
        text = canonical_json(document)
        if len(text.encode("utf-8")) <= MAX_EVIDENCE_BYTES:
            return text
    raise ValueError(f"evidence for hold {hold.id} does not fit in {MAX_EVIDENCE_BYTES} bytes")
