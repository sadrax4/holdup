"""`holdup serve`: loopback only, its port, the bound address, secrets kept out of logs, a clean stop.

The last two tests start the real command in a child process with a minimal
environment: offline, so nothing is sent anywhere, and with the bot pointed at
the fake Bot API of ``test_bot_polling``.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_bot_fakes import BOT_TOKEN, message_update
from test_bot_polling import FakeBotApi, wait_until

from holdup.cli import (
    QuietHealthChecks,
    SecretFilter,
    build_parser,
    is_loopback,
    keeper_interval,
    main,
)
from holdup.config import Settings, offline_settings

ROOT = Path(__file__).resolve().parents[1]
CHANNEL_VARS = (
    "HOLDUP_PORT",
    "HOLDUP_NETWORK",
    "PUBLIC_BASE_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_MODE",
    "TELEGRAM_API_BASE_URL",
    "TELEGRAM_WEBHOOK_SECRET",
    "OTARI_API_KEY",
    "OTARI_MODE",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in CHANNEL_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOLDUP_CREDENTIALS", str(tmp_path / "none.json"))


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# ----------------------------------------------------------------------
# refusing to start
# ----------------------------------------------------------------------


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "203.0.113.7", "holdup.example"])
def test_serve_refuses_an_address_other_machines_can_reach(host: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["serve", "--offline", "--host", host]) == 2
    err = capsys.readouterr().err
    assert "not a loopback address" in err and "--allow-public" in err


@pytest.mark.parametrize(
    ("host", "loopback"),
    [("127.0.0.1", True), ("127.0.0.2", True), ("::1", True), ("[::1]", True), ("localhost", True),
     ("LOCALHOST", True), ("0.0.0.0", False), ("::", False), ("10.0.0.5", False), ("holdup.example", False)],
)
def test_loopback_addresses(host: str, loopback: bool) -> None:
    assert is_loopback(host) is loopback


def test_a_busy_port_is_reported_before_anything_starts(capsys: pytest.CaptureFixture[str]) -> None:
    with socket.create_server(("127.0.0.1", 0)) as busy:
        port = busy.getsockname()[1]
        assert main(["serve", "--offline", "--port", str(port)]) == 2
    assert f"cannot listen on 127.0.0.1:{port}" in capsys.readouterr().err


async def test_the_offline_server_can_give_new_payees_the_hosted_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live 2026-10-31: the public offline server ran the demo profile, so its 20 s hold ended before a live
    AI check (17-57 s) did, and every honest payment to a new payee failed closed and was flagged."""
    from holdup import cli
    from holdup.runtime import build_runtime

    asked: dict[str, Any] = {}

    def runtime(settings: Settings, **kwargs: Any) -> Any:
        asked.update(kwargs)
        raise SystemExit(3)

    monkeypatch.setattr(cli, "_runtime", runtime)
    with pytest.raises(SystemExit):
        main(["serve", "--offline", "--port", "0", "--profile", "hosted"])
    assert asked == {"profile": "hosted"}
    assert build_parser().parse_args(["serve", "--offline"]).profile == "demo"
    built = build_runtime(offline_settings(), web=False, profile="hosted")
    try:
        assert (await built.vault.get_config()).new_payee_hold_ms == 5 * 60_000
    finally:
        await built.aclose()


def test_ports_are_checked(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["serve", "--offline", "--port", "70000"])
    assert exit_info.value.code == 2
    monkeypatch.setenv("HOLDUP_PORT", "eighty")
    with pytest.raises(SystemExit) as exit_info:
        main(["serve", "--offline"])
    assert exit_info.value.code == 2
    assert "HOLDUP_PORT must be a port number" in capsys.readouterr().err


def test_a_bad_telegram_mode_is_one_line_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TELEGRAM_MODE", "carrier-pigeon")
    with pytest.raises(SystemExit) as exit_info:
        main(["serve", "--offline"])
    assert exit_info.value.code == 2
    assert capsys.readouterr().err.startswith("holdup: TELEGRAM_MODE must be one of polling, webhook, off")


# ----------------------------------------------------------------------
# keeping secrets out of logs
# ----------------------------------------------------------------------


def record(msg: str, *args: Any, exc: BaseException | None = None) -> logging.LogRecord:
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    return logging.LogRecord("test", logging.WARNING, __file__, 1, msg, args, exc_info)


def formatted(rec: logging.LogRecord) -> str:
    return logging.Formatter("%(message)s").format(rec)


