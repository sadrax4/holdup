"""``holdup`` command line: demo, attack, prove, verify-deploy, deploy, chat, keeper, serve, probe.

Commands run offline (in memory, nothing sent anywhere) unless a network is
chosen with ``--network`` or ``HOLDUP_NETWORK``. Every command gets its vault,
checks, store and engine from :func:`holdup.runtime.build_runtime`.

``serve`` listens on a loopback address unless ``--allow-public`` is given:
on a shared server it is reached through a tunnel or a reverse proxy, never
directly.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import socket
import sys
import threading
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from .config import OFFLINE_SECRET, Settings, load_settings, port_from_env
from .messages import error_text, fmt_amount, fmt_utc
from .models import PaymentContext, parse_amount, reasons_from_bits

log = logging.getLogger("holdup")

NETWORKS = ("sandbox", "testnet")
KEEPER_STOP_S = 20.0  # how long `serve` waits for a keeper round in progress when it stops
# Seconds between keeper rounds unless --interval says otherwise. Testnet holds last minutes, so
# a 15 s round is soon enough and keeps the idle load on the shared public RPC low.
KEEPER_INTERVAL_S = {"testnet": 15.0}
DEFAULT_KEEPER_INTERVAL_S = 5.0

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
    """The settings for a command, with a configuration problem reported in one line."""
    try:
        if getattr(args, "offline", False):
            return load_settings(network="offline")
        return load_settings(network=getattr(args, "network", None))
    except ValueError as exc:
        print(f"holdup: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _offline(settings: Settings) -> bool:
    return settings.network == "offline"


def _runtime(settings: Settings, **kwargs: Any) -> Any:
    """``build_runtime``, with a configuration problem reported in one line instead of a traceback."""
    from .runtime import build_runtime

    try:
        return build_runtime(settings, **kwargs)
    except ValueError as exc:
        print(f"holdup: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


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

    settings = _settings_for(args)
    if _offline(settings):
        asyncio.run(offline_story(rules_only=args.rules_only))
        return 0
    if code := _explain_missing(settings, args.command):
        return code
    if args.rules_only:
        settings.otari_mode = "off"
    elif not settings.otari_api_key and not os.environ.get("OTARI_MODE"):
        # Without a key the story replays its recorded AI answers, one scenario per demo payee
        # (a real network otherwise runs the rules alone).
        settings.otari_mode = "replay"

    async def live() -> None:
        transcript = Transcript()
        notifier = TranscriptNotifier(transcript)
        # The story chooses the recorded answers for its own payees, so replay is allowed here.
        runtime = _runtime(settings, notifier=notifier, web=False, allow_replay=True)
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
        for note in report.notes:
            print(f"[skip] {note}")
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

    from .chain.deploy import DeployRefused, deploy

    # Settings of the target network: never the offline world's (its secret is public).
    base = load_settings(network=args.network)
    credentials = Path(os.environ.get("HOLDUP_CREDENTIALS") or ".holdup/credentials.json")
    # The balance check logs the budget line by line before anything is sent, and the RPC client
    # logs its retries: both are worth seeing during a deployment.
    logging.getLogger("holdup.chain").setLevel(min(logging.INFO, logging.getLogger().getEffectiveLevel()))
    try:
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
    except DeployRefused as exc:  # the root cannot pay for everything; the message says how short it is
        print(f"holdup deploy: {exc}", file=sys.stderr)
        return 2
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
                    reasons = ",".join(r.name or str(int(r)) for r in reasons_from_bits(hold.reason_bits))
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

    settings = _settings_for(args)
    if not _offline(settings) and (code := _explain_missing(settings, "chat")):
        return code
    notifier = TranscriptNotifier(Transcript())
    clock = SimClock() if _offline(settings) else None
    runtime = _runtime(settings, notifier=notifier, clock=clock, web=False)

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


def keeper_interval(args: argparse.Namespace, settings: Settings) -> float:
    """``--interval``, else the network's default (15 s on testnet, 5 s elsewhere)."""
    if args.interval is not None:
        return float(args.interval)
    return KEEPER_INTERVAL_S.get(settings.network, DEFAULT_KEEPER_INTERVAL_S)


