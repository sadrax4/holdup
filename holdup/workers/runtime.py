"""Process wiring shared by the CLI, the demo, the keeper and the server.

Builds a vault, a store, the risk checks and an engine for either the
in-memory world (offline: nothing touches a network) or a real NEAR network.
The risk package is imported when present; until then a small built-in set of
deterministic rules keeps every command runnable.
"""

from __future__ import annotations

import functools
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..chain.fake import FakeVault, SimClock
from ..chain.verify import expected_code_hash
from ..config import Settings
from ..engine import OFFLINE_SECRET, Engine
from ..interfaces import Notifier, RiskPort, VaultPort
from ..models import NEAR, Finding, Hold, PaymentContext, Reason, StepRecord, Verdict
from ..store import Store

log = logging.getLogger(__name__)

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
REPO_ROOT = Path(__file__).resolve().parents[2]
PAYEE_FIXTURES = REPO_ROOT / "fixtures" / "messages" / "payees.json"

OFFLINE_ROOT = "holdup.testnet"

# Accounts that exist in the in-memory world, with the age and inbound pattern
# the demo story needs (kept in line with fixtures/messages/payees.json).
OFFLINE_PAYEES: dict[str, dict[str, Any]] = {
    "safe-wallet-77.testnet": {"age_hours": 2, "distinct_inbound_senders_24h": 14},
    "northwind-paper.testnet": {"age_hours": 26, "distinct_inbound_senders_24h": 1},
    "northwindpaper.testnet": {"age_hours": 2 * 365 * 24, "distinct_inbound_senders_24h": 3},
    "ravi.testnet": {"age_hours": 3 * 365 * 24, "distinct_inbound_senders_24h": 1},
    "mum.testnet": {"age_hours": 5 * 365 * 24, "distinct_inbound_senders_24h": 1},
    "attacker.testnet": {"age_hours": 1, "distinct_inbound_senders_24h": 0},
}


def offline_settings(public_base_url: str = "") -> Settings:
    """Settings for the in-memory world: fixed accounts, HUSD demo token, replayed AI."""
    settings = Settings(
        network="offline",
        root_account=OFFLINE_ROOT,
        vault_account=f"vault.{OFFLINE_ROOT}",
        oracle_accounts=[f"oracle.{OFFLINE_ROOT}"],
        keeper_account=f"keeper.{OFFLINE_ROOT}",
        secret=OFFLINE_SECRET,
        husd_token=f"husd.{OFFLINE_ROOT}",
        otari_mode="replay",
        database_path=":memory:",
        public_base_url=public_base_url,
    )
    settings.demo_funds = {NEAR: 2 * 10**24, f"husd.{OFFLINE_ROOT}": 2_000 * 10**6}
    return settings


def offline_vault(settings: Settings, clock: Callable[[], int], profile: str = "demo") -> FakeVault:
    """The in-memory vault with the demo payees already on 'chain'."""
    vault = FakeVault.from_settings(settings, profile=profile, clock=clock, code_hash=expected_code_hash())
    for account, profile_data in OFFLINE_PAYEES.items():
        vault.create_account(account, age_ms=int(profile_data["age_hours"] * HOUR_MS))
    return vault


# ----------------------------------------------------------------------
# built-in rules (used only when holdup.risk.rules is not installed)
# ----------------------------------------------------------------------

_KEYWORDS: tuple[tuple[Reason, re.Pattern[str]], ...] = (
    (Reason.SECRET_REQUEST, re.compile(r"seed phrase|recovery phrase|private key|secret phrase|one-time code")),
    (Reason.CHECKER_INJECTION, re.compile(r"(note|message) to (any )?ai( assistant)?|ignore (all |any )?previous"
                                          r"|mark (it|this) (as )?safe|pre-?approved")),
    (Reason.URGENCY, re.compile(r"within \d+ (minutes|hours)|immediately|urgent|will be frozen|suspended")),
    (Reason.DETAILS_CHANGED, re.compile(r"(moved to |new )(a new )?(usdc |payment |bank )?wallet"
                                        r"|changed our (bank|wallet)|updated (bank|payment) details")),
    (Reason.IMPERSONATION, re.compile(r"\bsupport\b|security team|accounts? team")),
)


