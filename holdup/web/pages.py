"""Public pages: landing, hold timeline, evidence JSON, AI steps, proof, stats and CSV export."""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates

from ..bot.samples import stable_symbol
from ..chain.fake import FakeVault
from ..chain.prove import ProveReport, plan_attempts, prove_fake
from ..chain.verify import VerifyReport, verify_deploy, verify_fake
from ..config import Settings
from ..interfaces import VaultPort
from ..models import VaultConfig
from . import views
from .signing import read_export_token

log = logging.getLogger(__name__)

PROOF_TTL_S = 60.0
PROOF_ERROR_TTL_S = 10.0
PROOF_TIMEOUT_S = 20.0

ROBOTS = "User-agent: *\nDisallow: /fixtures/\nDisallow: /console\nDisallow: /export/\nDisallow: /tg/\nDisallow: /mcp\n"


@dataclass(frozen=True)
class CheckRow:
    ok: bool
    title: str
    explanation: str
    expected: str
    observed: str
    detail: str


@dataclass(frozen=True)
class AttemptRow:
    actor: str
    description: str
    expected: str
    observed: str
    passed: bool | None  # None: listed, not run here


@dataclass
class ProofView:
    network: str
    simulated: bool
    ok: bool | None
    checks: list[CheckRow] = field(default_factory=list)
    attempts: list[AttemptRow] = field(default_factory=list)
    error: str = ""
    checked_ms: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "network": self.network,
            "simulated": self.simulated,
            "ok": self.ok,
            "error": self.error or None,
            "checked_ms": self.checked_ms,
            "checks": [c.__dict__ for c in self.checks],
            "attempts": [a.__dict__ for a in self.attempts],
        }


def proof_view(
    report: VerifyReport | None,
    attempts: ProveReport | None,
    settings: Settings,
    *,
    checked_ms: int,
    error: str = "",
) -> ProofView:
    checks = []
    for check in report.checks if report else []:
        title, explanation = views.check_title(check.name)
        checks.append(CheckRow(check.ok, title, explanation, check.expected, check.observed, check.detail))
    if attempts is not None:
        rows = [
            AttemptRow(r.actor, r.description, ", ".join(r.expected), r.observed, r.passed) for r in attempts.results
        ]
    else:
        rows = [AttemptRow(a.actor, a.description, ", ".join(a.expected), "", None) for a in plan_attempts(settings)]
    return ProofView(
        network=settings.network,
        simulated=bool(report and report.simulated),
        ok=report.ok if report else None,
        checks=checks,
        attempts=rows,
        error=error,
        checked_ms=checked_ms,
    )


def network_label(settings: Settings) -> str:
    return {
        "offline": "Offline demo: simulated chain",
        "sandbox": "NEAR sandbox",
        "testnet": "NEAR testnet",
        "mainnet": "NEAR mainnet",
    }.get(settings.network, settings.network)