def cmd_keeper(args: argparse.Namespace) -> int:
    settings = _settings_for(args)
    if _offline(settings):
        print("The keeper releases holds on a real network; offline holds live in memory. "
              "Use `holdup chat` and its wait command to see releases offline.")
        return 2
    if code := _explain_missing(settings, "keeper"):
        return code
    runtime = _runtime(settings, web=False)
    interval = keeper_interval(args, settings)
    print(f"Keeper running on {settings.network} every {interval:g} s (Ctrl-C to stop).")

    async def run() -> None:
        try:
            await runtime.run_keeper(asyncio.Event(), interval)  # until Ctrl-C
        finally:
            await runtime.aclose()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())
    return 0


class SecretFilter(logging.Filter):
    """Replaces configured secret values in log records, tracebacks included, with ``[redacted]``.

    Attached to the log handlers of ``holdup serve`` as a last line of defence:
    the code already avoids logging secrets, but a library message or an
    exception text could still carry one (Telegram's InvalidToken does).
    """

    def __init__(self, values: Iterable[str]) -> None:
        super().__init__()
        # Short values would redact ordinary words; real secrets are long.
        self.values = sorted({v for v in values if len(v) >= 8}, key=len, reverse=True)

    def clean(self, text: str) -> str:
        for value in self.values:
            text = text.replace(value, "[redacted]")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.values:
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a filter that raises breaks the logging call; logging reports it
            return True
        if self.clean(message) != message:
            # Keep the arguments separate where possible (uvicorn's access log formats them itself).
            if isinstance(record.msg, str):
                record.msg = self.clean(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(self.clean(a) if isinstance(a, str) else a for a in record.args)
            if self.clean(record.getMessage()) != record.getMessage():
                record.msg, record.args = self.clean(record.getMessage()), None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.clean(record.exc_text)
        return True


class QuietHealthChecks(logging.Filter):
    """Leaves successful ``GET /health`` requests out of uvicorn's access log.

    A supervisor asks every few seconds; those lines would bury everything else.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5:
            _client, method, path, _version, status = args
            return not (method == "GET" and str(path).split("?", 1)[0] == "/health" and status == 200)
        return True


def keep_secrets_out_of_logs(values: Iterable[str]) -> None:
    """Add a :class:`SecretFilter` to the handlers that ``holdup serve`` writes its logs through."""
    secret_filter = SecretFilter(values)
    for name in ("", "uvicorn", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            handler.addFilter(secret_filter)


def is_loopback(host: str) -> bool:
    """Whether ``host`` only accepts connections from this machine (127.0.0.0/8, ::1, localhost)."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def listen(host: str, port: int) -> socket.socket:
    """A listening TCP socket. Binding before anything starts makes a busy port fail at once."""
    address = "127.0.0.1" if host.lower() == "localhost" else host.strip("[]")
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return socket.create_server((address, port), family=family, backlog=2048)


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def _uvicorn_log_config() -> dict[str, Any]:
    """uvicorn's own logging setup with a timestamp on every line: `serve` runs for weeks."""
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    for formatter in config["formatters"].values():
        formatter["fmt"] = "%(asctime)s " + formatter["fmt"]
    return config


def _let_shutdown_finish() -> None:
    """uvicorn stops gracefully on SIGINT or SIGTERM and then raises the signal again.

    With these no-op handlers in place, that second signal does not end the
    process before the keeper and the runtime are closed.
    """
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda signum, frame: None)


