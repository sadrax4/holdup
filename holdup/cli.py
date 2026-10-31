"""``holdup`` command line: demo, attack, prove, verify-deploy, deploy, chat, keeper, serve.

Commands run offline (in memory, nothing sent anywhere) unless a network is
chosen with ``--network`` or ``HOLDUP_NETWORK``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

from .config import Settings, load_settings
from .messages import error_text, fmt_amount, fmt_utc
from .models import PaymentContext, parse_amount, reasons_from_bits

log = logging.getLogger("holdup")

NETWORKS = ("sandbox", "testnet")


def _settings_for(args: argparse.Namespace) -> Settings:
    settings = load_settings()
    network = getattr(args, "network", None)
    if getattr(args, "offline", False):
        return replace(settings, network="offline")
    if network and network != settings.network:
        from .config import RPC_URLS

        settings = replace(settings, network=network, rpc_url=RPC_URLS.get(network, settings.rpc_url))
    return settings


def _offline(settings: Settings) -> bool:
    return settings.network == "offline"


# ----------------------------------------------------------------------
# demo and attack
# ----------------------------------------------------------------------


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import Transcript, TranscriptNotifier, run_demo, run_offline_demo
    from .workers.runtime import live_runtime

    settings = _settings_for(args)
    if _offline(settings):
        asyncio.run(run_offline_demo(builtin_risk=args.builtin_risk))
        return 0

    async def live() -> None:
        transcript = Transcript()
        notifier = TranscriptNotifier(transcript)
        runtime = live_runtime(settings, notifier)
        try:
            await run_demo(runtime, transcript, notifier)
        finally:
            await runtime.vault.aclose()

    asyncio.run(live())
    return 0


def cmd_attack(args: argparse.Namespace) -> int:
    from .demo import Transcript, TranscriptNotifier, run_attack, run_offline_attack
    from .workers.runtime import live_runtime

    settings = _settings_for(args)
    if _offline(settings):
        asyncio.run(run_offline_attack(builtin_risk=args.builtin_risk))
        return 0

    async def live() -> None:
        transcript = Transcript()
        notifier = TranscriptNotifier(transcript)
        runtime = live_runtime(settings, notifier)
        try:
            await run_attack(runtime, transcript, notifier)
        finally:
            await runtime.vault.aclose()

    asyncio.run(live())
    return 0


# ----------------------------------------------------------------------
# proofs
# ----------------------------------------------------------------------


def _print_checks(rows: Sequence[tuple[bool, str, str]], *, simulated: bool) -> None:
    if simulated:
        print("SAMPLE: offline, in-memory vault; nothing below comes from a real network.")
    for ok, name, text in rows:
        print(f"[{'ok' if ok else 'FAIL'}] {name}: {text}")


def cmd_prove(args: argparse.Namespace) -> int:
    from .chain.keys import derive_user
    from .chain.prove import prove, prove_fake
    from .demo import Transcript, TranscriptNotifier
    from .workers.runtime import offline_runtime

    settings = _settings_for(args)
    if _offline(settings):
        runtime = offline_runtime(TranscriptNotifier(Transcript(None)))
        user = runtime.engine.account_for("demo:priya")
        asyncio.run(runtime.engine.start("demo:priya", "Priya"))
        report = prove_fake(runtime.fake, runtime.settings, user=user)
    else:
        user = derive_user(settings.secret, "demo:priya", settings.root_account) if settings.secret else None
        report = asyncio.run(prove(settings, user=user))
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _print_checks(
            [
                (r.passed, f"{r.actor} key", f"{r.description}: {r.observed} (expected {', '.join(r.expected)})")
                for r in report.results
            ],
            simulated=report.simulated,
        )
    return 0 if report.ok else 1


def cmd_verify(args: argparse.Namespace) -> int:
    from .chain.verify import verify_deploy, verify_fake
    from .demo import Transcript, TranscriptNotifier
    from .workers.runtime import offline_runtime

    settings = _settings_for(args)
    if _offline(settings):
        runtime = offline_runtime(TranscriptNotifier(Transcript(None)))
        report = verify_fake(runtime.fake, runtime.settings)
    else:
        report = asyncio.run(verify_deploy(settings))
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _print_checks(
            [(c.ok, c.name, f"{c.observed} (expected {c.expected})") for c in report.checks],
            simulated=report.simulated,
        )
    return 0 if report.ok else 1


def cmd_deploy(args: argparse.Namespace) -> int:
    from .chain.deploy import deploy

    base = load_settings()
    data = asyncio.run(
        deploy(base, network=args.network, profile=args.profile, root=args.root, reserve_near=args.reserve)
    )
    print(f"Deployed to {data['network']}: vault {data['vault_account']}, oracle {data['oracle_accounts'][0]}, "
          f"keeper {data['keeper_account']}, token {data['husd_token']}.")
    for label, tx_hash in data["deployment"]["transactions"]:
        print(f"  {label}: {tx_hash}")
    print("Credentials written to .holdup/credentials.json. Run `holdup verify-deploy` next.")
    return 0


# ----------------------------------------------------------------------
# interactive console
# ----------------------------------------------------------------------

_SEND = re.compile(r"^/?send\s+([\d.,]+)\s+([a-z]+)\s+to\s+(\S+)$", re.IGNORECASE)
_PAY = re.compile(r"^/?pay\s+(\S+)\s+([\d.,]+)\s+([a-z]+)$", re.IGNORECASE)

CHAT_HELP = """Commands:
  forward <text>                 paste a message you received (context for your next payment)
  send <amount> <token> to <account>   or   pay <account> <amount> <token>
  balance | holds | cancel <id> | why <id> | trust <account> | untrust <account>
  guardian <name>                make another local user your guardian
  as <name>                      switch to (or create) another local user
  wait <seconds>                 let time pass (offline) and run the keeper
  scenario <name>                offline: recorded AI responses to replay (hero, invoice, benign, ...)
  help | quit"""


class Console:
    """A line-oriented chat with the engine, for trying HoldUp without Telegram."""

    def __init__(self, runtime: Any, notifier: Any, out: Callable[[str], None] = print) -> None:
        self.runtime = runtime
        self.notifier = notifier
        self.out = out
        self.user = "console:you"

    @property
    def engine(self) -> Any:
        return self.runtime.engine

    async def login(self, name: str) -> None:
        key = f"console:{name.lower()}"
        self.notifier.names[key] = name
        account, created = await self.engine.start(key, name)
        self.user = key
        self.out(f"You are {name} ({account.account_id}){', new account funded' if created else ''}.")

    async def handle(self, line: str) -> bool:
        """Run one command; False means quit."""
        line = line.strip()
        if not line:
            return True
        word, _, rest = line.partition(" ")
        word = word.lower().lstrip("/")
        settings = self.runtime.settings
        try:
            if word in ("quit", "exit"):
                return False
            if word == "help":
                self.out(CHAT_HELP)
            elif word == "as":
                await self.login(rest or "you")
            elif word == "forward":
                self.engine.remember_context(self.user, PaymentContext(forwarded_text=rest))
                self.out("Noted. It will be checked with your next payment (for 15 minutes).")
            elif match := _SEND.match(line) or _PAY.match(line):
                if word == "send":
                    amount_text, symbol, payee = match.groups()
                else:
                    payee, amount_text, symbol = match.groups()
                asset = settings.asset_by_symbol(symbol)
                if asset is None and symbol.upper() == "USDC":
                    asset = settings.asset_by_symbol("HUSD")
                    self.out("USDC is not configured here; using HUSD, the HoldUp demo token (not Circle USDC).")
                if asset is None:
                    self.out(error_text("E_UNKNOWN_ASSET"))
                    return True
                outcome = await self.engine.pay(self.user, payee, asset.asset_id, parse_amount(amount_text, asset.decimals))
                if outcome.refused:
                    self.out(outcome.message)
                await self.engine.wait_idle()
            elif word == "balance":
                balance = await self.engine.balance(self.user)
                self.out(", ".join(fmt_amount(settings, a, v) for a, v in balance.items()))
            elif word == "holds":
                now = await self.runtime.vault.now_ms()
                for hold in await self.engine.holds(self.user):
                    reasons = ",".join(r.name for r in reasons_from_bits(hold.reason_bits))
                    self.out(f"#{hold.id} {fmt_amount(settings, hold.asset, hold.amount)} to {hold.payee}: "
                             f"{hold.status.value}, release {fmt_utc(hold.release_at_ms, now)} {reasons}")
            elif word == "cancel":
                await self.engine.cancel(self.user, int(rest))
            elif word == "why":
                await self._why(int(rest))
            elif word == "trust":
                await self.engine.trust(self.user, rest)
            elif word == "untrust":
                await self.engine.untrust(self.user, rest)
            elif word == "guardian":
                me = self.user
                await self.login(rest)
                guardian = self.user
                self.user = me
                await self.engine.accept_guardian(guardian, await self.engine.guardian_invite(me))
            elif word == "wait":
                await self._wait(float(rest or "0"))
            elif word == "scenario":
                if self.runtime.risk.resolver is not None:
                    self.runtime.risk.resolver.current = rest or None
                    self.out(f"Replaying the {rest or 'automatic'} scenario.")
            else:
                self.out("Unknown command. Type help.")
        except ValueError as exc:
            self.out(f"Could not read that: {exc}")
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code is None:
                raise
            self.out(error_text(code, getattr(exc, "message", "")))
        return True

    async def _why(self, hold_id: int) -> None:
        view = await self.engine.why(hold_id)
        if view is None:
            self.out(error_text("E_HOLD_NOT_FOUND"))
            return
        for finding in view.findings:
            self.out(f"- {finding.reason.name} [{finding.source}{' ' + finding.model if finding.model else ''}] {finding.detail}")
        for step in view.steps:
            fields = (("model", step.model), ("provider", step.provider), ("fallback", step.fallback),
                      ("attempts", step.attempts), ("cost USD", step.cost_usd), ("latency ms", step.latency_ms))
            details = ", ".join(f"{name} {value}" for name, value in fields if value is not None)
            self.out(f"  step {step.step}: {details}".rstrip(": "))
        if view.evidence_hash:
            self.out(f"  evidence sha256 {view.evidence_hash}")
        for label, url in view.tx_links:
            self.out(f"  {label} {url}".rstrip())

    async def _wait(self, seconds: float) -> None:
        advance = getattr(self.runtime.clock, "advance", None)
        if advance is not None:
            advance(int(seconds * 1000))
        else:
            await asyncio.sleep(seconds)
        await self.engine.make_keeper().run_once()
        await self.engine.wait_idle()


def cmd_chat(args: argparse.Namespace) -> int:
    from .demo import Transcript, TranscriptNotifier
    from .workers.runtime import live_runtime, offline_runtime

    settings = _settings_for(args)
    notifier = TranscriptNotifier(Transcript())
    runtime = offline_runtime(notifier) if _offline(settings) else live_runtime(settings, notifier)

    async def loop() -> None:
        console = Console(runtime, notifier)
        where = "offline, in memory" if _offline(settings) else settings.network
        print(f"HoldUp console ({where}; checks: {runtime.risk.label}). Type help.")
        await console.login(args.name)
        while True:
            try:
                line = await asyncio.to_thread(input, "> ")
            except EOFError:
                break
            if not await console.handle(line):
                break

    asyncio.run(loop())
    return 0


# ----------------------------------------------------------------------
# long-running processes
# ----------------------------------------------------------------------


def cmd_keeper(args: argparse.Namespace) -> int:
    from .workers.runtime import LogNotifier, live_runtime

    settings = _settings_for(args)
    if _offline(settings):
        print("The keeper releases holds on a real network; offline holds live in memory. "
              "Use `holdup chat` and its wait command to see releases offline.")
        return 2
    runtime = live_runtime(settings, LogNotifier())
    print(f"Keeper running on {settings.network} every {args.interval} s (Ctrl-C to stop).")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(runtime.engine.make_keeper(args.interval).run())
    return 0


def _channel_notifier(bot_app: Any) -> Any:
    """Telegram and web-console delivery, when those packages are present."""
    from .workers.runtime import LogNotifier

    try:
        from .bot.notify import RoutingNotifier, TelegramNotifier
        from .web.console import ConsoleNotifier

        routes: dict[str, Any] = {"web": ConsoleNotifier()}
        if bot_app is not None:
            routes["tg"] = TelegramNotifier(bot_app.bot)
        return RoutingNotifier(routes)
    except Exception as exc:
        log.warning("chat channels unavailable (%s); notifications go to the log", exc)
        return LogNotifier()


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .workers.runtime import LogNotifier, live_runtime, offline_runtime

    try:
        from .web.app import create_app
    except ImportError as exc:
        print(f"The web app is not available ({exc}).", file=sys.stderr)
        return 2

    settings = _settings_for(args)
    base = settings.public_base_url
    if _offline(settings):
        runtime = offline_runtime(LogNotifier(), clock=_wall_clock(), public_base_url=base)
    else:
        runtime = live_runtime(settings, LogNotifier(), mcp_url=f"{base}/mcp", guard_url=f"{base}/validate")
    engine = runtime.engine

    bot_app = None
    if runtime.settings.telegram_bot_token:
        from .bot.app import build_bot

        bot_app = build_bot(runtime.settings, engine)
    engine.notifier = _channel_notifier(bot_app)

    mcp = guard_router = None
    try:
        from .mcp_server import build_mcp

        mcp = build_mcp(runtime.settings, runtime.vault, runtime.risk.intel, runtime.store, runtime.store.record_mcp_call)
    except Exception as exc:
        log.warning("MCP server not mounted (%s)", exc)
    try:
        from .guard import build_router

        classifier = None
        if runtime.settings.otari_mode == "live" and runtime.risk.client is not None:
            from .risk.guard_models import make_classifier

            classifier = make_classifier(runtime.risk.client)
        guard_router = build_router(runtime.settings, classifier)
    except Exception as exc:
        log.warning("guardrail endpoint not mounted (%s)", exc)

    app = create_app(runtime.settings, engine, runtime.vault, runtime.store, bot_app=bot_app, mcp=mcp, guard_router=guard_router)

    async def run() -> None:
        server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="info"))
        stop = asyncio.Event()
        keeper = asyncio.create_task(engine.make_keeper(args.interval).run(stop))
        try:
            await server.serve()
        finally:
            stop.set()
            await keeper

    asyncio.run(run())
    return 0


def _wall_clock() -> Callable[[], int]:
    from .chain.fake import wall_clock_ms

    return wall_clock_ms


# ----------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="holdup", description="Scam brakes for stablecoin payments on NEAR.")
    parser.add_argument("-v", "--verbose", action="store_true", help="log more")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_network(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        group = p.add_mutually_exclusive_group()
        group.add_argument("--offline", action="store_true", help="in-memory vault, nothing sent anywhere")
        group.add_argument("--network", choices=NETWORKS, help="run against a NEAR network")
        return p

    demo = with_network(sub.add_parser("demo", help="run the scripted story"))
    demo.add_argument("--builtin-risk", action="store_true", help="use the built-in rules only")
    demo.set_defaults(func=cmd_demo)

    attack = with_network(sub.add_parser("attack", help="three attacks and why they fail"))
    attack.add_argument("--builtin-risk", action="store_true", help="use the built-in rules only")
    attack.set_defaults(func=cmd_attack)

    prove = with_network(sub.add_parser("prove", help="show that the oracle and user keys cannot move money"))
    prove.add_argument("--json", action="store_true")
    prove.set_defaults(func=cmd_prove)

    verify = with_network(sub.add_parser("verify-deploy", help="check code hash and keys of a deployment"))
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(func=cmd_verify)

    deploy = sub.add_parser("deploy", help="deploy the vault, token, oracle and keeper")
    deploy.add_argument("--network", choices=NETWORKS, required=True)
    deploy.add_argument("--profile", choices=("demo", "hosted"), default="demo")
    deploy.add_argument("--root", help="root account (default: from settings or the sandbox root)")
    deploy.add_argument("--reserve", default="5", help="NEAR pre-funded into the vault reserve")
    deploy.set_defaults(func=cmd_deploy)

    chat = with_network(sub.add_parser("chat", help="interactive console on the same engine"))
    chat.add_argument("--name", default="You", help="your display name")
    chat.set_defaults(func=cmd_chat)

    keeper = with_network(sub.add_parser("keeper", help="release due holds in a loop"))
    keeper.add_argument("--interval", type=float, default=5.0)
    keeper.set_defaults(func=cmd_keeper)

    serve = with_network(sub.add_parser("serve", help="web app, Telegram webhook and keeper"))
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--interval", type=float, default=5.0, help="keeper interval in seconds")
    serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return int(args.func(args) or 0)
    except BrokenPipeError:  # output piped into a command that stopped reading, e.g. head
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