class Site:
    """Routes for the public pages, sharing a cached vault config and proof report."""

    def __init__(
        self,
        settings: Settings,
        engine: Any,
        vault: VaultPort,
        store: Any,
        templates: Jinja2Templates,
        *,
        secret: str,
        telegram_username: Callable[[], str] = lambda: "",
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.vault = vault
        self.store = store
        self.templates = templates
        self.secret = secret
        self.telegram_username = telegram_username
        self.simulated = bool(getattr(vault, "simulated", False))
        self._config: VaultConfig | None = None
        self._proof: tuple[float, ProofView] | None = None
        self._proof_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # shared helpers
    # ------------------------------------------------------------------

    def render(self, request: Request, name: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
        return self.templates.TemplateResponse(request, name, context, status_code=status_code)

    async def vault_config(self) -> VaultConfig:
        """Fixed when the vault was created, so read it once."""
        if self._config is None:
            self._config = await self.vault.get_config()
        return self._config

    async def now_ms(self) -> int:
        """Chain time for the simulated vault (it may run on a test clock), wall time otherwise."""
        if self.simulated:
            return await self.vault.now_ms()
        return int(time.time() * 1000)

    def _base(self) -> str:
        return self.settings.public_base_url

    # ------------------------------------------------------------------
    # routes
    # ------------------------------------------------------------------

    def router(self) -> APIRouter:
        router = APIRouter()
        html_pages: dict[str, Callable[..., Awaitable[Any]]] = {
            "/": self.landing,
            "/h/{hold_id:int}": self.hold_page,
            "/why/{hold_id:int}": self.why_page,
            "/prove": self.prove_page,
            "/stats": self.stats_page,
        }
        json_views: dict[str, Callable[..., Awaitable[Any]]] = {
            "/h/{hold_id:int}.json": self.hold_json,
            "/v/{hold_id:int}.json": self.evidence,
            "/prove.json": self.prove_json,
            "/stats.json": self.stats_json,
        }
        for path, endpoint in html_pages.items():
            router.add_api_route(path, endpoint, methods=["GET"], response_class=HTMLResponse, include_in_schema=False)
        for path, endpoint in json_views.items():
            router.add_api_route(path, endpoint, methods=["GET"])
        router.add_api_route("/export/{token}.csv", self.export, methods=["GET"], include_in_schema=False)
        router.add_api_route("/robots.txt", self.robots, methods=["GET"], include_in_schema=False)
        return router

    async def landing(self, request: Request) -> HTMLResponse:
        try:
            stats = await self.engine.stats()
        except Exception:
            log.exception("stats unavailable for the landing page")
            stats = {}
        symbol = stable_symbol(self.settings)
        info = self.settings.asset_by_symbol(symbol)
        note = f"On this server payments use {symbol}: {info.label}." if info and symbol == "HUSD" else ""
        return self.render(
            request,
            "landing.html",
            {
                "stats": stats,
                "symbol": symbol,
                "stable_note": note + (" It is not Circle USDC." if note else ""),
                "telegram": self.telegram_username(),
            },
        )

    async def hold_json(self, hold_id: int) -> dict[str, Any]:
        hold = await self.vault.get_hold(hold_id)
        if hold is None:
            raise HTTPException(404, f"there is no payment #{hold_id}")
        view = hold.to_view()
        return {"hold": view, "now_ms": await self.now_ms(), "signature": _poll_signature(view)}

    async def hold_page(self, request: Request, hold_id: int) -> HTMLResponse:
        why = await self.engine.why(hold_id)
        if why is None:
            raise HTTPException(404, f"There is no payment #{hold_id}.")
        now = await self.now_ms()
        view = views.build_hold_view(
            self.settings,
            why,
            config=await self.vault_config(),
            now_ms=now,
            txs=self.store.get_txs(hold_id),
            evidence_docs=[(r.evidence_json, r.evidence_hash) for r in self.store.list_evidence(hold_id)],
            context=self.store.get_context(hold_id),
            simulated=self.simulated,
        )
        return self.render(
            request,
            "hold.html",
            {
                "view": view,
                "now_ms": now,
                "steps": len(why.steps),
                "evidence_url": f"{self._base()}/v/{hold_id}.json",
                "poll_signature": _poll_signature(why.hold.to_view()),
            },
        )

    async def evidence(self, hold_id: int, sha256: str = "") -> Response:
        """The evidence JSON exactly as hashed on-chain (``?sha256=`` picks an earlier extension's)."""
        records = self.store.list_evidence(hold_id)
        if sha256:
            records = [r for r in records if r.evidence_hash == sha256.lower()]
        if not records:
            raise HTTPException(404, f"no evidence recorded for payment #{hold_id}")
        record = records[-1]
        digest = views.sha256_hex(record.evidence_json)
        headers = {"X-Evidence-Sha256": digest, "ETag": f'"{digest}"', "Cache-Control": "public, max-age=60"}
        hold = await self.vault.get_hold(hold_id)
        if hold is not None and hold.evidence_hash:
            headers["X-Onchain-Evidence-Hash"] = hold.evidence_hash
        return Response(record.evidence_json.encode("utf-8"), media_type="application/json", headers=headers)

    async def why_page(self, request: Request, hold_id: int) -> HTMLResponse:
        why = await self.engine.why(hold_id)
        if why is None:
            raise HTTPException(404, f"There is no payment #{hold_id}.")
        prompts = {s.step: s.prompt_sha256[:16] for s in why.steps if s.prompt_sha256}
        return self.render(
            request,
            "why.html",
            {
                "why": why,
                "reasons": views.reason_rows(f for f in why.findings if f.reason),
                "steps": views.step_rows(why.steps),
                "totals": views.step_totals(why.steps),
                "mcp_calls": [views.mcp_call_text(c) for c in self.store.get_mcp_calls(hold_id)],
                "prompts": prompts,
                "status": views.hold_headline(why.hold),
            },
        )

    async def proof(self) -> ProofView:
        """Verification of the deployment: computed for the simulated vault, cached for a live one."""
        if isinstance(self.vault, FakeVault):
            now = await self.now_ms()
            report = verify_fake(self.vault, self.settings)
            return proof_view(report, prove_fake(self.vault, self.settings), self.settings, checked_ms=now)
        async with self._proof_lock:
            if self._proof is not None and self._proof[0] > time.monotonic():
                return self._proof[1]
            now = int(time.time() * 1000)
            try:
                report = await asyncio.wait_for(verify_deploy(self.settings), PROOF_TIMEOUT_S)
                view, ttl = proof_view(report, None, self.settings, checked_ms=now), PROOF_TTL_S
            except Exception as exc:  # any RPC failure: show it on the page instead of a 500
                log.warning("deployment check failed", exc_info=True)
                error = f"Could not reach the NEAR RPC node ({type(exc).__name__}). Try again in a moment."
                view, ttl = proof_view(None, None, self.settings, checked_ms=now, error=error), PROOF_ERROR_TTL_S
            self._proof = (time.monotonic() + ttl, view)
            return view

    async def prove_page(self, request: Request) -> HTMLResponse:
        proof = await self.proof()
        commands = [] if proof.simulated else rpc_commands(self.settings)
        return self.render(request, "prove.html", {"proof": proof, "accounts": self._accounts(), "commands": commands})

    async def prove_json(self) -> dict[str, Any]:
        return (await self.proof()).to_json()

    def _accounts(self) -> list[tuple[str, str]]:
        accounts = [("Vault", self.settings.vault_account)]
        accounts += [("Oracle", oracle) for oracle in self.settings.oracle_accounts]
        accounts.append(("Keeper", self.settings.keeper_account))
        return [(label, account) for label, account in accounts if account]

    async def stats_page(self, request: Request) -> HTMLResponse:
        stats = await self.engine.stats()
        return self.render(request, "stats.html", {"tiles": views.stat_tiles(stats), "stats": stats})

    async def stats_json(self) -> dict[str, Any]:
        return await self.engine.stats()

    async def export(self, token: str) -> Response:
        account = read_export_token(self.secret, token, int(time.time() * 1000))
        user = self.store.user_by_account(account) if account else None
        if user is None:
            raise HTTPException(404, "This export link is invalid or has expired. Ask for a new one with /export.")
        content = await self.engine.export_csv(user["user_key"])
        return Response(
            content,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="holdup-{account}.csv"',
                "Cache-Control": "no-store",
                "X-Robots-Tag": "noindex",
            },
        )

    async def robots(self) -> PlainTextResponse:
        return PlainTextResponse(ROBOTS)


def rpc_query(rpc_url: str, request_type: str, account_id: str) -> str:
    """A curl command for one read-only RPC query."""
    body = {
        "jsonrpc": "2.0",
        "id": "holdup",
        "method": "query",
        "params": {"request_type": request_type, "finality": "final", "account_id": account_id},
    }
    data = json.dumps(body, separators=(",", ":"))
    return f"curl -s {shlex.quote(rpc_url)} -H 'Content-Type: application/json' -d {shlex.quote(data)}"


def rpc_commands(settings: Settings) -> list[tuple[str, str]]:
    """Commands anyone can run against the public RPC to repeat the main checks."""
    if not settings.rpc_url or not settings.vault_account:
        return []
    commands = [
        (
            "The vault's access keys (expect an empty list):",
            rpc_query(settings.rpc_url, "view_access_key_list", settings.vault_account),
        ),
        (
            "The vault's code hash (compare with base58 of sha256 of res/holdvault.wasm):",
            rpc_query(settings.rpc_url, "view_account", settings.vault_account),
        ),
    ]
    for oracle in settings.oracle_accounts:
        commands.append(
            (
                f"The oracle {oracle}'s keys (expect one function-call key for extend_hold):",
                rpc_query(settings.rpc_url, "view_access_key_list", oracle),
            )
        )
    return commands


def _poll_signature(view: dict[str, Any]) -> str:
    """What the hold page watches for: it reloads when any of these change."""
    return "|".join(str(view.get(k) or "") for k in ("status", "release_at_ms", "extensions", "evidence_hash"))


def json_error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse({"error": detail}, status_code=status_code)