def _telegram_summary(settings: Settings) -> str:
    if not settings.telegram_bot_token:
        return "off (TELEGRAM_BOT_TOKEN is not set)"
    if settings.bot_mode() == "off":
        return "off (TELEGRAM_MODE=off)"
    return settings.bot_mode()


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = _settings_for(args)
    if not _offline(settings) and (code := _explain_missing(settings, "serve")):
        return code
    if not args.allow_public and not is_loopback(args.host):
        print(
            f"holdup serve: --host {args.host} is not a loopback address, so other machines could connect. "
            "Serve on 127.0.0.1 behind a tunnel or reverse proxy, or add --allow-public if that is what you want.",
            file=sys.stderr,
        )
        return 2
    try:
        port = args.port if args.port is not None else port_from_env(os.environ)
        sock = listen(args.host, port)
    except ValueError as exc:
        print(f"holdup serve: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"holdup serve: cannot listen on {args.host}:{port}: {exc.strerror or exc}", file=sys.stderr)
        return 2
    bound_host, bound_port = sock.getsockname()[:2]
    url = f"http://{_url_host(bound_host)}:{bound_port}"
    if not os.environ.get("PUBLIC_BASE_URL") and is_loopback(bound_host):
        settings.public_base_url = url  # links then point where this server really listens
    if settings.secret == OFFLINE_SECRET:
        # The offline world starts from scratch on every restart anyway, so a server that may be
        # reachable by others signs its cookies, export links and hold tokens with a fresh secret.
        settings.secret = secrets.token_urlsafe(32)
    logging.getLogger("holdup").setLevel(min(logging.INFO, logging.getLogger().getEffectiveLevel()))
    try:
        runtime = _runtime(settings)
    except BaseException:
        sock.close()
        raise
    where = "offline, in-memory vault" if _offline(settings) else settings.network
    print(
        f"HoldUp listening on {url} ({where}; checks: {runtime.checks}; "
        f"Telegram: {_telegram_summary(settings)}).",
        flush=True,
    )
    if not settings.web_is_public():
        left_out = "; the bot leaves them out" if runtime.bot is not None else ""
        print(
            f"Web links point to {settings.public_base_url}, which only this machine can open{left_out}. "
            "Set PUBLIC_BASE_URL to the public https address to change that.",
            flush=True,
        )
    if settings.bot_mode() == "webhook" and (
        not settings.public_base_url.startswith("https://") or not settings.telegram_webhook_secret
    ):
        print(
            "TELEGRAM_MODE=webhook needs an https PUBLIC_BASE_URL and TELEGRAM_WEBHOOK_SECRET; until then "
            "Telegram delivers nothing. TELEGRAM_MODE=polling needs neither.",
            flush=True,
        )

    async def run() -> None:
        config = uvicorn.Config(
            runtime.app, log_level="info", log_config=_uvicorn_log_config(), timeout_graceful_shutdown=10
        )
        server = uvicorn.Server(config)
        keep_secrets_out_of_logs(settings.secret_values())  # after uvicorn has set up its handlers
        logging.getLogger("uvicorn.access").addFilter(QuietHealthChecks())
        _let_shutdown_finish()
        stop = asyncio.Event()
        keeper = asyncio.create_task(runtime.run_keeper(stop, keeper_interval(args, settings)))
        try:
            await server.serve(sockets=[sock])
        finally:
            stop.set()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(keeper, timeout=KEEPER_STOP_S)
            await runtime.aclose()
            sock.close()

    asyncio.run(run())
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    from .risk.probe import main as probe_main

    return probe_main(args.probe_args)


# ----------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------


def _port(text: str) -> int:
    if not text.isdigit() or not 0 <= int(text) <= 65535:
        raise argparse.ArgumentTypeError(f"not a port number: {text!r}")
    return int(text)


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
    deploy.add_argument(
        "--reserve", default=None, help="NEAR the vault keeps above its storage and deploy gas (default 0.25)"
    )
    deploy.set_defaults(func=cmd_deploy)

    chat = with_network(sub.add_parser("chat", help="interactive console on the same engine"))
    chat.add_argument("--name", default="You", help="your display name")
    chat.set_defaults(func=cmd_chat)

    keeper = with_network(sub.add_parser("keeper", help="release due holds in a loop"))
    keeper.add_argument(
        "--interval", type=float, default=None, help="seconds between rounds (default 15 on testnet, else 5)"
    )
    keeper.set_defaults(func=cmd_keeper)

    serve = with_network(sub.add_parser("serve", help="web app, Telegram bot (polling or webhook) and keeper"))
    serve.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1, loopback only)")
    serve.add_argument("--port", type=_port, default=None, help="port (default: HOLDUP_PORT, else 8000; 0 picks one)")
    serve.add_argument(
        "--allow-public", action="store_true", help="allow a --host that other machines can reach, such as 0.0.0.0"
    )
    serve.add_argument(
        "--interval", type=float, default=None, help="keeper interval in seconds (default 15 on testnet, else 5)"
    )
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
        format=("%(asctime)s " if args.command == "serve" else "") + "%(levelname)s %(name)s: %(message)s",
    )
    # httpx (python-telegram-bot) and httpx2 (openai, mcp) log every request URL at INFO, and
    # Bot API URLs contain the bot token.
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        return int(args.func(args) or 0)
    except BrokenPipeError:  # output piped into a command that stopped reading, e.g. head
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
