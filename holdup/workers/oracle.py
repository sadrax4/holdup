"""The oracle turns a braking verdict into a longer hold with evidence on-chain.

Its only key can call ``extend_hold``, and the contract only accepts later
release times (capped at created + max hold), so the worst a broken or
tricked oracle can do is make a payment wait longer.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..chain import errors as E
from ..chain.errors import VaultError
from ..interfaces import VaultPort
from ..models import Hold, TxResult, VaultConfig, Verdict

log = logging.getLogger(__name__)

MAX_EVIDENCE_BYTES = 4096
DETAIL_MAX = 160

EvidenceBuilder = Callable[..., str]


@dataclass
class OracleResult:
    hold_id: int
    extended: bool
    release_at_ms: int
    reason_bits: int = 0
    evidence_json: str | None = None
    evidence_hash: str | None = None
    tx: TxResult | None = None
    skipped: str | None = None  # "clean", "missing", "inactive" or "not_longer" when nothing was sent


def evidence_digest(evidence_json: str) -> str:
    """sha256 hex of the evidence bytes, exactly what the contract stores."""
    return hashlib.sha256(evidence_json.encode()).hexdigest()


def _load_builder() -> EvidenceBuilder:
    try:
        from ..risk.evidence import build_evidence
    except ImportError:
        return fallback_evidence
    except Exception:  # a broken risk module must not stop the brake
        log.exception("holdup.risk.evidence failed to import; using the built-in evidence format")
        return fallback_evidence
    return build_evidence


class Oracle:
    """Applies verdicts to holds through ``extend_hold``. Safe to call twice."""

    def __init__(
        self,
        vault: VaultPort,
        store: Any,
        clock: Callable[[], int] | None = None,
        *,
        network: str = "offline",
        evidence_builder: EvidenceBuilder | None = None,
    ) -> None:
        self.vault = vault
        self.store = store
        self.clock = clock
        self.network = network
        self._builder = evidence_builder or _load_builder()
        self._config: VaultConfig | None = None
        self._locks: dict[int, asyncio.Lock] = {}
        self._applied: dict[str, OracleResult] = {}

    async def apply(
        self, hold: Hold, verdict: Verdict, *, release_at_ms: int | None = None
    ) -> OracleResult:
        """Extend ``hold`` to ``max(release_at, now + verdict.hold_for_ms)``, clamped to the cap.

        ``release_at_ms`` replaces ``now + hold_for_ms`` with an absolute target
        (used by the fail-closed policy). Applying the same verdict again
        returns the first result without sending anything.
        """
        key = _verdict_key(hold.id, verdict, release_at_ms)
        lock = self._locks.setdefault(hold.id, asyncio.Lock())
        async with lock:
            if key in self._applied:
                return self._applied[key]
            result = await self._apply(hold.id, verdict, release_at_ms)
            self._applied[key] = result
            return result

    async def _apply(self, hold_id: int, verdict: Verdict, absolute: int | None) -> OracleResult:
        bits = verdict.reason_bits
        if not verdict.brake or bits == 0 or (absolute is None and verdict.hold_for_ms <= 0):
            current = await self.vault.get_hold(hold_id)
            return _unchanged(hold_id, current, "clean")
        max_hold_ms = (await self._vault_config()).max_hold_ms
        for attempt in range(2):
            current = await self.vault.get_hold(hold_id)
            if current is None:
                return _unchanged(hold_id, None, "missing")
            if not current.active:
                return _unchanged(hold_id, current, "inactive")
            now = await self._now()
            wanted = absolute if absolute is not None else now + verdict.hold_for_ms
            target = min(max(current.release_at_ms, wanted), current.created_ms + max_hold_ms)
            if target <= current.release_at_ms:
                return _unchanged(hold_id, current, "not_longer")
            evidence = self._evidence(current, verdict, now)
            try:
                tx = await self.vault.extend_hold(hold_id, target, bits, evidence)
            except VaultError as exc:
                if exc.code == E.E_HOLD_NOT_ACTIVE:
                    return _unchanged(hold_id, await self.vault.get_hold(hold_id), "inactive")
                if attempt == 0 and exc.code in (E.E_ONLY_LONGER, E.E_ABOVE_MAX_HOLD):
                    continue  # another extension landed first; re-read and recompute
                raise
            digest = evidence_digest(evidence)
            self.store.save_evidence(hold_id, evidence, digest)
            self.store.record_tx(hold_id, "extend", tx.tx_hash)
            return OracleResult(
                hold_id=hold_id,
                extended=True,
                release_at_ms=target,
                reason_bits=current.reason_bits | bits,
                evidence_json=evidence,
                evidence_hash=digest,
                tx=tx,
            )
        return _unchanged(hold_id, await self.vault.get_hold(hold_id), "not_longer")

    async def _now(self) -> int:
        return int(self.clock()) if self.clock is not None else await self.vault.now_ms()

    async def _vault_config(self) -> VaultConfig:
        if self._config is None:
            self._config = await self.vault.get_config()
        return self._config

    def _evidence(self, hold: Hold, verdict: Verdict, now: int) -> str:
        prompts = {s.step: s.prompt_sha256[:16] for s in verdict.steps if s.prompt_sha256}
        kwargs = {"network": self.network, "vault_id": self.vault.vault_id, "prompt_versions": prompts}
        try:
            if self._builder is fallback_evidence:
                text = fallback_evidence(hold, verdict, **kwargs, decided_ms=now)
            else:
                text = self._builder(hold, verdict, **kwargs)
        except Exception:
            log.exception("evidence builder failed for hold %s; using the built-in format", hold.id)
            text = fallback_evidence(hold, verdict, **kwargs, decided_ms=now)
        if len(text.encode()) > MAX_EVIDENCE_BYTES:
            text = fallback_evidence(hold, verdict, **kwargs, decided_ms=now, compact=True)
        return text


def _unchanged(hold_id: int, hold: Hold | None, why: str) -> OracleResult:
    return OracleResult(
        hold_id=hold_id,
        extended=False,
        release_at_ms=hold.release_at_ms if hold else 0,
        reason_bits=hold.reason_bits if hold else 0,
        skipped=why,
    )


def _verdict_key(hold_id: int, verdict: Verdict, absolute: int | None) -> str:
    material = {
        "hold": hold_id,
        "brake": verdict.brake,
        "provisional": verdict.provisional,
        "hold_for_ms": verdict.hold_for_ms,
        "absolute": absolute,
        "findings": sorted(
            (int(f.reason.value), f.source, f.model or "", f.detail) for f in verdict.findings
        ),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def fallback_evidence(
    hold: Hold,
    verdict: Verdict,
    *,
    network: str,
    vault_id: str,
    prompt_versions: dict[str, str],
    decided_ms: int | None = None,
    compact: bool = False,
) -> str:
    """Canonical evidence JSON in the shared schema, used when the risk package has none.

    Sorted keys, compact separators, no raw message text, at most 4096 bytes.
    """
    reasons = [
        {
            "bit": int(f.reason.value),
            "reason": f.reason.name,
            "source": f.source,
            "model": f.model,
            "detail": "" if compact else f.detail[:DETAIL_MAX],
            "citations": [] if compact else list(f.citations[:2]),
        }
        for f in verdict.findings
    ]
    steps = [
        {
            "step": s.step,
            "model": s.model,
            "provider": s.provider,
            "fallback": s.fallback,
            "attempts": s.attempts,
            "request_id": s.request_id,
            "cost_usd": s.cost_usd,
            "latency_ms": s.latency_ms,
            "tools": [str(call.get("name", call.get("type", ""))) for call in s.tool_calls],
            "guardrail": s.guardrail,
        }
        for s in verdict.steps
    ]
    doc = {
        "v": 1,
        "hold": hold.id,
        "vault": vault_id,
        "network": network,
        "payee": hold.payee,
        "asset": hold.asset,
        "amount": str(hold.amount),
        "reasons": reasons,
        "steps": [] if compact else steps,
        "prompts": prompt_versions,
        "decided_ms": decided_ms,
    }
    text = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    while len(text.encode()) > MAX_EVIDENCE_BYTES and doc["reasons"]:
        doc["reasons"] = doc["reasons"][:-1]
        text = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text