def test_the_secret_filter_redacts_messages_arguments_and_tracebacks() -> None:
    secret_filter = SecretFilter([BOT_TOKEN, "hook-secret_123", "short", ""])
    rec = record("HTTP Request: POST %s", f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates")
    assert secret_filter.filter(rec)
    assert formatted(rec) == "HTTP Request: POST https://api.telegram.org/bot[redacted]/getUpdates"

    # uvicorn's access log formats its five arguments itself: they must stay a tuple.
    access = record('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "POST", "/tg/hook-secret_123", "1.1", 200)
    secret_filter.filter(access)
    assert access.args == ("127.0.0.1:5000", "POST", "/tg/[redacted]", "1.1", 200)

    try:
        raise ValueError(f"The token `{BOT_TOKEN}` was rejected by the server.")
    except ValueError as exc:
        failed = record("could not start", exc=exc)
    secret_filter.filter(failed)
    assert "[redacted]" in formatted(failed) and BOT_TOKEN not in formatted(failed)

    harmless = record("a short word stays: %s", "short")
    secret_filter.filter(harmless)
    assert formatted(harmless) == "a short word stays: short"


def test_successful_health_checks_stay_out_of_the_access_log() -> None:
    quiet = QuietHealthChecks()

    def access(method: str, path: str, status: int) -> logging.LogRecord:
        return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                                 ("127.0.0.1:50000", method, path, "1.1", status), None)

    assert not quiet.filter(access("GET", "/health", 200))
    assert not quiet.filter(access("GET", "/health?from=supervisor", 200))
    assert quiet.filter(access("GET", "/health", 503))  # a failing check is worth a line
    assert quiet.filter(access("GET", "/h/1", 200))
    assert quiet.filter(access("POST", "/mcp", 200))
    assert quiet.filter(record("not an access line"))


def test_the_keeper_runs_every_15_s_on_testnet_and_every_5_s_elsewhere() -> None:
    parser = build_parser()
    testnet = Settings(network="testnet")
    for command in (["serve", "--offline"], ["keeper", "--network", "testnet"]):
        args = parser.parse_args(command)
        assert keeper_interval(args, testnet) == 15.0
        assert keeper_interval(args, offline_settings()) == 5.0
        assert keeper_interval(args, Settings(network="sandbox")) == 5.0
    assert keeper_interval(parser.parse_args(["serve", "--interval", "2"]), testnet) == 2.0


# ----------------------------------------------------------------------
# the real command, in a child process
# ----------------------------------------------------------------------


class Child:
    """A `holdup serve` process; its stdout and stderr are collected line by line."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self.lines: list[str] = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.append(line.rstrip("\n"))

    @property
    def output(self) -> str:
        return "\n".join(self.lines)

    def line_starting(self, prefix: str, timeout: float = 30.0) -> str:
        assert wait_until(lambda: any(line.startswith(prefix) for line in self.lines), timeout), self.output
        return next(line for line in self.lines if line.startswith(prefix))

    def stop(self, sig: int) -> int:
        self.process.send_signal(sig)
        code = self.process.wait(timeout=30)
        self._reader.join(timeout=5)
        return code


@contextmanager
def holdup_serve(tmp_path: Path, *args: str, **env: str) -> Iterator[Child]:
    """`python -m holdup serve --offline` with only the environment given here (no proxies, no secrets)."""
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "HOLDUP_CREDENTIALS": str(tmp_path / "none.json"),
        "PYTHONUNBUFFERED": "1",
        **env,
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "holdup", "serve", "--offline", "--interval", "0.2", *args],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    child = Child(process)
    try:
        yield child
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def get_json(url: str) -> dict[str, Any]:
    response = httpx.get(url, timeout=5, trust_env=False)
    response.raise_for_status()
    return dict(response.json())


def test_serve_listens_on_holdup_port_and_stops_cleanly_on_sigterm(tmp_path: Path) -> None:
    port = free_port()
    with holdup_serve(tmp_path, HOLDUP_PORT=str(port)) as child:
        url = f"http://127.0.0.1:{port}"
        line = child.line_starting("HoldUp listening on ")
        assert line.startswith(f"HoldUp listening on {url} (offline")
        assert "Telegram: off (TELEGRAM_BOT_TOKEN is not set)" in line
        # Without PUBLIC_BASE_URL, links point where the server really listens, and nobody else can open them.
        assert "only this machine can open" in child.output
        assert wait_until(lambda: get_json(f"{url}/health")["keeper"]["rounds"] > 0)
        health = get_json(f"{url}/health")
        assert health["public_base_url"] == url and health["telegram"]["mode"] == "off"
        assert health["keeper"]["running"] is True and health["keeper"]["last_error"] is None
        assert child.stop(signal.SIGTERM) == 0
    # uvicorn shut down gracefully, and the process outlived its re-raised SIGTERM.
    assert "Finished server process" in child.output and "Traceback" not in child.output


def test_serve_runs_the_bot_by_polling_and_stops_it_on_ctrl_c(tmp_path: Path) -> None:
    with FakeBotApi() as api, holdup_serve(
        tmp_path, "--port", "0", TELEGRAM_BOT_TOKEN=BOT_TOKEN, TELEGRAM_API_BASE_URL=api.url
    ) as child:
        line = child.line_starting("HoldUp listening on http://127.0.0.1:")
        assert "Telegram: polling" in line
        assert wait_until(lambda: "getUpdates" in api.names(), timeout=30), child.output
        api.push(message_update("/start"))
        assert wait_until(lambda: bool(api.sent()), timeout=15), child.output
        assert api.sent()[0]["text"].startswith("Welcome to HoldUp, Priya.")
        assert child.stop(signal.SIGINT) == 0
    assert api.names()[:3] == ["getMe", "setMyCommands", "deleteWebhook"]
    assert api.params("getUpdates")[-1]["timeout"] == 0  # polling was stopped, not killed
    assert "Telegram bot @HoldUpTestBot is polling for updates" in child.output
    assert BOT_TOKEN not in child.output and "Traceback" not in child.output
