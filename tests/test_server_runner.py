"""The server runner: scripts/server.sh and scripts/server_tools.py.

The first tests call server_tools directly. The others run the real
scripts/server.sh as an unprivileged user (``nobody`` when the suite runs as
root) in a temporary HOLDUP_HOME, with HOME pointed at a temporary directory,
an isolated tmux server (HOLDUP_TMUX_SOCKET), a fake cloudflared, a fake uv and
a fake Telegram Bot API; the app itself is the real ``holdup serve --offline``
from this checkout's .venv. Nothing goes to the network.
"""

from __future__ import annotations

import errno
import importlib.util
import json
import os
import pwd
import secrets
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fakes.telegram_api import FakeBotAPI

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "server.sh"
FAKES = Path(__file__).resolve().parent / "fakes"


def load_tools() -> Any:
    spec = importlib.util.spec_from_file_location("server_tools", REPO / "scripts" / "server_tools.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["server_tools"] = module  # dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


tools = load_tools()


def wait_for(condition: Callable[[], Any], timeout: float, what: str) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out after {timeout} s waiting for {what}")


# ----------------------------------------------------------------------
# env files
# ----------------------------------------------------------------------


def test_parse_env_reads_quoted_values_without_expanding_anything() -> None:
    text = "# comment\nA='one two'\nexport B=\"x\"\nC=plain # note\nD='$(rm -rf /) $HOME'\n\nE=\n"
    assert tools.parse_env(text) == {"A": "one two", "B": "x", "C": "plain", "D": "$(rm -rf /) $HOME", "E": ""}


def test_a_comment_after_a_quoted_value_is_not_part_of_it() -> None:
    text = "HOLDUP_SECRET='abc123def456'  # never change this\nTELEGRAM_MODE='polling' # default\nURL=\"a#b\" # x\n"
    assert tools.parse_env(text) == {"HOLDUP_SECRET": "abc123def456", "TELEGRAM_MODE": "polling", "URL": "a#b"}


@pytest.mark.parametrize("line", ["A='abc", "A='abc'x", "A='abc'#glued", "A='it's'"])
def test_a_quoted_value_with_text_after_its_quote_is_refused(line: str) -> None:
    with pytest.raises(tools.ToolError, match="line 2"):
        tools.parse_env("OK='1'\n" + line + "\n")


def test_parse_env_names_the_line_but_never_shows_it() -> None:
    with pytest.raises(tools.ToolError) as excinfo:
        tools.parse_env("A='1'\nthis is secret-ish garbage\n")
    assert "line 2" in str(excinfo.value)
    assert "garbage" not in str(excinfo.value)


@pytest.mark.parametrize("value", ["it's", 'say "hi"', "two\nlines", "cr\rhere"])
def test_values_with_quotes_or_line_breaks_are_refused(tmp_path: Path, value: str) -> None:
    with pytest.raises(tools.ToolError):
        tools.merge_env(tmp_path / "holdup.env", {"TELEGRAM_BOT_TOKEN": value})
    assert not (tmp_path / "holdup.env").exists()


def test_merge_env_adds_missing_values_and_never_replaces_one(tmp_path: Path) -> None:
    path = tmp_path / "holdup.env"
    added, kept = tools.merge_env(path, {"HOLDUP_SECRET": "first", "TELEGRAM_MODE": "polling", "EMPTY": ""})
    assert added == ["HOLDUP_SECRET", "TELEGRAM_MODE"] and kept == []
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "HOLDUP_SECRET='first'" in path.read_text()

    added, kept = tools.merge_env(path, {"HOLDUP_SECRET": "second", "OTARI_API_KEY": "otk_x"})
    assert added == ["OTARI_API_KEY"] and kept == ["HOLDUP_SECRET"]
    assert tools.read_env_file(path) == {"HOLDUP_SECRET": "first", "TELEGRAM_MODE": "polling",
                                         "OTARI_API_KEY": "otk_x"}
    assert path.read_text().startswith("# HoldUp settings and secrets")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["holdup.env"]  # no temporary files left


def test_secure_secrets_makes_holdups_files_private_and_leaves_others_alone(tmp_path: Path) -> None:
    folder = tmp_path / "secrets"
    folder.mkdir(mode=0o755)
    os.chmod(folder, 0o755)
    for name, mode in (("holdup.env", 0o644), ("holdup-credentials.json", 0o600),
                       ("holdup-credentials.json.20261101T000000Z.old", 0o640), ("other-bot.sh", 0o755)):
        (folder / name).write_text("x")
        os.chmod(folder / name, mode)
    changed = tools.secure_secrets(folder)
    assert len(changed) == 3 and any("holdup.env to mode 600 (it was 644)" in line for line in changed)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    for name in ("holdup.env", "holdup-credentials.json", "holdup-credentials.json.20261101T000000Z.old"):
        assert stat.S_IMODE((folder / name).stat().st_mode) == 0o600
    assert stat.S_IMODE((folder / "other-bot.sh").stat().st_mode) == 0o755  # not HoldUp's
    assert tools.secure_secrets(folder) == []
    with pytest.raises(tools.ToolError, match="does not exist"):
        tools.secure_secrets(tmp_path / "missing")


def test_env_get_prints_one_value_and_fails_when_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "holdup.env"
    path.write_text("TELEGRAM_MODE='polling'\n")
    assert tools.main(["env-get", str(path), "TELEGRAM_MODE"]) == 0
    assert capsys.readouterr().out == "polling\n"
    assert tools.main(["env-get", str(path), "OTARI_API_KEY"]) == 1


# ----------------------------------------------------------------------
# listening sockets and ports
# ----------------------------------------------------------------------

PROC_NET_TCP = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:223D 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 101 1
   1: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 102 1
   2: 0100007F:BAE4 0100007F:223D 01 00000000:00000000 00:00000000 00000000  1000        0 103 1
"""
PROC_NET_TCP6 = """\
  sl  local_address                         remote_address                        st tx_queue rx_queue
   0: 00000000000000000000000001000000:0277 00000000000000000000000000000000:0000 0A 00000000:00000000
   1: 00000000000000000000000000000000:01BB 00000000000000000000000000000000:0000 0A 00000000:00000000
"""


def test_proc_net_parsing_finds_listeners_only() -> None:
    v4 = tools.parse_proc_net(PROC_NET_TCP, ipv6=False)
    v6 = tools.parse_proc_net(PROC_NET_TCP6, ipv6=True)
    assert v4 == [tools.Listener("127.0.0.1", 8765), tools.Listener("0.0.0.0", 22)]
    assert v6 == [tools.Listener("::1", 631), tools.Listener("::", 443)]


def test_ss_parsing() -> None:
    text = (
        "LISTEN 0 4096 127.0.0.1:8765 0.0.0.0:*\n"
        "LISTEN 0 128 [::]:22 [::]:*\n"
        "LISTEN 0 4096 127.0.0.53%lo:53 0.0.0.0:*\n"
        "LISTEN 0 511 *:8080 *:*\n"
    )
    assert tools.parse_ss(text) == [
        tools.Listener("127.0.0.1", 8765), tools.Listener("::", 22),
        tools.Listener("127.0.0.53", 53), tools.Listener("*", 8080),
    ]


def test_listeners_include_a_socket_this_test_opens() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert tools.Listener("127.0.0.1", port) in tools.listeners()
        assert not tools.can_bind(port)
    assert tools.can_bind(port)


def test_choose_ports_keeps_free_saved_ports_and_replaces_taken_ones() -> None:
    busy = {47801, 47802}
    chosen = tools.choose_ports([47800, 47801, None], 47800, 47805, busy=busy.__contains__)
    assert chosen[0] == 47800
    assert len(set(chosen)) == 3
    assert not set(chosen) & busy
    assert all(47800 <= port <= 47805 for port in chosen)


def test_choose_ports_drops_saved_ports_outside_the_range_or_repeated() -> None:
    chosen = tools.choose_ports([8000, 47810, 47810], 47810, 47812, busy=lambda port: False)
    assert sorted(chosen) == [47810, 47811, 47812]


def test_choose_ports_fails_when_the_range_is_full() -> None:
    with pytest.raises(tools.ToolError, match="not enough free ports"):
        tools.choose_ports([None, None], 47800, 47801, busy=lambda port: port == 47800)


@pytest.mark.parametrize(("low", "high"), [("8000", "8010"), ("47790", "47810"), ("47990", "48010"), ("47900", "47800")])
def test_the_port_range_cannot_leave_47800_47999(low: str, high: str) -> None:
    with pytest.raises(tools.ToolError):
        tools.port_range({"HOLDUP_PORT_MIN": low, "HOLDUP_PORT_MAX": high})


def test_ensure_ports_saves_its_choice_and_avoids_a_real_listener(tmp_path: Path) -> None:
    path = tmp_path / "ports.env"
    env = {"HOLDUP_PORT_MIN": "47980", "HOLDUP_PORT_MAX": "47999"}
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        taken = next(p for p in range(47980, 48000) if tools.can_bind(p))
        sock.bind(("127.0.0.1", taken))
        sock.listen()
        tools.save_ports(path, {"HOLDUP_PORT": taken, "TUNNEL_METRICS_PORT": taken, "FIXTURE_METRICS_PORT": taken})
        ports, changed = tools.ensure_ports(path, env)
    assert taken not in ports.values()
    assert len(set(ports.values())) == 3
    assert set(changed) == set(tools.PORT_KEYS)
    assert tools.read_ports(path) == ports
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    again, changed = tools.ensure_ports(path, env)
    assert again == ports and changed == []


# ----------------------------------------------------------------------
# processes: only what was recorded, and verified, is ever signalled
# ----------------------------------------------------------------------


def sleeper(cwd: Path, *markers: str, new_session: bool = True) -> subprocess.Popen[bytes]:
    code = "import signal, time; signal.signal(signal.SIGTERM, lambda *a: exit(0)); time.sleep(60)"
    return subprocess.Popen([sys.executable, "-c", code, *markers], cwd=cwd, start_new_session=new_session)


def record_for(proc: subprocess.Popen[bytes], cwd: Path, name: str, *markers: str) -> Any:
    wait_for(lambda: markers[0] in " ".join(tools.proc_cmdline(proc.pid)), 5, "the child to start")
    info = tools.proc_info(proc.pid)
    assert info is not None
    return tools.ProcRecord(name=name, pid=proc.pid, start=info.start, pgid=info.pgid, cwd=str(cwd),
                            markers=list(markers))


def test_a_recorded_process_is_recognised_by_start_time_command_and_directory(tmp_path: Path) -> None:
    proc = sleeper(tmp_path, "holdup-test", "serve")
    try:
        record = record_for(proc, tmp_path, "app", "holdup-test", "serve")
        assert tools.is_ours(record, proc.pid)
        assert not tools.is_ours(tools.ProcRecord(**{**record.__dict__, "start": record.start + 1}), proc.pid)
        assert not tools.is_ours(tools.ProcRecord(**{**record.__dict__, "cwd": "/"}), proc.pid)
        assert not tools.is_ours(tools.ProcRecord(**{**record.__dict__, "markers": ["cloudflared"]}), proc.pid)
        assert not tools.is_ours(tools.ProcRecord(**{**record.__dict__, "uid": os.getuid() + 1}), proc.pid)
    finally:
        proc.kill()
        proc.wait()


def test_stop_recorded_stops_ours_and_leaves_a_look_alike_alone(tmp_path: Path) -> None:
    ours = sleeper(tmp_path, "holdup-test", "serve")
    twin = sleeper(tmp_path, "holdup-test", "serve")  # same command and directory, not recorded
    try:
        record = record_for(ours, tmp_path, "app", "holdup-test", "serve")
        record_for(twin, tmp_path, "twin", "holdup-test", "serve")
        messages: list[str] = []
        assert tools.stop_recorded([record], 5.0, messages.append) == []
        assert ours.wait(5) in (0, -signal.SIGTERM)  # SIGTERM may land before its handler is set
        assert twin.poll() is None
        assert messages == [f"stopping app (pid {ours.pid})"]
    finally:
        for proc in (ours, twin):
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def test_cleanup_drops_records_of_processes_that_are_gone_or_not_ours(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    stale = tools.ProcRecord(name="tunnel", pid=1, start=1, pgid=1, cwd="/", markers=["cloudflared"])
    tools.write_json(tools.record_path(run_dir, "tunnel"), stale.__dict__)
    (run_dir / "app.pid.json").write_text("not json")
    messages: list[str] = []
    assert tools.cleanup(run_dir, 1.0, messages.append)
    assert messages == []
    assert not tools.record_path(run_dir, "tunnel").exists()


def test_lock_is_free_unless_a_process_holds_it(tmp_path: Path) -> None:
    lock = tmp_path / "supervisor.lock"
    assert tools.lock_is_free(lock)
    code = (
        "import fcntl, os, sys, time; fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT); "
        "fcntl.flock(fd, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(30)"
    )
    holder = subprocess.Popen([sys.executable, "-c", code, str(lock)], stdout=subprocess.PIPE)
    try:
        assert holder.stdout is not None and holder.stdout.readline() == b"locked\n"
        assert not tools.lock_is_free(lock)
    finally:
        holder.kill()
        holder.wait()
    assert tools.lock_is_free(lock)


# ----------------------------------------------------------------------
# credentials, Telegram, health, logs
# ----------------------------------------------------------------------


def test_credentials_status_and_output_never_show_keys(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "holdup-credentials.json"
    assert tools.credentials_status(path)[0] == "none"
    path.write_text("{oops")
    assert tools.credentials_status(path)[0] == "invalid"
    partial = {"network": "testnet", "root_account": "holdup-abc123.testnet", "treasury_private_key": "ed25519:SECRETKEY"}
    path.write_text(json.dumps(partial))
    assert tools.credentials_status(path) == ("partial", {"network": "testnet", "root_account": "holdup-abc123.testnet"})
    complete = {**partial, "vault_account": "vault.holdup-abc123.testnet", "keeper_account": "k", "husd_token": "h",
                "oracle_accounts": ["o"], "oracle_private_key": "ed25519:ORACLEKEY", "keeper_private_key": "ed25519:KEEPERKEY",
                "secret": "SHAREDSECRET"}
    path.write_text(json.dumps(complete))
    assert tools.credentials_status(path)[0] == "complete"
    assert tools.main(["credentials", str(path)]) == 0
    assert tools.main(["credentials", str(path), "root_account"]) == 0
    assert tools.main(["credentials", str(path), "treasury_private_key"]) == 2
    output = capsys.readouterr()
    assert "holdup-abc123.testnet" in output.out
    for secret in ("SECRETKEY", "ORACLEKEY", "KEEPERKEY", "SHAREDSECRET"):
        assert secret not in output.out + output.err


def test_telegram_username_from_get_me() -> None:
    ok = {"ok": True, "result": {"id": 1, "is_bot": True, "username": "HoldUp_HoldUp_bot"}}
    assert tools.telegram_username(json.dumps(ok)) == "HoldUp_HoldUp_bot"
    with pytest.raises(tools.ToolError, match="Unauthorized"):
        tools.telegram_username(json.dumps({"ok": False, "error_code": 401, "description": "Unauthorized"}))
    with pytest.raises(tools.ToolError):
        tools.telegram_username("<html>")


def test_health_line_mentions_what_matters() -> None:
    health = {"ok": True, "network": "testnet",
              "telegram": {"mode": "polling", "running": False, "username": "bot", "conflict": True,
                           "last_error": "Conflict: terminated by other getUpdates request"},
              "keeper": {"running": True, "last_error": None}}
    line = tools.health_line(health)
    assert line.startswith("ok; network testnet; Telegram polling as @bot (not running)")
    assert "CONFLICT" in line and "keeper running" in line
    assert tools.health_line(None) == "no answer"


def test_rotate_log_keeps_two_old_copies(tmp_path: Path) -> None:
    log = tmp_path / "app.log"
    for round_number in range(3):
        log.write_text(f"round {round_number}\n" * 100)
        assert tools.rotate_log(log, 100, keep=2)
    assert log.read_text() == ""
    assert (tmp_path / "app.log.1").read_text().startswith("round 2")
    assert (tmp_path / "app.log.2").read_text().startswith("round 1")
    assert not (tmp_path / "app.log.3").exists()
    assert not tools.rotate_log(log, 100, keep=2)


def test_root_names() -> None:
    for _ in range(20):
        name = tools.default_root_name()
        assert tools.ACCOUNT_NAME.match(name) and len(name) == len("holdup-abc123.testnet")
    assert tools.main(["check-root-name", "--", "my-holdup.testnet"]) == 0
    assert tools.main(["check-root-name", "--", "a" * 40 + ".testnet"]) == 0  # 48 characters
    # 49: northwind-paper.<root> would be longer than an account id may be.
    for bad in ("x.near", "a.b.testnet", "Upper.testnet", "-x.testnet", "x_.testnet", "a" * 41 + ".testnet"):
        assert tools.main(["check-root-name", "--", bad]) == 1


def test_backoff_doubles_up_to_the_cap() -> None:
    backoff = tools.Backoff(5, 60)
    assert [backoff.failed() for _ in range(6)] == [5, 10, 20, 40, 60, 60]
    backoff.reset()
    assert backoff.failed() == 5


def test_get_json_locally_and_through_urllib(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b'{"hostname": "a-b.trycloudflare.com"}' if self.path == "/quicktunnel" else b"nope"
            self.send_response(200 if self.path == "/quicktunnel" else 503)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("NO_PROXY", "*")  # the urllib path honours proxies; this test must not use one
    monkeypatch.setenv("no_proxy", "*")
    try:
        for local in (True, False):
            assert tools.get_json(f"{base}/quicktunnel", 2, local=local) == (200, {"hostname": "a-b.trycloudflare.com"})
            assert tools.get_json(f"{base}/ready", 2, local=local) == (503, None)
    finally:
        server.shutdown()
        server.server_close()
    assert tools.get_json(f"{base}/quicktunnel", 1) == (0, None)  # nothing listens any more
    assert tools.get_json("https://example.invalid/", 1) == (0, None)  # local fetches are plain http only


# ----------------------------------------------------------------------
# what the supervisor starts
# ----------------------------------------------------------------------


def supervisor_for(tmp_path: Path, **env: str) -> Any:
    base = {"HOME": str(tmp_path), "HOLDUP_HOME": str(tmp_path / "work"), "HOLDUP_UV": "/opt/uv",
            "HOLDUP_CLOUDFLARED": "/opt/cloudflared", "PATH": "/usr/bin:/bin"}
    config = tools.Config.from_env({**base, **env})
    supervisor = tools.Supervisor(config, out=lambda line: None)
    supervisor.ports = {"HOLDUP_PORT": 47900, "TUNNEL_METRICS_PORT": 47901, "FIXTURE_METRICS_PORT": 47902}
    return supervisor


def test_config_defaults_and_checks(tmp_path: Path) -> None:
    config = supervisor_for(tmp_path).cfg
    assert config.secrets_dir == tmp_path / "work" / "secrets"
    assert config.data_dir == tmp_path / "work" / "holdup-data"
    assert config.credentials.name == "holdup-credentials.json"
    assert (config.network, config.protocol, config.edge_ip_version) == ("testnet", "http2", "4")
    assert (config.backoff_min, config.backoff_max, config.check_interval) == (5.0, 60.0, 10.0)
    # QUIC, which "auto" tries first, binds a UDP socket on every interface: not allowed here.
    for name, value in (("HOLDUP_NETWORK", "mainnet"), ("HOLDUP_TUNNEL_PROTOCOL", "h3"),
                        ("HOLDUP_TUNNEL_PROTOCOL", "quic"), ("HOLDUP_TUNNEL_PROTOCOL", "auto"),
                        ("HOLDUP_BACKOFF_MIN", "soon"), ("HOLDUP_STOP_TIMEOUT", "-1")):
        with pytest.raises(tools.ToolError):
            supervisor_for(tmp_path, **{name: value})


def test_the_app_is_started_with_uv_locked_on_loopback(tmp_path: Path) -> None:
    supervisor = supervisor_for(tmp_path)
    assert supervisor.app_argv() == [
        "/opt/uv", "run", "--locked", "--no-dev", "--no-managed-python", "--no-python-downloads",
        "holdup", "serve", "--network", "testnet", "--host", "127.0.0.1", "--port", "47900",
    ]
    offline = supervisor_for(tmp_path, HOLDUP_NETWORK="offline")
    assert offline.app_argv()[7:9] == ["serve", "--offline"]


def test_the_app_environment_comes_from_holdup_env_with_our_urls_on_top(tmp_path: Path) -> None:
    supervisor = supervisor_for(tmp_path)
    supervisor.cfg.secrets_dir.mkdir(parents=True)
    tools.merge_env(supervisor.cfg.env_file, {"TELEGRAM_BOT_TOKEN": "1:abc", "PUBLIC_BASE_URL": "https://old.example",
                                              "HOLDUP_DB": "/elsewhere.db"})
    env = supervisor.app_env(("https://a-b.trycloudflare.com", "https://c-d.trycloudflare.com"))
    assert env["TELEGRAM_BOT_TOKEN"] == "1:abc"
    assert env["PUBLIC_BASE_URL"] == "https://a-b.trycloudflare.com"
    assert env["FIXTURE_BASE_URL"] == "https://c-d.trycloudflare.com"
    assert env["HOLDUP_DB"] == str(supervisor.cfg.data_dir / "holdup.sqlite3")
    assert env["HOLDUP_CREDENTIALS"] == str(supervisor.cfg.credentials)
    assert env["HOLDUP_PORT"] == "47900" and env["HOLDUP_NETWORK"] == "testnet"
    local_only = supervisor.app_env(None)
    assert "PUBLIC_BASE_URL" not in local_only
    # The training pages keep a host of their own that only this machine reaches.
    assert local_only["FIXTURE_BASE_URL"] == "http://localhost:47900"


def test_three_failed_public_checks_replace_the_tunnel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor = supervisor_for(tmp_path, HOLDUP_PUBLIC_CHECK_INTERVAL="1")
    supervisor.cfg.logs.mkdir(parents=True)
    supervisor.status = "running"
    supervisor.tunnel.hostname, supervisor.tunnel.ready = "a-b.trycloudflare.com", True
    supervisor.tunnel.proc = object()  # type: ignore[assignment]
    asked: list[str] = []
    replaced: list[Any] = []
    monkeypatch.setattr(tools, "get_json", lambda url, timeout, local=True: (asked.append(url), (0, None))[1])

    def replace(tunnel: Any, now: float) -> None:  # what the real one leaves behind: no tunnel, no URL
        replaced.append(tunnel)
        tunnel.proc, tunnel.hostname, tunnel.ready = None, None, False

    monkeypatch.setattr(supervisor, "replace", replace)
    now = 1000.0
    for _ in range(12):
        supervisor.step_public_check(now)
        if supervisor._public_thread is not None:
            supervisor._public_thread.join(5)
        now += 1.5
    assert len(asked) == 3 and set(asked) == {"https://a-b.trycloudflare.com/health"}
    assert replaced == [supervisor.tunnel]
    assert supervisor.public_ok is False


class Running:
    """Stands in for a Popen whose process is still running."""

    pid = 4242

    def poll(self) -> None:
        return None


def test_app_health_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor = supervisor_for(tmp_path, HOLDUP_APP_TIMEOUT="30")
    supervisor.cfg.logs.mkdir(parents=True)
    events: list[str] = []
    monkeypatch.setattr(supervisor, "stop_child", lambda child: events.append("stop"))
    monkeypatch.setattr(supervisor, "schedule", lambda child, now: events.append("schedule"))
    monkeypatch.setattr(tools, "app_python_pid", lambda app: 4243)
    answers: list[tuple[int, Any]] = []
    monkeypatch.setattr(tools, "get_json", lambda url, timeout, local=True: answers.pop(0))
    app = supervisor.app
    app.proc, app.started, app.urls = Running(), 0.0, None  # type: ignore[assignment]

    answers += [(0, None)] * 3  # starting: not answering yet is fine until the timeout
    for now in (1.0, 2.0, 3.0):
        supervisor.step_app(now, check=True)
    assert events == [] and not app.healthy

    answers += [(200, {"ok": True}), (0, None), (200, {"ok": True})]
    for now in (4.0, 5.0, 6.0):
        supervisor.step_app(now, check=True)
    assert events == [] and app.healthy and app.health_failures == 0 and app.python_pid == 4243

    answers += [(0, None), (500, None), (200, {"ok": False})]
    for now in (7.0, 8.0, 9.0):
        supervisor.step_app(now, check=True)
    assert events == ["stop", "schedule"]

    events.clear()
    app.proc, app.started, app.answered, app.healthy = Running(), 100.0, False, False  # type: ignore[assignment]
    answers += [(0, None)]
    supervisor.step_app(131.0, check=True)  # never answered within HOLDUP_APP_TIMEOUT
    assert events == ["stop", "schedule"]


def test_without_tunnels_the_app_starts_without_a_public_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor = supervisor_for(tmp_path, HOLDUP_TUNNEL_TIMEOUT="90")
    supervisor.cfg.logs.mkdir(parents=True)
    started: list[Any] = []
    monkeypatch.setattr(supervisor, "start_app", lambda urls, now: started.append(urls))
    supervisor.no_tunnel_since = 0.0
    supervisor.step_app(60.0, check=True)
    assert started == []
    supervisor.step_app(91.0, check=True)
    assert started == [None]
    started.clear()
    for tunnel, host in ((supervisor.tunnel, "a-b"), (supervisor.fixtures, "c-d")):
        tunnel.hostname, tunnel.ready = f"{host}.trycloudflare.com", True
    supervisor.step_app(92.0, check=True)
    assert started == [("https://a-b.trycloudflare.com", "https://c-d.trycloudflare.com")]
    assert supervisor.no_tunnel_since is None


def test_status_follows_the_tunnels_and_the_app(tmp_path: Path) -> None:
    supervisor = supervisor_for(tmp_path)
    supervisor.update_status()
    assert supervisor.status == "starting"
    for tunnel, host in ((supervisor.tunnel, "a-b"), (supervisor.fixtures, "c-d")):
        tunnel.hostname, tunnel.ready = f"{host}.trycloudflare.com", True
    supervisor.app.proc = object()  # type: ignore[assignment]
    supervisor.app.healthy = True
    supervisor.app.urls = None
    supervisor.update_status()
    assert supervisor.status == "degraded"  # serving, but not with the tunnels' URLs yet
    supervisor.app.urls = supervisor.wanted_urls()
    supervisor.update_status()
    assert supervisor.status == "running"
    supervisor.fixtures.ready = False
    supervisor.update_status()
    assert supervisor.status == "degraded"


def test_a_child_whose_record_cannot_be_written_is_stopped_again(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor = supervisor_for(tmp_path)
    supervisor.cfg.logs.mkdir(parents=True)
    marker = f"holdup-test-{secrets.token_hex(4)}"

    def disk_full(*args: object, **kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(tools, "write_json", disk_full)
    argv = [sys.executable, "-c", "import time; time.sleep(60)", marker]
    assert not supervisor.spawn(supervisor.app, argv, env={"PATH": "/usr/bin:/bin"}, cwd=tmp_path, markers=[marker])
    assert supervisor.app.proc is None and supervisor.app.record is None
    assert not [pid for pid in tools.all_pids() if marker in " ".join(tools.proc_cmdline(pid))]
    assert "No space left on device" in supervisor.message and "stopped it again" in supervisor.message
    assert supervisor.app.next_start > time.monotonic()  # tried again after a backoff, not on every tick


def test_a_log_that_cannot_be_written_puts_the_start_off(tmp_path: Path) -> None:
    supervisor = supervisor_for(tmp_path)
    (supervisor.cfg.logs / "app.log").mkdir(parents=True)  # opening it for appending fails
    assert not supervisor.spawn(supervisor.app, [sys.executable, "-c", "pass"], env={}, cwd=tmp_path, markers=["x"])
    assert supervisor.app.proc is None and "cannot write" in supervisor.message
    assert supervisor.app.next_start > time.monotonic()


# Answers every GET with {"hostname": argv[2]} on 127.0.0.1:<argv[1]>, as cloudflared's /quicktunnel does.
HOSTNAME_SNIPPET = (
    "import http.server, json, sys\n"
    "class H(http.server.BaseHTTPRequestHandler):\n"
    "    def do_GET(self):\n"
    "        body = json.dumps({'hostname': sys.argv[2]}).encode()\n"
    "        self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers()\n"
    "        self.wfile.write(body)\n"
    "    def log_message(self, *args): pass\n"
    "http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()\n"
)


def free_test_port() -> int:
    return next(port for port in range(47900, 47960) if tools.can_bind(port) and not tools.listening_sockets(port))


def test_a_hostname_is_only_taken_from_a_socket_our_tunnel_holds(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor = supervisor_for(tmp_path)
    supervisor.cfg.logs.mkdir(parents=True)
    tunnel = supervisor.tunnel
    replaced: list[Any] = []
    monkeypatch.setattr(supervisor, "replace", lambda child, now: replaced.append(child))

    # Someone else's program got the metrics port first, while our cloudflared was still provisioning.
    port = free_test_port()
    supervisor.ports["TUNNEL_METRICS_PORT"] = port
    impostor = subprocess.Popen([sys.executable, "-c", HOSTNAME_SNIPPET, str(port), "evil-host.trycloudflare.com"])
    ours = sleeper(tmp_path, "holdup-test-tunnel", "--metrics")
    try:
        wait_for(lambda: tools.listening_sockets(port), 10, "the impostor to listen")
        tunnel.proc, tunnel.started = ours, time.monotonic()
        tunnel.record = record_for(ours, tmp_path, "tunnel", "holdup-test-tunnel", "--metrics")
        supervisor.step_tunnel(tunnel, time.monotonic(), check=True)
        assert tunnel.hostname is None and replaced == [tunnel]
        assert f"port {port} (TUNNEL_METRICS_PORT) is held by another program" in supervisor.message
    finally:
        for proc in (impostor, ours):
            proc.kill()
            proc.wait()

    # The same answer from the socket our own tunnel process holds is taken.
    port = free_test_port()
    supervisor.ports["TUNNEL_METRICS_PORT"] = port
    ours = subprocess.Popen([sys.executable, "-c", HOSTNAME_SNIPPET, str(port), "good-host.trycloudflare.com",
                             "holdup-test-tunnel"], cwd=tmp_path, start_new_session=True)
    try:
        wait_for(lambda: tools.listening_sockets(port), 10, "our tunnel to listen")
        tunnel.proc, tunnel.started = ours, time.monotonic()
        tunnel.record = record_for(ours, tmp_path, "tunnel", "holdup-test-tunnel")
        supervisor.step_tunnel(tunnel, time.monotonic(), check=True)
        assert tunnel.hostname == "good-host.trycloudflare.com" and len(replaced) == 1
    finally:
        ours.kill()
        ours.wait()


def test_a_stranger_on_the_app_port_cuts_the_tunnels_at_once(tmp_path: Path) -> None:
    supervisor = supervisor_for(tmp_path)
    supervisor.cfg.logs.mkdir(parents=True)
    port = free_test_port()
    supervisor.ports["HOLDUP_PORT"] = port
    stranger = subprocess.Popen([sys.executable, "-c", LISTEN_SNIPPET, str(port)])
    tunnels = [sleeper(tmp_path, "holdup-test-tunnel", name) for name in ("main", "training")]
    try:
        wait_for(lambda: tools.listening_sockets(port), 10, "the stranger to listen")
        for child, proc in zip((supervisor.tunnel, supervisor.fixtures), tunnels, strict=True):
            child.proc = proc
            child.record = record_for(proc, tmp_path, child.name, "holdup-test-tunnel")
        supervisor.guard_app_port(time.monotonic())  # the app is down, so the port should be free
        assert supervisor.tunnel.proc is None and supervisor.fixtures.proc is None
        assert all(proc.poll() is not None for proc in tunnels)
        assert supervisor.ports["HOLDUP_PORT"] != port
        assert tools.read_ports(supervisor.cfg.ports_file)["HOLDUP_PORT"] == supervisor.ports["HOLDUP_PORT"]
        assert f"port {port} (HOLDUP_PORT) is held by another program" in supervisor.message
        assert stranger.poll() is None  # someone else's program is never signalled
    finally:
        for proc in (stranger, *tunnels):
            if proc.poll() is None:
                proc.kill()
            proc.wait()


# ----------------------------------------------------------------------
# scripts/server.sh, run for real as an unprivileged user
# ----------------------------------------------------------------------


def _target_user() -> tuple[int, int] | None:
    if os.geteuid() != 0:
        return os.getuid(), os.getgid()
    try:
        entry = pwd.getpwnam("nobody")
    except KeyError:
        return None
    return entry.pw_uid, entry.pw_gid


TARGET = _target_user()
needs_user = pytest.mark.skipif(TARGET is None, reason="running as root and there is no 'nobody' user")
needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
needs_curl = pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")
needs_linux = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs Linux /proc")


@dataclass
class Box:
    """A temporary home for one server: files, environment and helpers to run server.sh in it."""

    root: Path
    uid: int
    gid: int
    env: dict[str, str]
    api: FakeBotAPI
    outputs: list[str] = field(default_factory=list)
    decoy: subprocess.Popen[bytes] | None = None  # someone else's program listening in the port range
    decoy_port: int = 0

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def work(self) -> Path:
        return self.home / "work"

    @property
    def data(self) -> Path:
        return self.work / "holdup-data"

    @property
    def secrets(self) -> Path:
        return self.work / "secrets"

    @property
    def records(self) -> Path:
        return self.root / "records"

    def as_user(self) -> dict[str, Any]:
        if os.geteuid() == 0:
            return {"user": self.uid, "group": self.gid, "extra_groups": []}
        return {}

    def run(self, *args: str, stdin: str | None = None, timeout: float = 90, check: bool = True,
            env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["bash", str(SCRIPT), *args], env=env or self.env, input=stdin if stdin is not None else "",
            capture_output=True, text=True, timeout=timeout, cwd=self.root, check=False, **self.as_user(),
        )
        self.outputs.append(result.stdout + result.stderr)
        if check:
            assert result.returncode == 0, f"server.sh {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
        return result

    def tmux(self, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["tmux", "-L", self.env["HOLDUP_TMUX_SOCKET"], "-f", "/dev/null", *args],
                              env=self.env, capture_output=True, text=True, timeout=20, check=check,
                              **self.as_user())

    def spawn(self, argv: list[str], cwd: Path) -> subprocess.Popen[bytes]:
        return subprocess.Popen(argv, cwd=cwd, env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True, **self.as_user())

    def state(self) -> dict[str, Any]:
        try:
            return json.loads((self.data / "run" / "state.json").read_text())
        except (OSError, ValueError):
            return {}

    def ports(self) -> dict[str, int | None]:
        return tools.read_ports(self.data / "ports.env")

    def secret_values(self) -> list[str]:
        values = tools.read_env_file(self.secrets / "holdup.env")
        names = ("OTARI_API_KEY", "TELEGRAM_BOT_TOKEN", "HOLDUP_SECRET", "TELEGRAM_WEBHOOK_SECRET")
        found = [values[name] for name in names if values.get(name)]
        assert len(found) == 4
        return found

    def chown(self, path: Path) -> None:
        if os.geteuid() == 0:
            os.chown(path, self.uid, self.gid)


OTHER_BOTS_TOKEN = "1234567890:" + "P" * 35  # e.g. the PDF bot's token, exported in the owner's shell


def make_box(token_for_api: str | None = None) -> Box:
    assert TARGET is not None
    uid, gid = TARGET
    root = Path(tempfile.mkdtemp(prefix="holdup-server-test-"))
    os.chmod(root, 0o755)
    for name in ("home", "bin", "records", "tmux"):
        (root / name).mkdir()
    for name in ("cloudflared", "uv"):
        shutil.copy(FAKES / name, root / "bin" / name)
        os.chmod(root / "bin" / name, 0o755)
    # curl keeps working, but every command line it gets is written down.
    real_curl = shutil.which("curl") or "/usr/bin/curl"
    (root / "bin" / "curl").write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{root}/records/curl-argv.txt"\nexec {real_curl} "$@"\n')
    os.chmod(root / "bin" / "curl", 0o755)
    token = f"{secrets.randbelow(10**9) + 10**9}:{secrets.token_urlsafe(27)}"
    api = FakeBotAPI(token_for_api or token, poll_wait_s=0.3)
    base_url = api.start()
    env = {
        "HOME": str(root / "home"),
        "PATH": f"{root / 'bin'}:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TMUX_TMPDIR": str(root / "tmux"),
        "HOLDUP_TMUX_SOCKET": f"holdup-test-{secrets.token_hex(4)}",
        "HOLDUP_CLOUDFLARED": str(root / "bin" / "cloudflared"),
        "HOLDUP_UV": str(root / "bin" / "uv"),
        "HOLDUP_PORT_MIN": "47960",
        "HOLDUP_PORT_MAX": "47979",
        "HOLDUP_CHECK_INTERVAL": "0.3",
        "HOLDUP_BACKOFF_MIN": "0.5",
        "HOLDUP_BACKOFF_MAX": "2",
        "HOLDUP_PUBLIC_CHECK_INTERVAL": "0",
        "HOLDUP_STOP_TIMEOUT": "10",
        "HOLDUP_START_TIMEOUT": "60",
        "HOLDUP_TELEGRAM_BOT_TOKEN": token,
        "HOLDUP_TELEGRAM_API_BASE_URL": base_url,
        "HOLDUP_OTARI_API_KEY": f"otk_v1_us_{secrets.token_hex(20)}",
        "HOLDUP_OTARI_MODE": "off",  # the fake key never reaches a gateway
        # What another bot on the same account may export under the plain names: never used.
        "TELEGRAM_BOT_TOKEN": OTHER_BOTS_TOKEN,
        "OTARI_API_KEY": "otk_v1_us_someone_elses_key_0000",
    }
    box = Box(root, uid, gid, env, api)
    for path in (root / "home", root / "records", root / "tmux"):
        box.chown(path)
    os.chmod(root / "tmux", 0o700)
    return box


def destroy_box(box: Box) -> None:
    for step in (lambda: box.run("stop", check=False, timeout=60) if (box.data / "run").is_dir() else None,
                 lambda: box.tmux("kill-server")):  # this box's own tmux server only
        try:
            step()
        except (OSError, subprocess.SubprocessError):
            pass
    # A safety net if a test failed half-way: whatever still runs from this box names its
    # directory on its command line (the fakes live there), and so do the groups they lead.
    leftovers = [pid for pid in tools.all_pids() if str(box.root) in " ".join(tools.proc_cmdline(pid))]
    groups = {info.pgid for pid in leftovers if (info := tools.proc_info(pid))} - {os.getpgrp()}
    for pid in tools.all_pids():
        info = tools.proc_info(pid)
        if pid != os.getpid() and (pid in leftovers or (info is not None and info.pgid in groups)):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    box.api.stop()
    shutil.rmtree(box.root, ignore_errors=True)


def user_can_use_repo() -> bool:
    if TARGET is None:
        return False
    if os.geteuid() != 0:
        return True
    check = subprocess.run(["test", "-r", str(SCRIPT), "-a", "-x", str(REPO / ".venv" / "bin" / "python")],
                           user=TARGET[0], group=TARGET[1], extra_groups=[], check=False)
    return check.returncode == 0


needs_access = pytest.mark.skipif(not user_can_use_repo(), reason="the test user cannot read this checkout")
runner = [needs_linux, needs_user, needs_tmux, needs_curl, needs_access]


def apply(marks: list[pytest.MarkDecorator]) -> Callable[[Any], Any]:
    def decorate(item: Any) -> Any:
        for mark in marks:
            item = mark(item)
        return item

    return decorate


@apply(runner)
def test_refuses_to_run_as_root() -> None:
    if os.geteuid() != 0:
        pytest.skip("only meaningful when the suite runs as root")
    result = subprocess.run(["bash", str(SCRIPT), "status"], capture_output=True, text=True, timeout=30,
                            env={"HOME": "/root", "PATH": "/usr/bin:/bin"}, check=False)
    assert result.returncode != 0
    assert "do not run this as root" in result.stderr


@apply(runner)
def test_refuses_a_holdup_home_owned_by_someone_else() -> None:
    box = make_box()
    try:
        env = {**box.env, "HOLDUP_HOME": "/usr"}  # owned by root, never by the test user
        result = box.run("setup", env=env, check=False)
        assert result.returncode != 0
        assert "belongs to another user" in result.stderr
    finally:
        destroy_box(box)


@apply(runner)
def test_setup_refuses_a_token_telegram_rejects_and_saves_nothing() -> None:
    box = make_box(token_for_api="999999999:" + "x" * 35)  # the fake accepts only another token
    try:
        result = box.run("setup", check=False)
        assert result.returncode != 0
        assert "Telegram rejected this bot token" in result.stderr
        assert not (box.secrets / "holdup.env").exists()
        assert box.env["HOLDUP_TELEGRAM_BOT_TOKEN"] not in result.stdout + result.stderr
    finally:
        destroy_box(box)


@apply(runner)
def test_setup_never_takes_another_bots_token_from_the_environment() -> None:
    box = make_box()
    try:
        env = {k: v for k, v in box.env.items() if k != "HOLDUP_TELEGRAM_BOT_TOKEN"}  # only the plain name is set
        result = box.run("setup", env=env, check=False)
        assert result.returncode != 0 and "HOLDUP_TELEGRAM_BOT_TOKEN" in result.stderr
        assert "TELEGRAM_BOT_TOKEN in the environment is not used" in result.stdout
        assert not (box.secrets / "holdup.env").exists()
        assert box.api.calls == [] and box.api.rejected == 0  # the other bot's token was never sent anywhere
        assert OTHER_BOTS_TOKEN not in result.stdout + result.stderr
    finally:
        destroy_box(box)


@apply(runner)
def test_holdup_has_a_tmux_server_of_its_own_by_default() -> None:
    box = make_box()
    try:
        env = {k: v for k, v in box.env.items() if k != "HOLDUP_TMUX_SOCKET"}  # TMUX_TMPDIR stays in the box
        box.run("setup", env=env)
        status = box.run("status", env=env).stdout
        assert "no tmux session 'holdup' (tmux -L holdup)" in status
    finally:
        destroy_box(box)


@apply(runner)
def test_testnet_step_and_backup_with_a_simulated_network() -> None:
    box = make_box()
    try:
        box.run("setup")
        creds = box.secrets / "holdup-credentials.json"

        refused = box.run("testnet", stdin="no\n", check=False)
        assert refused.returncode != 0 and "not confirmed" in refused.stderr
        assert "no real money" in refused.stdout
        assert not creds.exists()

        done = box.run("testnet", stdin="yes\n\n")
        data = json.loads(creds.read_text())
        assert stat.S_IMODE(creds.stat().st_mode) == 0o600
        root = data["root_account"]
        assert tools.ACCOUNT_NAME.match(root) and root.startswith("holdup-")
        assert data["vault_account"] == f"vault.{root}"
        assert data["secret"] == tools.read_env_file(box.secrets / "holdup.env")["HOLDUP_SECRET"]
        for step in ("1/5", "2/5", "3/5", "4/5", "5/5"):
            assert step in done.stdout

        calls = [json.loads(p.read_text()) for p in sorted(box.records.glob("uv-*.json"))]
        ran = [" ".join(c["argv"]) for c in calls]
        assert any("testnet_tools.py bootstrap " + root in line for line in ran)
        assert any(f"holdup deploy --network testnet --profile hosted --root {root}" in line for line in ran)
        assert any(f"testnet_tools.py topup {root} --min 5" in line for line in ran)
        deploy = next(c for c in calls if "deploy" in c["argv"])
        assert "HOLDUP_SECRET" in deploy["env_keys"] and "HOLDUP_CREDENTIALS" in deploy["env_keys"]
        for name in ("TELEGRAM_BOT_TOKEN", "OTARI_API_KEY", "HOLDUP_TELEGRAM_BOT_TOKEN", "HOLDUP_OTARI_API_KEY"):
            assert name not in deploy["env_keys"]

        status = box.run("status").stdout
        assert "Testnet accounts" in status and f"{root}: 5 NEAR" in status
        topup = box.run("topup", "7").stdout
        assert f"{root}: 5.0000 NEAR, target 7 NEAR (simulated)" in topup
        assert box.run("topup", "lots", check=False).returncode != 0

        again = box.run("testnet", stdin="yes\n\n", check=False)
        assert again.returncode != 0 and "--force-new" in again.stderr
        assert json.loads(creds.read_text())["root_account"] == root

        box.run("testnet", "--force-new", stdin="yes\nmy-second-root.testnet\n")
        assert json.loads(creds.read_text())["root_account"] == "my-second-root.testnet"
        old = list(box.secrets.glob("holdup-credentials.json.*.old"))
        assert len(old) == 1 and json.loads(old[0].read_text())["root_account"] == root

        check_backup(box)
        for output in box.outputs:
            for value in box.secret_values():
                assert value not in output
    finally:
        destroy_box(box)


@apply(runner)
def test_force_new_also_starts_over_from_an_unfinished_setup() -> None:
    box = make_box()
    try:
        box.run("setup")
        creds = box.secrets / "holdup-credentials.json"
        unfinished = {"network": "testnet", "root_account": "holdup-old111.testnet",
                      "treasury_private_key": "ed25519:fake-old-key"}  # bootstrap ran, deploy did not finish
        creds.write_text(json.dumps(unfinished))
        box.chown(creds)
        box.run("testnet", "--force-new", stdin="yes\nmy-fresh-root.testnet\n")
        data = json.loads(creds.read_text())
        assert data["root_account"] == "my-fresh-root.testnet" and data["vault_account"] == "vault.my-fresh-root.testnet"
        old = list(box.secrets.glob("holdup-credentials.json.*.old"))
        assert len(old) == 1 and json.loads(old[0].read_text()) == unfinished
    finally:
        destroy_box(box)


def check_backup(box: Box) -> None:
    """`backup` archives HoldUp's secrets files and a consistent copy of the database, mode 600."""
    db = box.data / "holdup.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("create table t (x)")
        conn.execute("insert into t values (42)")
    box.chown(db)
    result = box.run("backup")
    archives = list((box.work / "backups").glob("holdup-*.tar.gz"))
    assert len(archives) == 1 and str(archives[0]) in result.stdout
    assert stat.S_IMODE(archives[0].stat().st_mode) == 0o600
    with tarfile.open(archives[0]) as tar:
        names = set(tar.getnames())
        assert {"./secrets/holdup.env", "./secrets/holdup-credentials.json", "./holdup.sqlite3"} <= names
        assert any(name.endswith(".old") for name in names)  # the credentials --force-new moved aside
        extracted = Path(tempfile.mkdtemp(dir=box.root))
        tar.extract("./holdup.sqlite3", extracted, filter="data")
    with sqlite3.connect(extracted / "holdup.sqlite3") as conn:
        assert conn.execute("select x from t").fetchone() == (42,)


# ----------------------------------------------------------------------
# the running service, in an isolated tmux server
# ----------------------------------------------------------------------


# A program that listens on 127.0.0.1:<argv[1]> the way servers do (SO_REUSEADDR), then waits.
LISTEN_SNIPPET = (
    "import socket, sys, time; s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); "
    "s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen(); time.sleep(600)"
)


@pytest.fixture(scope="module")
def server() -> Iterator[Box]:
    if TARGET is None or shutil.which("tmux") is None or shutil.which("curl") is None or not user_can_use_repo():
        pytest.skip("needs tmux, curl, Linux and a user that can read this checkout")
    box = make_box()
    try:
        # Neighbours that must survive: sessions whose names start like ours, and a program
        # listening in the port range whose command line even looks like ours.
        for session in ("pdfbot", "holdup2"):
            box.tmux("new-session", "-d", "-s", session, "sleep", "600", check=True)
        # As if the owner's ~/.tmux.conf kept dead windows open: HoldUp's session must still end.
        box.tmux("set-option", "-g", "remain-on-exit", "on", check=True)
        listen = LISTEN_SNIPPET
        decoy_port = next(p for p in range(47960, 47980) if tools.can_bind(p))
        decoy = box.spawn([sys.executable, "-c", listen, str(decoy_port), "holdup", "serve", "tunnel", "--metrics",
                           f"127.0.0.1:{decoy_port}"], cwd=REPO)
        wait_for(lambda: tools.Listener("127.0.0.1", decoy_port) in tools.listeners(), 10, "the decoy listener")
        box.decoy, box.decoy_port = decoy, decoy_port
        box.run("install", "--offline", timeout=120)
        yield box
    finally:
        destroy_box(box)
        if box.decoy is not None:
            box.decoy.kill()
            box.decoy.wait()


def our_pids(box: Box) -> set[int]:
    """The supervisor and every process below it."""
    supervisor = box.state().get("supervisor_pid")
    if not supervisor:
        return set()
    parents = {}
    for pid in tools.all_pids():
        info = tools.proc_info(pid)
        if info is not None:
            parents[pid] = info.ppid
    found, frontier = {supervisor}, [supervisor]
    while frontier:
        parent = frontier.pop()
        for pid, ppid in parents.items():
            if ppid == parent and pid not in found:
                found.add(pid)
                frontier.append(pid)
    return found


def listening_sockets_of(pids: set[int]) -> list[tuple[str, int]]:
    inodes: set[str] = set()
    for pid in pids:
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
                if target.startswith("socket:["):
                    inodes.add(target[8:-1])
        except OSError:
            continue
    found = []
    for path, ipv6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
        if not Path(path).exists():  # no IPv6 on this machine
            continue
        for line in Path(path).read_text().splitlines()[1:]:
            parts = line.split()
            if parts[3] == "0A" and parts[9] in inodes:
                address, _, port = parts[1].partition(":")
                host = tools._hex_ipv6(address) if ipv6 else tools._hex_ipv4(address)
                found.append((host, int(port, 16)))
    return found


@apply(runner)
class TestRunningServer:
    def test_setup_wrote_private_files_and_ports_in_the_range(self, server: Box) -> None:
        mode = lambda path: stat.S_IMODE(path.stat().st_mode)
        assert mode(server.secrets) == 0o700
        assert mode(server.secrets / "holdup.env") == 0o600
        for path in (server.data, server.data / "logs", server.data / "run"):
            assert mode(path) == 0o700
        for path in (server.data / "ports.env", *(server.data / "logs").iterdir()):
            assert mode(path) == 0o600
        settings = tools.read_env_file(server.secrets / "holdup.env")
        assert len(settings["HOLDUP_SECRET"]) == 64 and len(settings["TELEGRAM_WEBHOOK_SECRET"]) == 64
        assert settings["TELEGRAM_MODE"] == "polling"
        ports = list(server.ports().values())
        assert len(set(ports)) == 3 and all(47960 <= p <= 47979 for p in ports)  # type: ignore[operator]
        assert server.decoy_port not in ports

    def test_public_urls_are_discovered_and_health_answers_on_loopback(self, server: Box) -> None:
        state = server.state()
        assert state["status"] == "running"
        assert tools.TUNNEL_HOST.match(state["public_url"].removeprefix("https://"))
        assert tools.TUNNEL_HOST.match(state["fixture_url"].removeprefix("https://"))
        assert state["public_url"] != state["fixture_url"]
        status, health = tools.get_json(f"http://127.0.0.1:{server.ports()['HOLDUP_PORT']}/health", 5)
        assert status == 200 and health["ok"] and health["network"] == "offline"
        assert health["public_base_url"] == state["public_url"]
        telegram = wait_for(lambda: tools.get_json(
            f"http://127.0.0.1:{server.ports()['HOLDUP_PORT']}/health", 5)[1]["telegram"], 10, "Telegram status")
        assert telegram["mode"] == "polling"
        assert server.run("url").stdout.strip() == state["public_url"]

    def test_the_training_pages_have_their_own_host(self, server: Box) -> None:
        state = server.state()
        port = server.ports()["HOLDUP_PORT"]
        main_host = state["public_url"].removeprefix("https://")
        fixture_host = state["fixture_url"].removeprefix("https://")
        redirect = subprocess.run(["curl", "-s", "--noproxy", "*", "-o", "/dev/null", "-w", "%{http_code} %{redirect_url}",
                                   "-H", f"Host: {main_host}", f"http://127.0.0.1:{port}/fixtures/coinhaven/verify"],
                                  capture_output=True, text=True, timeout=10, check=False)
        assert redirect.stdout == f"302 https://{fixture_host}/fixtures/coinhaven/verify"
        other = subprocess.run(["curl", "-s", "--noproxy", "*", "-o", "/dev/null", "-w", "%{http_code}",
                                "-H", f"Host: {fixture_host}",
                                f"http://127.0.0.1:{port}/health"], capture_output=True, text=True, timeout=10,
                               check=False)
        assert other.stdout == "404"

        def mcp_status(host: str) -> str:
            return subprocess.run(
                ["curl", "-s", "--noproxy", "*", "-o", "/dev/null", "-w", "%{http_code}", "-X", "POST",
                 "-H", f"Host: {host}", "-H", "Content-Type: application/json", "-H", "Accept: application/json",
                 "--data", "{}", f"http://127.0.0.1:{port}/mcp"],
                capture_output=True, text=True, timeout=10, check=False).stdout

        assert mcp_status(main_host) not in ("421", "404", "000")  # Otari's calls to /mcp get through
        assert mcp_status(fixture_host) == "404"  # the training host serves nothing else

    def test_only_loopback_listeners_in_the_range(self, server: Box) -> None:
        sockets = listening_sockets_of(our_pids(server))
        assert sorted(port for _, port in sockets) == sorted(server.ports().values())  # type: ignore[type-var]
        assert all(host == "127.0.0.1" for host, _ in sockets)

    def test_cloudflared_is_isolated_and_gets_no_secrets(self, server: Box) -> None:
        starts = [json.loads(p.read_text()) for p in server.records.glob("cloudflared-*.json")]
        assert len(starts) >= 2
        for start in starts:
            argv = start["argv"]
            assert argv[0] == "tunnel" and "--no-autoupdate" in argv
            assert argv[argv.index("--protocol") + 1] == "http2"
            assert argv[argv.index("--loglevel") + 1] == "info"
            assert argv[argv.index("--metrics") + 1].startswith("127.0.0.1:")
            assert Path(argv[argv.index("--config") + 1]).is_relative_to(server.data)
            assert start["env"]["HOME"] == str(server.data / "cloudflared")
            assert not any(key.startswith(("TELEGRAM", "OTARI", "HOLDUP_SECRET", "TUNNEL_")) for key in start["env"])

    def test_no_secret_in_logs_outputs_or_the_process_list(self, server: Box) -> None:
        values = server.secret_values()
        texts = list(server.outputs)
        texts += [p.read_text(errors="replace") for p in server.data.rglob("*") if p.is_file()]
        texts += [p.read_text(errors="replace") for p in server.records.iterdir() if p.is_file()]
        texts.append(server.tmux("capture-pane", "-p", "-S", "-", "-t", "=holdup:=run").stdout)
        texts += [" ".join(tools.proc_cmdline(pid)) for pid in tools.all_pids()]
        assert (server.data / "logs" / "app.log").read_text()  # the app did write its log
        for text in texts:
            for value in values:
                assert value not in text
        app_python = server.state()["app"]["python_pid"]
        environ = Path(f"/proc/{app_python}/environ").read_bytes()
        assert server.env["HOLDUP_TELEGRAM_BOT_TOKEN"].encode() in environ  # the app itself does get its settings
        settings = (server.secrets / "holdup.env").read_text()
        assert OTHER_BOTS_TOKEN not in settings and "someone_elses_key" not in settings
        assert server.api.rejected == 0  # the other bot's token never reached the Bot API

    def test_status_and_logs_commands(self, server: Box) -> None:
        status = server.run("status").stdout
        state = server.state()
        assert "tmux session 'holdup' runs HoldUp's supervisor" in status
        option = server.tmux("show-options", "-w", "-t", "=holdup:=run", "remain-on-exit").stdout
        assert option.strip() == "remain-on-exit off"  # despite the global "on" set by the fixture
        assert state["public_url"] in status and state["fixture_url"] in status
        assert "Health       ok; network offline; Telegram polling" in status
        assert f"127.0.0.1:{server.ports()['HOLDUP_PORT']}" in status
        app_log = server.run("logs", "app").stdout
        assert "HoldUp listening on http://127.0.0.1:" in app_log
        assert '"GET /health HTTP/1.1" 200' not in app_log  # the supervisor's checks every few seconds are left out
        assert "is running at" in server.run("logs", "supervisor").stdout
        assert "trycloudflare.com" in server.run("logs", "tunnel").stdout
        assert server.run("logs", "nonsense", check=False).returncode != 0

    def test_the_bot_answers_a_user_and_links_to_the_public_url(self, server: Box) -> None:
        api, url = server.api, server.state()["public_url"]
        api.send_text("/start")
        wait_for(lambda: any(text.startswith("Welcome to HoldUp") for text in api.replies()), 30, "the welcome")
        api.send_text("send 5 HUSD to ravi.testnet")  # a payee of the in-memory world
        confirm = wait_for(lambda: [data for label, data in api.buttons() if label == "Confirm"], 30, "Confirm")
        api.press(confirm[-1])
        held = wait_for(lambda: [t for t in api.replies() if t.startswith("Payment held")], 30, "the held payment")
        assert f"Details: {url}/h/" in held[0]  # the bot links to the tunnel's public address

    def test_start_and_install_again_change_nothing(self, server: Box) -> None:
        before = server.state()
        env_text = (server.secrets / "holdup.env").read_text()
        ports = server.ports()
        refused = server.run("start", "--offline", check=False)
        assert refused.returncode != 0 and "already running" in refused.stderr
        again = server.run("install", "--offline")
        assert "already running" in again.stdout
        assert (server.secrets / "holdup.env").read_text() == env_text
        assert server.ports() == ports
        after = server.state()
        assert after["supervisor_pid"] == before["supervisor_pid"]
        assert after["public_url"] == before["public_url"]
        assert after["app"]["pid"] == before["app"]["pid"]

    def test_a_second_supervisor_refuses_and_touches_nothing(self, server: Box) -> None:
        before = server.state()
        record = (server.data / "run" / "supervisor.pid.json").read_text()
        second = server.run("run", check=False, timeout=30)
        assert second.returncode == 1
        assert "another HoldUp supervisor is already running" in second.stdout
        assert (server.data / "run" / "supervisor.pid.json").read_text() == record
        after = server.state()
        assert after["status"] == "running" and after["supervisor_pid"] == before["supervisor_pid"]

    def test_a_killed_tunnel_is_replaced_and_the_app_gets_the_new_url(self, server: Box) -> None:
        before = server.state()
        old_url, old_app = before["public_url"], before["app"]["pid"]
        os.kill(before["tunnel"]["pid"], signal.SIGKILL)  # a tunnel this test's server started
        after = wait_for(lambda: (s := server.state()).get("status") == "running" and s.get("public_url") != old_url
                         and s, 30, "a new tunnel and a restarted app")
        assert after["fixture_url"] == before["fixture_url"]  # the training tunnel was left alone
        assert after["app"]["pid"] != old_app
        assert after["tunnel"]["restarts"] == 1
        status, health = tools.get_json(f"http://127.0.0.1:{server.ports()['HOLDUP_PORT']}/health", 5)
        assert status == 200 and health["public_base_url"] == after["public_url"]
        log = (server.data / "logs" / "supervisor.log").read_text()
        assert "tunnel exited" in log and "public URLs changed" in log

    def test_a_crashed_app_is_restarted_with_the_same_url(self, server: Box) -> None:
        before = server.state()
        os.kill(before["app"]["python_pid"], signal.SIGKILL)
        after = wait_for(lambda: (s := server.state()).get("status") == "running"
                         and s["app"]["pid"] != before["app"]["pid"] and s, 30, "the app to come back")
        assert after["public_url"] == before["public_url"]  # the tunnels kept their hostnames
        assert after["tunnel"]["pid"] == before["tunnel"]["pid"]

    def test_stop_leaves_nothing_behind_and_spares_the_neighbours(self, server: Box) -> None:
        pids = our_pids(server) - {os.getpid()}
        ports = list(server.ports().values())
        # The owner opened a window of their own in HoldUp's session, and left the supervisor's
        # pane scrolled back in copy mode, where tmux would swallow a typed Ctrl-c.
        owner = server.tmux("new-window", "-d", "-P", "-F", "#{pane_pid}", "-t", "=holdup:", "-n", "logs",
                            "sleep", "600", check=True).stdout.strip()
        server.tmux("copy-mode", "-t", "=holdup:=run", check=True)
        started = time.monotonic()
        server.run("stop")
        assert time.monotonic() - started < 20  # the supervisor itself was asked, not the pane
        log = (server.data / "logs" / "supervisor.log").read_text()
        assert "stopping: app and tunnels" in log and "did not stop in time" not in log
        windows = server.tmux("list-windows", "-t", "=holdup", "-F", "#{window_name}").stdout.split()
        assert windows == ["logs"]  # the supervisor's window closed; the owner's stays
        assert tools.proc_info(int(owner)) is not None and tools.proc_info(int(owner)).state != "Z"
        for session in ("pdfbot", "holdup2"):
            assert server.tmux("has-session", "-t", f"={session}").returncode == 0
        wait_for(lambda: not [p for p in pids if tools.proc_info(p) and tools.proc_info(p).state != "Z"], 10,
                 "every process of the service to end")
        for port in ports:
            assert tools.can_bind(port)  # type: ignore[arg-type]
        assert server.decoy is not None and server.decoy.poll() is None
        assert tools.Listener("127.0.0.1", server.decoy_port) in tools.listeners()
        assert not list((server.data / "run").glob("*.pid.json"))
        assert server.state()["status"] == "stopped"
        assert server.run("url", check=False).returncode != 0

    def test_a_port_taken_while_stopped_is_replaced_at_the_next_start(self, server: Box) -> None:
        server.run("stop")
        old = server.ports()
        listen = LISTEN_SNIPPET
        squatter = server.spawn([sys.executable, "-c", listen, str(old["HOLDUP_PORT"])], cwd=server.root)
        # The secrets were copied back with the wrong modes meanwhile; start makes them private again.
        os.chmod(server.secrets / "holdup.env", 0o644)
        os.chmod(server.secrets, 0o755)
        try:
            wait_for(lambda: not tools.can_bind(old["HOLDUP_PORT"]), 10, "the squatter to listen")
            started = server.run("start", "--offline", timeout=90)
            assert "set " + str(server.secrets / "holdup.env") + " to mode 600" in started.stdout
            assert stat.S_IMODE((server.secrets / "holdup.env").stat().st_mode) == 0o600
            assert stat.S_IMODE(server.secrets.stat().st_mode) == 0o700
            new = server.ports()
            assert new["HOLDUP_PORT"] != old["HOLDUP_PORT"]
            assert server.state()["ports"]["HOLDUP_PORT"] == new["HOLDUP_PORT"]
            # HoldUp's window was added to its own session, next to the owner's window.
            windows = server.tmux("list-windows", "-t", "=holdup", "-F", "#{window_name}").stdout.split()
            assert sorted(windows) == ["logs", "run"]
            server.run("stop")
            assert squatter.poll() is None
        finally:
            squatter.kill()
            squatter.wait()

    def test_a_holdup_session_this_script_did_not_start_is_left_alone(self, server: Box) -> None:
        server.tmux("kill-session", "-t", "=holdup")  # what the earlier tests left: the owner's window only
        try:
            # Someone's own session that happens to have HoldUp's names, with a program in it.
            server.tmux("new-session", "-d", "-s", "holdup", "-n", "run", "sleep", "600", check=True)
            pane = int(server.tmux("list-panes", "-t", "=holdup:=run", "-F", "#{pane_pid}").stdout)
            status = server.run("status").stdout
            assert "exists, but HoldUp's supervisor does not run in it" in status
            refused = server.run("start", "--offline", check=False)
            assert refused.returncode != 0 and "did not start" in refused.stderr and "rename-session" in refused.stderr
            started = time.monotonic()
            server.run("stop")
            assert time.monotonic() - started < 10
            assert server.tmux("has-session", "-t", "=holdup").returncode == 0
            assert tools.proc_info(pane) is not None and tools.proc_info(pane).state != "Z"
            assert not (server.data / "run" / "supervisor.pid.json").exists()
        finally:
            server.tmux("kill-session", "-t", "=holdup")
