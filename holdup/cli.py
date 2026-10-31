"""``holdup`` command line: demo, attack, prove, verify-deploy, deploy, chat, keeper, serve, probe.

Commands run offline (in memory, nothing sent anywhere) unless a network is
chosen with ``--network`` or ``HOLDUP_NETWORK``. Every command gets its vault,
checks, store and engine from :func:`holdup.runtime.build_runtime`.
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
from typing import Any

from .config import Settings, load_settings
from .messages import error_text, fmt_amount, fmt_utc
from .models import PaymentContext, parse_amount, reasons_from_bits

log = logging.getLogger("holdup")

NETWORKS = ("sandbox", "testnet")

# What a command on a real network needs, and why; `holdup deploy` writes all of it to
# .holdup/credentials.json, and the matching environment variables override that file.
LIVE_NEEDS = (
    ("secret", "HOLDUP_SECRET", "derives every user's NEAR account and key; generate it once and keep it"),
    ("treasury_private_key", "HOLDUP_TREASURY_KEY", "full-access key of the root account, which onboards users"),
    ("oracle_private_key", "HOLDUP_ORACLE_KEY", "the oracle's only key, limited to extend_hold on the vault"),
    ("keeper_private_key", "HOLDUP_KEEPER_KEY", "the keeper's only key, limited to release on the vault"),
    ("husd_token", "HOLDUP_HUSD_TOKEN", "the HUSD demo token that new users are funded with"),
)


def _settings_for(args: argparse.Namespace) -> Settings:
    if getattr(args, "offline", False):
        return load_settings(network="offline")
    return load_settings(network=getattr(args, "network", None))


def _offline(settings: Settings) -> bool:
    return settings.network == "offline"


def missing_credentials(settings: Settings) -> list[str]:
    """What a live command still needs, as ``NAME (why)`` lines; empty when nothing is missing."""
    return [f"{name} ({why})" for attr, name, why in LIVE_NEEDS if not getattr(settings, attr)]


def _explain_missing(settings: Settings, command: str) -> int:
    """Print what is missing for ``command`` on a real network and return 2, or return 0 when ready."""
    missing = missing_credentials(settings)
    if not missing:
        return 0
    print(
        f"`holdup {command}` on {settings.network} signs real transactions and needs a deployment.\n"
        f"Run `holdup deploy --network {settings.network} --profile demo` first: it needs HOLDUP_ROOT_ACCOUNT "
        "and HOLDUP_TREASURY_KEY (the root account and its full-access key) and writes "
        ".holdup/credentials.json. Still missing:",
        file=sys.stderr,
    )
    for line in missing:
        print(f"  {line}", file=sys.stderr)
    return 2


# ----------------------------------------------------------------------
# demo and attack
# ----------------------------------------------------------------------


def _run_story(args: argparse.Namespace, offline_story: Callable[..., Any], story: Callable[..., Any]) -> int:
    from .demo import Transcript, TranscriptNotifier
    from .runtime import build_runtime

    settings = _settings_for(args)
    if _offline(settings):
        asyncio.run(offline_story(rules_only=args.rules_only))
        return 0
    if code := _explain_missing(settings, args.command):
        return code
    if args.rules_only:
        settings.otari_mode = "off"

    async def live() -> None:
        transcript = Transcript()
        notifier = TranscriptNotifier(transcript)
        runtime = build_runtime(settings, notifier=notifier, web=False)
        try:
            await story(runtime, transcript, notifier)
        finally:
            await runtime.aclose()

    asyncio.run(live())
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import run_demo, run_offline_demo

    return _run_story(args, run_offline_demo, run_demo)


def cmd_attack(args: argparse.Namespace) -> int:
    from .demo import run_attack, run_offline_attack

    return _run_story(args, run_offline_attack, run_attack)


# ----------------------------------------------------------------------
# proofs
# ----------------------------------------------------------------------


def _print_checks(rows: Sequence[tuple[bool, str, str]], *, simulated: bool) -> None:
    if simulated:
        print("SAMPLE: offline, in-memory vault; nothing below comes from a real network.")
    for ok, name, text in rows:
        print(f"[{'ok' if ok else 'FAIL'}] {name}: {text}")


def _offline_runtime(settings: Settings) -> Any:
    """The in-memory world on simulated time, without the web app."""
    from .chain.fake import SimClock
    from .runtime import build_runtime

    return build_runtime(settings, clock=SimClock(), web=False)


def cmd_prove(args: argparse.Namespace) -> int:
    from .chain.keys import derive_user
    from .chain.prove import prove, prove_fake

    settings = _settings_for(args)
    if _offline(settings):
        runtime = _offline_runtime(settings)
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

    settings = _settings_for(args)
    if _offline(settings):
        runtime = _offline_runtime(settings)
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
    from pathlib import Path

    from .chain.deploy import deploy

    # Settings of the target network: never the offline world's (its secret is public).
    base = load_settings(network=args.network)
    credentials = Path(os.environ.get("HOLDUP_CREDENTIALS") or ".holdup/credentials.json")
    data = asyncio.run(
        deploy(
            base,
            network=args.network,
            profile=args.profile,
            root=args.root,
            reserve_near=args.reserve,
            credentials_path=credentials,
        )
    )
    print(f"Deployed to {data['network']}: vault {data['vault_account']}, oracle {data['oracle_accounts'][0]}, "
          f"keeper {data['keeper_account']}, token {data['husd_token']}.")
    for label, tx_hash in data["deployment"]["transactions"]:
        print(f"  {label}: {tx_hash}")
    print(f"Credentials written to {credentials}. Run `holdup verify-deploy --network {data['network']}` next.")
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
                if self.runtime.resolver is None:
                    self.out("Scenarios only exist when recorded AI responses are replayed (OTARI_MODE=replay).")
                else:
                    self.runtime.resolver.current = rest or None
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
    from .chain.fake import SimClock
    from .demo import Transcript, TranscriptNotifier
    from .runtime import build_runtime

    settings = _settings_for(args)
    if not _offline(settings) and (code := _explain_missing(settings, "chat")):
        return code
    notifier = TranscriptNotifier(Transcript())
    clock = SimClock() if _offline(settings) else None
    runtime = build_runtime(settings, notifier=notifier, clock=clock, web=False)

    async def loop() -> None:
        console = Console(runtime, notifier)
        where = "offline, in memory" if _offline(settings) else settings.network
        print(f"HoldUp console ({where}; checks: {runtime.checks}). Type help.")
        try:
            await console.login(args.name)
            while True:
                try:
                    line = await asyncio.to_thread(input, "> ")
                except EOFError:
                    break
                if not await console.handle(line):
                    break
        finally:
            await runtime.aclose()

    asyncio.run(loop())
    return 0


# ----------------------------------------------------------------------
# long-running processes
# ----------------------------------------------------------------------


def cmd_keeper(args: argparse.Namespace) -> int:
    from .runtime import build_runtime

    settings = _settings_for(args)
    if _offline(settings):
        print("The keeper releases holds on a real network; offline holds live in memory. "
              "Use `holdup chat` and its wait command to see releases offline.")
        return 2
    if code := _explain_missing(settings, "keeper"):
        return code
    runtime = build_runtime(settings, web=False)
    print(f"Keeper running on {settings.network} every {args.interval} s (Ctrl-C to stop).")

    async def run() -> None:
        try:
            await runtime.engine.make_keeper(args.interval).run()
        finally:
            await runtime.aclose()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .runtime import build_runtime

    settings = _settings_for(args)
    if not _offline(settings) and (code := _explain_missing(settings, "serve")):
        return code
    runtime = build_runtime(settings)
    where = "offline, in-memory vault" if _offline(settings) else settings.network
    bot = "on" if runtime.bot is not None else "off (TELEGRAM_BOT_TOKEN is not set)"
    print(f"HoldUp on http://{args.host}:{args.port} ({where}; checks: {runtime.checks}; Telegram {bot}).")

    async def run() -> None:
        server = uvicorn.Server(uvicorn.Config(runtime.app, host=args.host, port=args.port, log_level="info"))
        stop = asyncio.Event()
        keeper = asyncio.create_task(runtime.engine.make_keeper(args.interval).run(stop))
        try:
            await server.serve()
        finally:
            stop.set()
            await keeper
            await runtime.aclose()

    asyncio.run(run())
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    from .risk.probe import main as probe_main

    return probe_main(args.probe_args)


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
    demo.add_argument("--rules-only", action="store_true", help="deterministic rules only, no AI step")
    demo.set_defaults(func=cmd_demo)

    attack = with_network(sub.add_parser("attack", help="three attacks and why they fail"))
    attack.add_argument("--rules-only", action="store_true", help="deterministic rules only, no AI step")
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

    # Everything after `probe` goes to holdup.risk.probe (try `holdup probe --help`).
    probe = sub.add_parser("probe", help="live checks of the Otari gateway (needs OTARI_API_KEY)", add_help=False)
    probe.set_defaults(func=cmd_probe, probe_args=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    if args.command == "probe":
        args.probe_args = extra
    elif extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
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
