"""The composition root: settings in, a wired HoldUp out.

``build_runtime(settings)`` builds, in this order:

- the vault: the in-memory ``FakeVault`` on the offline network (with the demo
  payees already on its simulated chain), ``NearVault`` on sandbox and testnet;
- payee intel and the deterministic rules (``RulesRisk``), the fast check;
- the Otari pipeline (``OtariRisk``), the full check: recorded answers when
  ``otari_mode`` is ``replay``, the hosted gateway when it is ``live``, and no
  AI step at all when it is ``off``;
- the store, the notifiers and the engine;
- with ``web=True``: the Telegram bot (when a token is set) and the web app,
  with the near-intel MCP server at ``/mcp`` and the guardrail at ``/validate``.

Every command of the CLI (``serve``, ``demo``, ``attack``, ``chat``, ``prove``,
``verify-deploy``, ``keeper``) starts here, so they all run the same wiring.
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .bot.notify import RoutingNotifier
from .chain.fake import FakeVault, wall_clock_ms
from .chain.verify import expected_code_hash
from .config import NETWORKS, OFFLINE_SECRET, OTARI_MODES, Settings
from .engine import Engine
from .intel import PayeeIntel, load_payee_fixtures
from .interfaces import Notifier
from .risk.rules import RulesRisk
from .store import Store

log = logging.getLogger(__name__)

HOUR_MS = 3_600_000
CONSOLE_CHANNEL = "web"
TELEGRAM_CHANNEL = "tg"


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


class LogNotifier:
    """Writes notifications to the log, for users of channels nobody is listening to."""

    async def notify(self, user_key: str, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
        log.info("to %s: %s", user_key, text.replace("\n", " | "))


def offline_vault(
    settings: Settings,
    clock: Callable[[], int],
    profile: str = "demo",
    payees: dict[str, dict[str, Any]] | None = None,
) -> FakeVault:
    """The in-memory vault, with the payees of ``fixtures/messages/payees.json`` on its chain."""
    vault = FakeVault.from_settings(settings, profile=profile, clock=clock, code_hash=expected_code_hash())
    table = payees if payees is not None else load_payee_fixtures()["payees"]
    for account, facts in table.items():
        vault.create_account(account, age_ms=int(float(facts["age_hours"]) * HOUR_MS))
    return vault


@dataclass
class Runtime:
    """Everything ``build_runtime`` wired together."""

    settings: Settings
    vault: Any  # FakeVault offline, NearVault on a network
    intel: PayeeIntel
    fast_risk: RulesRisk
    full_risk: Any  # holdup.risk.pipeline.OtariRisk, or None when the AI step is off
    otari: Any  # the OtariClient behind full_risk, or None
    resolver: ScenarioResolver | None  # picks the recorded scenario in replay mode
    store: Store
    notifier: RoutingNotifier
    engine: Engine
    clock: Callable[[], int]
    console: Any = None  # holdup.web.console.ConsoleNotifier, with the web app
    bot: Any = None  # telegram.ext.Application, when TELEGRAM_BOT_TOKEN is set
    app: Any = None  # fastapi.FastAPI, with web=True

    @property
    def fake(self) -> FakeVault | None:
        return self.vault if isinstance(self.vault, FakeVault) else None

    @property
    def checks(self) -> str:
        """The risk checks in words, for the demo and the console."""
        mode = self.settings.otari_mode
        if self.full_risk is None:
            return "rules only; the AI step is off"
        return f"rules, then the Otari pipeline ({'recorded responses' if mode == 'replay' else 'live'})"

    def model_requests(self, hold_id: int) -> list[str] | None:
        """The steps of the model requests sent so far for one hold, in order.

        None when the client keeps no log of its requests (only the replay transport does).
        """
        transport = getattr(self.otari, "transport", None)
        requests = getattr(transport, "requests", None)
        if requests is None:
            return None
        return [r.step for r in requests if str((r.body.get("metadata") or {}).get("hold")) == str(hold_id)]

    async def aclose(self) -> None:
        """Release network clients and the database."""
        for closable in (self.vault, self.intel, self.otari):
            close = getattr(closable, "aclose", None)
            if close is not None:
                await close()
        self.store.close()


def build_runtime(
    settings: Settings,
    *,
    notifier: Notifier | None = None,
    clock: Callable[[], int] | None = None,
    web: bool = True,
    profile: str = "demo",
) -> Runtime:
    """Wire HoldUp for ``settings``.

    ``notifier`` receives messages for users of channels without a notifier of
    their own (the demo's and the console's users); by default they are logged.
    ``clock`` drives the offline world (a ``SimClock`` for the scripted demo);
    on a real network time comes from the chain. ``profile`` picks the offline
    vault's waits (``demo``: seconds, ``hosted``: minutes).
    """
    if settings.network not in NETWORKS:
        raise ValueError(f"unknown network {settings.network!r}")
    if settings.otari_mode not in OTARI_MODES:
        raise ValueError(f"unknown OTARI_MODE {settings.otari_mode!r}")
    offline = settings.network == "offline"
    if not offline and settings.secret in ("", OFFLINE_SECRET):
        raise ValueError("a real network needs its own HOLDUP_SECRET (every user's account is derived from it)")

    if offline:
        clock = clock or wall_clock_ms
        vault: Any = offline_vault(settings, clock, profile)
        # The vault forgets everything on restart, so the store must too.
        store = Store(":memory:", secret=settings.secret or None, clock=clock)
        intel = PayeeIntel(settings, vault, fixtures=load_payee_fixtures())
    else:
        from .chain.vault import NearVault

        vault = NearVault(settings)
        store = Store(settings.database_path, secret=settings.secret or None, clock=clock)
        intel = PayeeIntel(settings, vault)
    fast = RulesRisk(intel, vault)
    full, otari, resolver = _full_risk(settings, intel, fast, store)

    routing = RoutingNotifier({}, default=notifier or LogNotifier())
    engine = Engine(settings, vault, fast, full, store, routing, clock=clock)
    runtime = Runtime(
        settings=settings,
        vault=vault,
        intel=intel,
        fast_risk=fast,
        full_risk=full,
        otari=otari,
        resolver=resolver,
        store=store,
        notifier=routing,
        engine=engine,
        clock=engine.clock,
    )
    if web:
        _add_web(runtime)
    return runtime


def _full_risk(
    settings: Settings, intel: PayeeIntel, rules: RulesRisk, store: Store
) -> tuple[Any, Any, ScenarioResolver | None]:
    """``(OtariRisk, its client, the replay resolver)`` for the configured Otari mode."""
    if settings.otari_mode == "off":
        return None, None, None
    from .guard import guard_token
    from .mcp_server import mcp_token
    from .risk.pipeline import OtariRisk

    extra: dict[str, Any] = {}
    resolver: ScenarioResolver | None = None
    if settings.otari_mode == "replay":
        from .risk.replay import replay_client

        resolver = ScenarioResolver()
        client = replay_client(settings, resolver)
    else:
        from .risk.budgets import KeyPool, user_key_resolver
        from .risk.otari import OtariClient

        if not settings.otari_api_key:
            raise ValueError("OTARI_MODE=live needs OTARI_API_KEY (or use OTARI_MODE=replay or off)")
        client = OtariClient(settings)
        # Live only: a replayed gateway never calls our MCP server, so its log stays empty.
        extra = {"user_keys": user_key_resolver(store, KeyPool.from_env()), "mcp_log": store.get_mcp_calls}
    base = settings.public_base_url
    full = OtariRisk(
        settings,
        client,
        intel,
        rules,
        f"{base}/mcp" if base else None,
        f"{base}/validate" if base else None,
        guard_token_fn=functools.partial(guard_token, settings.secret),
        mcp_token_fn=functools.partial(mcp_token, settings.secret),
        **extra,
    )
    return full, client, resolver


def _add_web(runtime: Runtime) -> None:
    """The web console's notifier, the Telegram bot and the web app with MCP and the guardrail."""
    from .guard import build_router
    from .mcp_server import build_mcp
    from .web.app import create_app
    from .web.console import ConsoleNotifier

    settings, engine = runtime.settings, runtime.engine
    runtime.console = ConsoleNotifier()
    runtime.notifier.routes[CONSOLE_CHANNEL] = runtime.console
    if settings.telegram_bot_token:
        from .bot.app import build_bot
        from .bot.notify import TelegramNotifier

        runtime.bot = build_bot(settings, engine)
        runtime.notifier.routes[TELEGRAM_CHANNEL] = TelegramNotifier(runtime.bot.bot)
    classifier = None
    if settings.otari_mode == "live" and runtime.otari is not None:
        from .risk.guard_models import make_classifier

        classifier = make_classifier(runtime.otari)
    runtime.app = create_app(
        settings,
        engine,
        runtime.vault,
        runtime.store,
        bot_app=runtime.bot,
        mcp=build_mcp(settings, runtime.vault, runtime.intel, runtime.store, runtime.store.record_mcp_call),
        guard_router=build_router(settings, classifier),
    )