def _skeleton(account_id: str) -> str:
    name = account_id.rsplit(".", 1)[0] if account_id.count(".") >= 1 else account_id
    return re.sub(r"[-_.0-9]", "", name)


def _edit_distance(a: str, b: str) -> int:
    """Optimal string alignment distance (Damerau-Levenshtein with adjacent swaps)."""
    rows = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        rows[i][0] = i
    for j in range(len(b) + 1):
        rows[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            rows[i][j] = min(rows[i - 1][j] + 1, rows[i][j - 1] + 1, rows[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                rows[i][j] = min(rows[i][j], rows[i - 2][j - 2] + 1)
    return rows[-1][-1]


class BuiltinRules:
    """A small deterministic stand-in for ``holdup.risk.rules.RulesRisk``."""

    def __init__(self, vault: VaultPort, profiles: dict[str, dict[str, Any]] | None = None) -> None:
        self.vault = vault
        self.profiles = profiles if profiles is not None else OFFLINE_PAYEES

    async def assess(self, hold: Hold, ctx: PaymentContext) -> Verdict:
        findings: list[Finding] = []
        stats = await self.vault.get_payee_stats(hold.payee)
        now = await self.vault.now_ms()
        if stats.cancelled_after_flag >= 2 and (stats.last_flag_ms or 0) >= now - 30 * DAY_MS:
            findings.append(Finding(Reason.PAYEE_REPORTED,
                                    f"{stats.cancelled_after_flag} reports from earlier senders", "rules"))
        profile = self.profiles.get(hold.payee, {})
        if "age_hours" in profile and profile["age_hours"] < 72:
            findings.append(Finding(Reason.NEW_ACCOUNT, f"created {profile['age_hours']} h ago", "rules"))
        if profile.get("distinct_inbound_senders_24h", 0) >= 8:
            findings.append(Finding(Reason.MULE_PATTERN,
                                    f"{profile['distinct_inbound_senders_24h']} unrelated senders today", "rules"))
        for trusted in ctx.trusted_payees:
            if trusted != hold.payee and (
                _skeleton(trusted) == _skeleton(hold.payee) or _edit_distance(trusted, hold.payee) <= 2
            ):
                findings.append(Finding(Reason.LOOKALIKE_PAYEE, f"looks like {trusted}", "rules"))
                break
        text = (ctx.forwarded_text or "").lower()
        for reason, pattern in _KEYWORDS:
            if match := pattern.search(text):
                findings.append(Finding(reason, f'message says "{match.group(0)}"', "rules"))
        strong = {Reason.PAYEE_REPORTED, Reason.SECRET_REQUEST}
        hold_for = 24 * HOUR_MS if any(f.reason in strong for f in findings) else HOUR_MS if findings else 0
        return Verdict(
            hold_id=hold.id,
            brake=bool(findings),
            hold_for_ms=hold_for,
            findings=findings,
            steps=[StepRecord(step="precheck", provider="builtin rules")],
            summary="built-in rules",
        )


# ----------------------------------------------------------------------
# loading the risk package
# ----------------------------------------------------------------------


class ScenarioResolver:
    """Chooses which recorded Otari exchange to replay for a request.

    The demo sets ``current`` before each payment; otherwise well-known
    phrases in the request pick the scenario.
    """

    KEYWORDS = (("coinhaven", "hero"), ("northwind", "invoice"))

    def __init__(self, default: str = "benign") -> None:
        self.default = default
        self.current: str | None = None

    def __call__(self, *args: Any, **kwargs: Any) -> str:
        if self.current:
            return self.current
        text = json.dumps([args, kwargs], default=str).lower()
        for needle, scenario in self.KEYWORDS:
            if needle in text:
                return scenario
        return self.default


@dataclass
class RiskSetup:
    fast: RiskPort
    full: RiskPort | None
    label: str
    resolver: ScenarioResolver | None = None
    intel: Any = None  # holdup.intel.PayeeIntel when the risk package is installed
    client: Any = None  # holdup.risk.otari.OtariClient when the AI step is on


def _payee_fixtures() -> dict[str, Any] | None:
    try:
        return json.loads(PAYEE_FIXTURES.read_text())
    except (OSError, ValueError):
        return None


def load_risk(
    settings: Settings,
    vault: VaultPort,
    *,
    builtin: bool = False,
    http: Any = None,
    mcp_url: str | None = None,
    guard_url: str | None = None,
) -> RiskSetup:
    """Rules as the fast check and the Otari pipeline as the full check, when installed."""
    resolver = ScenarioResolver()
    if builtin:
        return RiskSetup(BuiltinRules(vault), None, "built-in rules; no AI step", resolver)
    try:
        from ..intel import PayeeIntel
        from ..risk.rules import RulesRisk

        fixtures = _payee_fixtures() if settings.network == "offline" else None
        intel = PayeeIntel(settings, vault, http=http, fixtures=fixtures)
        fast: RiskPort = RulesRisk(intel, vault)
    except ImportError as exc:
        log.info("risk package not installed (%s); using the built-in rules", exc)
        return RiskSetup(BuiltinRules(vault), None, "built-in rules (risk package not installed); no AI step", resolver)
    except Exception as exc:
        log.warning("holdup.risk.rules failed to start (%r); using the built-in rules", exc)
        return RiskSetup(BuiltinRules(vault), None, "built-in rules (risk package failed to start); no AI step", resolver)

    if settings.otari_mode == "off":
        return RiskSetup(fast, None, "rules; AI step off", resolver, intel)
    try:
        from ..risk.pipeline import OtariRisk

        if settings.otari_mode == "replay":
            from ..risk.replay import replay_client

            client = replay_client(settings, resolver)
        else:
            from ..risk.otari import OtariClient

            client = OtariClient(settings)
        guard_token_fn = mcp_token_fn = None
        if guard_url:
            from ..guard import guard_token

            guard_token_fn = functools.partial(guard_token, settings.secret)
        if mcp_url:
            from ..mcp_server import mcp_token

            mcp_token_fn = functools.partial(mcp_token, settings.secret)
        full = OtariRisk(
            settings, client, intel, fast, mcp_url, guard_url,
            guard_token_fn=guard_token_fn, mcp_token_fn=mcp_token_fn,
        )
    except ImportError as exc:
        log.info("Otari pipeline not installed (%s); rules only", exc)
        return RiskSetup(fast, None, "rules; AI step not installed", resolver, intel)
    except Exception as exc:
        log.warning("Otari pipeline failed to start (%r); rules only", exc)
        return RiskSetup(fast, None, "rules; AI step failed to start", resolver, intel)
    mode = "recorded responses" if settings.otari_mode == "replay" else "live"
    return RiskSetup(fast, full, f"rules, then the Otari pipeline ({mode})", resolver, intel, client)


# ----------------------------------------------------------------------
# runtimes
# ----------------------------------------------------------------------


class LogNotifier:
    """Writes notifications to the log; for workers that have no chat channel."""

    async def notify(self, user_key: str, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
        log.info("to %s: %s", user_key, text.replace("\n", " | "))


@dataclass
class Runtime:
    settings: Settings
    vault: Any
    store: Store
    engine: Engine
    risk: RiskSetup
    clock: Callable[[], int]

    @property
    def fake(self) -> FakeVault | None:
        return self.vault if isinstance(self.vault, FakeVault) else None


def offline_runtime(
    notifier: Notifier,
    *,
    clock: Callable[[], int] | None = None,
    builtin_risk: bool = False,
    profile: str = "demo",
    public_base_url: str = "",
) -> Runtime:
    """Everything in memory: the vault, the store and replayed AI responses."""
    clock = clock or SimClock()
    settings = offline_settings(public_base_url)
    vault = offline_vault(settings, clock, profile)
    store = Store(":memory:", secret=settings.secret, clock=clock)
    risk = load_risk(settings, vault, builtin=builtin_risk)
    engine = Engine(settings, vault, risk.fast, risk.full, store, notifier, clock=clock)
    return Runtime(settings, vault, store, engine, risk, clock)


def live_runtime(
    settings: Settings,
    notifier: Notifier,
    *,
    mcp_url: str | None = None,
    guard_url: str | None = None,
) -> Runtime:
    """A runtime on the configured NEAR network (sandbox or testnet)."""
    from ..chain.vault import NearVault

    vault = NearVault(settings)
    store = Store(settings.database_path, secret=settings.secret or None)
    risk = load_risk(settings, vault, mcp_url=mcp_url, guard_url=guard_url)
    engine = Engine(settings, vault, risk.fast, risk.full, store, notifier)
    return Runtime(settings, vault, store, engine, risk, engine.clock)
