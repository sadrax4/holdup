#!/usr/bin/env python3
"""Helpers for scripts/server.sh, and the supervisor that keeps HoldUp running on one server.

Standard library only: this runs with the system python3 (3.12 or newer), before
`uv sync` has built the project's environment. Nothing here prints a secret.

    supervise                       the supervisor (`server.sh run` starts it inside tmux)
    cleanup RUN_DIR                 stop processes recorded in RUN_DIR that are still ours
    ports FILE                      choose free ports in the allowed range and remember them
    ports-busy FILE                 list remembered ports that something still listens on
    lock-free FILE                  exit 0 when no supervisor holds this lock
    supervisor-pid RUN_DIR          print the pid of the recorded supervisor, if it still runs
    secure-secrets DIR              make HoldUp's secrets files mode 600 and their folder 700
    env-merge FILE                  add KEY=value lines from stdin to an env file (never replaces)
    env-get FILE KEY                print one value of an env file
    env-check FILE                  exit 0 when an env file can be read
    secret                          print a new random secret (64 hex digits)
    root-name                       print a default testnet root account name
    check-root-name NAME            exit 0 when NAME is a top-level .testnet account name
    credentials FILE [FIELD]        what a credentials file holds, without its keys
    credentials-secret FILE         its HOLDUP_SECRET, for capture into a shell variable
    telegram-username               read a getMe answer on stdin, print the bot's username
    state FILE [KEY]                the supervisor's state, or one field of it
    status RUN_DIR PORTS_FILE       a human summary of the running service
    health                          one line about a /health answer read from stdin

The supervisor starts two Cloudflare quick tunnels (the bot's host and a second host
for the training pages) and `holdup serve`, each in its own session, and watches them.
See docs/SERVER.md for the rules it follows.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import random
import re
import secrets
import shutil
import signal
import socket
import stat
import string
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent

# The owner's rule: everything listens on 127.0.0.1, on ports from this range only.
PORT_RANGE = (47800, 47999)
PORT_KEYS = ("HOLDUP_PORT", "TUNNEL_METRICS_PORT", "FIXTURE_METRICS_PORT")
LOOPBACK = "127.0.0.1"

TUNNEL_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.trycloudflare\.com$")
ACCOUNT_NAME = re.compile(r"^[a-z0-9]+([_-][a-z0-9]+)*\.testnet$")
# Account ids have at most 64 characters, and the deployment creates names such as
# northwind-paper.<root>: a longer root would fail half-way through it.
ROOT_NAME_MAX = 48
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
FORBIDDEN_IN_VALUES = ("'", '"', "\n", "\r", "\x00")

# Credentials fields that may be shown; the private keys and the secret never are.
PUBLIC_CREDENTIAL_FIELDS = ("network", "root_account", "vault_account", "keeper_account", "oracle_accounts",
                            "husd_token", "rpc_url")
DEPLOYED_FIELDS = ("vault_account", "keeper_account", "oracle_accounts", "husd_token",
                   "treasury_private_key", "oracle_private_key", "keeper_private_key")


class ToolError(Exception):
    """A problem to report in one line, without a traceback."""


class AlreadyRunning(ToolError):
    """Another supervisor holds the lock; its files are left exactly as they are."""


# ----------------------------------------------------------------------
# small file helpers
# ----------------------------------------------------------------------


def write_private(path: Path, text: str) -> None:
    """Replace ``path`` atomically with a mode 600 file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_json(path: Path, data: Any) -> None:
    write_private(path, json.dumps(data, indent=1, sort_keys=True) + "\n")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def utc_stamp(when: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(time.time() if when is None else when))


# ----------------------------------------------------------------------
# env files: KEY='value' lines, never sourced
# ----------------------------------------------------------------------


def parse_env(text: str) -> dict[str, str]:
    """The settings in an env file. Values may be single- or double-quoted; nothing is expanded.

    A comment may follow a value after a space (``KEY='value'  # note``). A malformed line
    raises :class:`ToolError` naming its line number only, because the line itself may hold
    a secret.
    """
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = ENV_LINE.match(line)
        if not match:
            raise ToolError(f"line {number} is not KEY=value")
        values[match.group(1)] = _env_value(match.group(2).strip(), number)
    return values


def _env_value(raw: str, number: int) -> str:
    if raw[:1] not in ("'", '"'):
        return raw.split(" #", 1)[0].strip()
    end = raw.find(raw[0], 1)
    rest = raw[end + 1:] if end > 0 else ""
    # After the closing quote only a comment may follow, set off by a space.
    if end < 0 or (rest.strip() and not (rest[0].isspace() and rest.strip().startswith("#"))):
        raise ToolError(f"line {number}: the quoted value does not end with its quote (or text follows it)")
    return raw[1:end]


def check_value(key: str, value: str) -> None:
    if not ENV_KEY.match(key):
        raise ToolError(f"{key!r} is not a valid setting name")
    if any(char in value for char in FORBIDDEN_IN_VALUES):
        raise ToolError(f"the value of {key} contains a quote or a line break; refusing to write it")


def format_env(values: Mapping[str, str], header: Sequence[str] = ()) -> str:
    lines = [f"# {line}".rstrip() for line in header]
    for key, value in values.items():
        check_value(key, value)
        lines.append(f"{key}='{value}'")
    return "\n".join(lines) + "\n"


ENV_HEADER = (
    "HoldUp settings and secrets, written by scripts/server.sh setup. Keep this file at mode 600.",
    "One KEY='value' per line. scripts/server.sh never overwrites a value that is already here;",
    "edit this file to change one, then run: bash scripts/server.sh restart",
)


def read_env_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        return parse_env(text)
    except ToolError as exc:
        raise ToolError(f"{path}: {exc}") from None


def merge_env(path: Path, additions: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """Add settings to ``path`` that it does not have yet (or has empty). Returns (added, kept)."""
    existing = read_env_file(path)
    added, kept = [], []
    merged = dict(existing)
    for key, value in additions.items():
        check_value(key, value)
        if existing.get(key):
            kept.append(key)
        elif value:
            merged[key] = value
            added.append(key)
    if added or not path.exists():
        header = ENV_HEADER if not path.exists() else _header_lines(path)
        write_private(path, format_env(merged, header))
    else:
        os.chmod(path, 0o600)
    return added, kept


def _header_lines(path: Path) -> list[str]:
    """The comment block at the top of an existing env file, kept when the file is rewritten."""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("#"):
            break
        lines.append(line[1:].strip())
    return lines or list(ENV_HEADER)


SECRET_FILES = ("holdup.env", "holdup-credentials.json", "holdup-credentials.json.*.old")


def secure_secrets(secrets_dir: Path) -> list[str]:
    """Make HoldUp's secrets private again: the folder mode 700, its files mode 600.

    Only HoldUp's own files are touched (the folder may hold other secrets of the
    owner). Returns what was changed; a folder or file of another user is refused.
    """
    uid, changed = os.getuid(), []
    try:
        info = secrets_dir.stat()
    except FileNotFoundError:
        raise ToolError(f"{secrets_dir} does not exist; run: bash scripts/server.sh setup") from None
    if info.st_uid != uid:
        raise ToolError(f"{secrets_dir} belongs to another user; refusing to use it")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(secrets_dir, 0o700)
        changed.append(f"set {secrets_dir} to mode 700 (it was {stat.S_IMODE(info.st_mode):o})")
    for pattern in SECRET_FILES:
        for path in sorted(secrets_dir.glob(pattern)):
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                continue
            if info.st_uid != uid:
                raise ToolError(f"{path} belongs to another user; refusing to use it")
            if stat.S_IMODE(info.st_mode) != 0o600:
                os.chmod(path, 0o600)
                changed.append(f"set {path} to mode 600 (it was {stat.S_IMODE(info.st_mode):o})")
    return changed


def read_stdin_pairs(stream: Iterable[str]) -> dict[str, str]:
    """``KEY=value`` lines (value taken literally, up to the end of the line)."""
    pairs: dict[str, str] = {}
    for line in stream:
        line = line.rstrip("\n")
        if not line:
            continue
        key, sep, value = line.partition("=")
        if not sep or not ENV_KEY.match(key):
            raise ToolError("expected KEY=value lines on standard input")
        pairs[key] = value
    return pairs


# ----------------------------------------------------------------------
# listening sockets and ports
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Listener:
    address: str
    port: int


def _hex_ipv4(text: str) -> str:
    raw = bytes.fromhex(text)
    return socket.inet_ntop(socket.AF_INET, raw[::-1])


def _hex_ipv6(text: str) -> str:
    raw = bytes.fromhex(text)
    words = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
    return socket.inet_ntop(socket.AF_INET6, words)


@dataclass(frozen=True)
class Socket:
    """A listening TCP socket as /proc/net/tcp shows it: who owns it, and its inode."""

    address: str
    port: int
    uid: int  # -1 when the line has no such column
    inode: str


def parse_proc_sockets(text: str, ipv6: bool) -> list[Socket]:
    """TCP sockets in the LISTEN state (``st`` 0A) from /proc/net/tcp or /proc/net/tcp6."""
    found = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4 or parts[3] != "0A":
            continue
        address, _, port = parts[1].partition(":")
        try:
            host = _hex_ipv6(address) if ipv6 else _hex_ipv4(address)
            uid, inode = (int(parts[7]), parts[9]) if len(parts) > 9 else (-1, "")
            found.append(Socket(host, int(port, 16), uid, inode))
        except (ValueError, OSError):
            continue
    return found


def parse_proc_net(text: str, ipv6: bool) -> list[Listener]:
    return [Listener(item.address, item.port) for item in parse_proc_sockets(text, ipv6)]


def listening_sockets(port: int) -> list[Socket]:
    """Every TCP socket listening on ``port``, on any address, from /proc/net/tcp and tcp6."""
    found = []
    for path, ipv6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
        with contextlib.suppress(OSError):
            found += [item for item in parse_proc_sockets(Path(path).read_text(), ipv6) if item.port == port]
    return found


def socket_inodes(pids: Iterable[int]) -> set[str]:
    """The inodes of the sockets these processes have open (only our own processes can be read)."""
    inodes = set()
    for pid in pids:
        try:
            names = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for name in names:
            with contextlib.suppress(OSError):
                target = os.readlink(f"/proc/{pid}/fd/{name}")
                if target.startswith("socket:[") and target.endswith("]"):
                    inodes.add(target[8:-1])
    return inodes


def parse_ss(text: str) -> list[Listener]:
    """Listening sockets from ``ss -Htln`` (State Recv-Q Send-Q Local:Port Peer:Port ...)."""
    found = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        host, sep, port = parts[3].rpartition(":")
        if not sep or not port.isdigit():
            continue
        host = host.strip("[]").split("%", 1)[0]
        found.append(Listener(host or "*", int(port)))
    return found


def listeners() -> list[Listener]:
    """Every TCP socket listening on this machine: ``ss`` when it is installed, and /proc/net."""
    found: set[Listener] = set()
    if shutil.which("ss"):
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            result = subprocess.run(["ss", "-Htln"], capture_output=True, text=True, timeout=10, check=False)
            if result.returncode == 0:
                found.update(parse_ss(result.stdout))
    for path, ipv6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
        with contextlib.suppress(OSError):
            found.update(parse_proc_net(Path(path).read_text(), ipv6))
    return sorted(found, key=lambda item: (item.port, item.address))


def can_bind(port: int, host: str = LOOPBACK) -> bool:
    """Whether a listener could bind ``host:port`` now (SO_REUSEADDR, as servers do)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def port_range(env: Mapping[str, str] | None = None) -> tuple[int, int]:
    """HOLDUP_PORT_MIN..HOLDUP_PORT_MAX, which must stay inside 47800-47999."""
    env = os.environ if env is None else env
    try:
        low = int(env.get("HOLDUP_PORT_MIN") or PORT_RANGE[0])
        high = int(env.get("HOLDUP_PORT_MAX") or PORT_RANGE[1])
    except ValueError:
        raise ToolError("HOLDUP_PORT_MIN and HOLDUP_PORT_MAX must be numbers") from None
    if not PORT_RANGE[0] <= low <= high <= PORT_RANGE[1]:
        raise ToolError(f"ports must stay within {PORT_RANGE[0]}-{PORT_RANGE[1]} (got {low}-{high})")
    return low, high


def choose_ports(
    saved: Sequence[int | None],
    low: int,
    high: int,
    *,
    busy: Callable[[int], bool],
    rng: random.Random | None = None,
) -> list[int]:
    """Keep each saved port that is still free and in range; replace the others with free ones."""
    rng = rng or random.SystemRandom()
    chosen: list[int | None] = []
    for port in saved:
        ok = port is not None and low <= port <= high and port not in chosen and not busy(port)
        chosen.append(port if ok else None)
    candidates = [p for p in range(low, high + 1) if p not in chosen]
    rng.shuffle(candidates)
    for index, port in enumerate(chosen):
        if port is not None:
            continue
        while candidates:
            candidate = candidates.pop()
            if not busy(candidate):
                chosen[index] = candidate
                break
        else:
            raise ToolError(f"not enough free ports in {low}-{high}")
    return [int(p) for p in chosen if p is not None]


def busy_checker() -> Callable[[int], bool]:
    """A port is busy when anything listens on it (any address) or 127.0.0.1 cannot bind it."""
    listening = {item.port for item in listeners()}
    return lambda port: port in listening or not can_bind(port)


def read_ports(path: Path) -> dict[str, int | None]:
    values = read_env_file(path)
    out: dict[str, int | None] = {}
    for key in PORT_KEYS:
        text = values.get(key, "")
        out[key] = int(text) if text.isdigit() else None
    return out


def save_ports(path: Path, ports: Mapping[str, int]) -> None:
    header = (
        "Ports HoldUp listens on, on 127.0.0.1 only, chosen by scripts/server.sh from 47800-47999.",
        "The supervisor checks them before each start and picks new ones if something else took them.",
    )
    write_private(path, format_env({key: str(ports[key]) for key in PORT_KEYS}, header))


def ensure_ports(path: Path, env: Mapping[str, str] | None = None) -> tuple[dict[str, int], list[str]]:
    """The remembered ports, re-checked; changed ones are saved. Returns (ports, changed keys)."""
    low, high = port_range(env)
    saved = read_ports(path)
    chosen = choose_ports([saved[key] for key in PORT_KEYS], low, high, busy=busy_checker())
    ports = dict(zip(PORT_KEYS, chosen, strict=True))
    changed = [key for key in PORT_KEYS if saved[key] != ports[key]]
    if changed or not path.exists():
        save_ports(path, ports)
    return ports, changed


# ----------------------------------------------------------------------
# processes we started: records, verification, signals
# ----------------------------------------------------------------------


@dataclass
class ProcRecord:
    """A process this tool started: what to check before ever signalling it."""

    name: str
    pid: int
    start: int  # start time in clock ticks after boot (/proc/<pid>/stat, field 22)
    pgid: int
    cwd: str
    markers: list[str]  # every one must appear in the command line
    uid: int = field(default_factory=os.getuid)

    @classmethod
    def load(cls, path: Path) -> ProcRecord | None:
        data = read_json(path)
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                name=str(data["name"]),
                pid=int(data["pid"]),
                start=int(data["start"]),
                pgid=int(data["pgid"]),
                cwd=str(data["cwd"]),
                markers=[str(m) for m in data["markers"]],
                uid=int(data.get("uid", os.getuid())),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    state: str
    ppid: int
    pgid: int
    start: int


def proc_info(pid: int) -> ProcInfo | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # The command name (field 2) is in parentheses and may contain spaces or parentheses.
    fields = raw[raw.rfind(")") + 2:].split()
    try:
        return ProcInfo(pid, fields[0], int(fields[1]), int(fields[2]), int(fields[19]))
    except (IndexError, ValueError):
        return None


def proc_cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def proc_cwd(pid: int) -> str | None:
    try:
        return os.path.realpath(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        return None


def proc_uid(pid: int) -> int | None:
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return None


def all_pids() -> list[int]:
    return [int(name) for name in os.listdir("/proc") if name.isdigit()]


def is_ours(record: ProcRecord, pid: int, *, same_start: bool = True) -> bool:
    """``pid`` is the recorded process (or, with ``same_start=False``, one of its group) and still ours."""
    info = proc_info(pid)
    if info is None or info.state in ("Z", "X"):
        return False
    if same_start and (pid != record.pid or info.start != record.start):
        return False
    if proc_uid(pid) != record.uid or record.uid != os.getuid():
        return False
    command = " ".join(proc_cmdline(pid))
    if not command or not all(marker in command for marker in record.markers):
        return False
    return proc_cwd(pid) == os.path.realpath(record.cwd)


def our_processes(record: ProcRecord) -> list[int]:
    """The recorded process and the members of its process group, each verified."""
    found = [record.pid] if is_ours(record, record.pid) else []
    for pid in all_pids():
        if pid in found or pid == os.getpid():
            continue
        info = proc_info(pid)
        if info is not None and info.pgid == record.pgid and is_ours(record, pid, same_start=False):
            found.append(pid)
    return found


def send_signal(record: ProcRecord, pid: int, sig: int) -> bool:
    """Signal ``pid`` only after verifying it again; a pidfd keeps it from being swapped in between."""
    pidfd = None
    with contextlib.suppress(AttributeError, OSError):
        pidfd = os.pidfd_open(pid)
    try:
        if not is_ours(record, pid, same_start=pid == record.pid):
            return False
        if pidfd is not None:
            signal.pidfd_send_signal(pidfd, sig)
        else:
            os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return False
    finally:
        if pidfd is not None:
            os.close(pidfd)


def stop_recorded(records: Sequence[ProcRecord], timeout: float, say: Callable[[str], None]) -> list[str]:
    """SIGTERM every verified process of ``records``, wait up to ``timeout``, then SIGKILL what is left."""
    targets = [(record, pid) for record in records for pid in our_processes(record)]
    for record, pid in targets:
        if send_signal(record, pid, signal.SIGTERM):
            say(f"stopping {record.name} (pid {pid})")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(_alive(record, pid) for record, pid in targets):
        time.sleep(0.1)
    for record, pid in targets:
        if _alive(record, pid) and send_signal(record, pid, signal.SIGKILL):
            say(f"{record.name} (pid {pid}) did not stop within {timeout:.0f} s; killed it")
    time.sleep(0.05)
    return [record.name for record, pid in targets if _alive(record, pid)]


def _alive(record: ProcRecord, pid: int) -> bool:
    return is_ours(record, pid, same_start=pid == record.pid)


def record_path(run_dir: Path, name: str) -> Path:
    return run_dir / f"{name}.pid.json"


def load_records(run_dir: Path) -> dict[str, ProcRecord]:
    records = {}
    for path in sorted(run_dir.glob("*.pid.json")):
        record = ProcRecord.load(path)
        if record is not None:
            records[record.name] = record
    return records


def cleanup(run_dir: Path, timeout: float, say: Callable[[str], None]) -> bool:
    """Stop leftover processes recorded in ``run_dir``; True when none is left."""
    records = load_records(run_dir)
    supervisor = records.pop("supervisor", None)
    if supervisor is not None and our_processes(supervisor):
        # The supervisor stops its own children; give it the time it needs for that.
        stop_recorded([supervisor], timeout + 10, say)
        records = {name: rec for name, rec in load_records(run_dir).items() if name != "supervisor"}
    left = stop_recorded(list(records.values()), timeout, say)
    for name, record in list(records.items()) + ([("supervisor", supervisor)] if supervisor else []):
        if name not in left and not our_processes(record):
            with contextlib.suppress(FileNotFoundError):
                record_path(run_dir, name).unlink()
    if left:
        say(f"still running: {', '.join(left)}")
    return not left


def lock_is_free(path: Path) -> bool:
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


# ----------------------------------------------------------------------
# small HTTP client for 127.0.0.1 and the public check
# ----------------------------------------------------------------------


def get_json(url: str, timeout: float, *, local: bool = True) -> tuple[int, Any]:
    """(HTTP status, parsed JSON or None); status 0 when nothing answered.

    Local URLs are fetched directly, never through a proxy. The public check goes
    through urllib (HTTPS, and a proxy when the environment names one); it is
    imported only then, because setting up TLS takes a noticeable moment.
    """
    import http.client  # here, not at the top: most commands of this tool need no HTTP

    try:
        if local:
            parts = urllib.parse.urlsplit(url)
            if parts.scheme != "http" or not parts.hostname:
                return 0, None
            conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
            try:
                conn.request("GET", parts.path or "/", headers={"User-Agent": "holdup-server"})
                response = conn.getresponse()
                status, body = response.status, response.read(1_000_000)
            finally:
                conn.close()
        else:
            from urllib import error as url_error
            from urllib import request as url_request

            request = url_request.Request(url, headers={"User-Agent": "holdup-server"})
            try:
                with url_request.urlopen(request, timeout=timeout) as response:
                    status, body = response.status, response.read(1_000_000)
            except url_error.HTTPError as exc:
                status, body = exc.code, exc.read(1_000_000)
    except (OSError, http.client.HTTPException, ValueError):
        return 0, None
    try:
        return status, json.loads(body)
    except ValueError:
        return status, None


def health_line(data: Any) -> str:
    """One line about a /health answer: network, Telegram and keeper."""
    if not isinstance(data, dict):
        return "no answer"
    parts = ["ok" if data.get("ok") else "NOT ok", f"network {data.get('network', '?')}"]
    raw = data.get("telegram")
    telegram: dict[str, Any] = raw if isinstance(raw, dict) else {}
    mode = telegram.get("mode", "off")
    if mode == "off":
        parts.append("Telegram off")
    else:
        who = f" as @{telegram['username']}" if telegram.get("username") else ""
        state = "running" if telegram.get("running") else "not running"
        text = f"Telegram {mode}{who} ({state})"
        if telegram.get("conflict"):
            text += ", CONFLICT: another process uses this bot token"
        if telegram.get("last_error"):
            text += f", last error: {telegram['last_error']}"
        parts.append(text)
    keeper = data.get("keeper")
    if isinstance(keeper, dict):
        text = "keeper running" if keeper.get("running") else "keeper not running"
        if keeper.get("last_error"):
            text += f" (last error: {keeper['last_error']})"
        parts.append(text)
    return "; ".join(parts)


# ----------------------------------------------------------------------
# the supervisor
# ----------------------------------------------------------------------


def _number(env: Mapping[str, str], name: str, default: float) -> float:
    text = env.get(name, "").strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        raise ToolError(f"{name} must be a number (got {text!r})") from None
    if value < 0:
        raise ToolError(f"{name} must not be negative")
    return value


@dataclass
class Config:
    repo: Path
    home: Path
    secrets_dir: Path
    data_dir: Path
    cloudflared: str
    uv: str
    network: str  # "testnet" or "offline"
    path_env: str
    user_home: str
    port_env: dict[str, str] = field(default_factory=dict)
    check_interval: float = 10.0  # between health checks
    backoff_min: float = 5.0
    backoff_max: float = 60.0
    stable_after: float = 600.0  # healthy this long resets the backoff
    public_check_interval: float = 300.0  # 0 turns the check through Cloudflare off
    stop_timeout: float = 40.0  # holdup serve: 10 s for open requests, up to 20 s for a keeper round
    tunnel_timeout: float = 90.0  # to get a hostname and connect to Cloudflare
    app_timeout: float = 120.0  # for `holdup serve` to answer /health after it starts
    ready_grace: float = 120.0  # a connected tunnel may lose its connection this long
    health_failures: int = 3
    public_failures: int = 3
    log_max_bytes: int = 20 * 1024 * 1024
    log_keep: int = 2
    protocol: str = "http2"
    edge_ip_version: str = "4"

    @property
    def env_file(self) -> Path:
        return self.secrets_dir / "holdup.env"

    @property
    def credentials(self) -> Path:
        return self.secrets_dir / "holdup-credentials.json"

    @property
    def logs(self) -> Path:
        return self.data_dir / "logs"

    @property
    def run_dir(self) -> Path:
        return self.data_dir / "run"

    @property
    def ports_file(self) -> Path:
        return self.data_dir / "ports.env"

    @property
    def cloudflared_home(self) -> Path:
        return self.data_dir / "cloudflared"

    @property
    def cloudflared_config(self) -> Path:
        return self.cloudflared_home / "config.yml"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config:
        user_home = env.get("HOME") or str(Path.home())
        home = Path(env.get("HOLDUP_HOME") or f"{user_home}/work")
        network = env.get("HOLDUP_NETWORK") or "testnet"
        if network not in ("testnet", "offline"):
            raise ToolError(f"HOLDUP_NETWORK must be testnet or offline (got {network!r})")
        protocol = env.get("HOLDUP_TUNNEL_PROTOCOL") or "http2"
        if protocol != "http2":
            # QUIC (also tried first by "auto") opens a UDP socket on every interface, which this
            # server's rules forbid; HTTP/2 only makes outbound TCP connections.
            raise ToolError("HOLDUP_TUNNEL_PROTOCOL can only be http2: QUIC and auto bind a UDP socket "
                            "on all interfaces")
        edge = env.get("HOLDUP_TUNNEL_EDGE_IP_VERSION") or "4"
        if edge not in ("4", "6", "auto"):
            raise ToolError("HOLDUP_TUNNEL_EDGE_IP_VERSION must be 4, 6 or auto")
        return cls(
            repo=REPO,
            home=home,
            secrets_dir=Path(env.get("HOLDUP_SECRETS_DIR") or home / "secrets"),
            data_dir=Path(env.get("HOLDUP_DATA_DIR") or home / "holdup-data"),
            cloudflared=env.get("HOLDUP_CLOUDFLARED") or str(home / "bin" / "cloudflared"),
            uv=env.get("HOLDUP_UV") or shutil.which("uv") or "uv",
            network=network,
            path_env=env.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
            user_home=user_home,
            port_env={k: env[k] for k in ("HOLDUP_PORT_MIN", "HOLDUP_PORT_MAX") if env.get(k)},
            check_interval=_number(env, "HOLDUP_CHECK_INTERVAL", 10.0) or 10.0,
            backoff_min=_number(env, "HOLDUP_BACKOFF_MIN", 5.0),
            backoff_max=_number(env, "HOLDUP_BACKOFF_MAX", 60.0),
            stable_after=_number(env, "HOLDUP_STABLE_AFTER", 600.0),
            public_check_interval=_number(env, "HOLDUP_PUBLIC_CHECK_INTERVAL", 300.0),
            stop_timeout=_number(env, "HOLDUP_STOP_TIMEOUT", 40.0),
            tunnel_timeout=_number(env, "HOLDUP_TUNNEL_TIMEOUT", 90.0),
            app_timeout=_number(env, "HOLDUP_APP_TIMEOUT", 120.0),
            ready_grace=_number(env, "HOLDUP_READY_GRACE", 120.0),
            log_max_bytes=int(_number(env, "HOLDUP_LOG_MAX_BYTES", 20 * 1024 * 1024)),
            protocol=protocol,
            edge_ip_version=edge,
        )


class Backoff:
    """5 s, 10 s, 20 s, ... up to the cap; reset once things have been healthy for a while."""

    def __init__(self, first: float, cap: float) -> None:
        self.first, self.cap, self.failures = first, cap, 0

    def failed(self) -> float:
        self.failures += 1
        return min(self.first * 2 ** (self.failures - 1), self.cap)

    def reset(self) -> None:
        self.failures = 0


@dataclass
class Child:
    """A process the supervisor runs: the app or one of the tunnels."""

    name: str  # also the log file and record name: app, tunnel, fixtures
    label: str
    backoff: Backoff
    proc: subprocess.Popen[bytes] | None = None
    record: ProcRecord | None = None
    started: float = 0.0
    next_start: float = 0.0
    restarts: int = 0
    healthy_since: float | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None


@dataclass
class Tunnel(Child):
    port_key: str = ""
    hostname: str | None = None
    ready: bool = False
    not_ready_since: float | None = None

    @property
    def url(self) -> str | None:
        return f"https://{self.hostname}" if self.hostname and self.ready else None


@dataclass
class App(Child):
    urls: tuple[str, str] | None = None  # (public, training) it was started with; None: local only
    health_failures: int = 0
    healthy: bool = False
    answered: bool = False  # /health has worked at least once since this process started
    health: Any = None
    python_pid: int | None = None  # the `holdup serve` process uv started, for the status page


TICK_S = 0.25
ROTATE_EVERY_S = 60.0
PORT_GUARD_EVERY_S = 1.0  # how often the app's port is checked while the app does not hold it


class Supervisor:
    """Starts the tunnels and the app, watches them and restarts what fails. See docs/SERVER.md."""

    def __init__(self, config: Config, out: Callable[[str], None] | None = None) -> None:
        self.cfg = config
        self.out = out or self._print
        backoff = (config.backoff_min, config.backoff_max)
        self.tunnel = Tunnel("tunnel", "tunnel", Backoff(*backoff), port_key="TUNNEL_METRICS_PORT")
        self.fixtures = Tunnel("fixtures", "training-pages tunnel", Backoff(*backoff),
                               port_key="FIXTURE_METRICS_PORT")
        self.app = App("app", "holdup serve", Backoff(*backoff))
        self.ports: dict[str, int] = {}
        self.stopping = False
        self.hurry = False
        self.status = "starting"
        self.status_since = time.time()
        self.message = ""
        self.ever_running = False
        self.no_tunnel_since: float | None = None
        self.public_ok: bool | None = None
        self.public_checked_at: float | None = None
        self.public_failures = 0
        self.next_public_check = 0.0
        self._public_thread: threading.Thread | None = None
        self._public_result: bool | None = None
        self.next_rotate = 0.0
        self.next_port_guard = 0.0
        self.lock_fd: int | None = None
        self.wrote_record = False
        self._last_error_log = 0.0
        self._last_state: str = ""

    # -- output ----------------------------------------------------------
    @staticmethod
    def _print(line: str) -> None:
        with contextlib.suppress(OSError, ValueError):
            print(line, flush=True)

    def log(self, message: str) -> None:
        line = f"{utc_stamp()} {message}"
        self.out(line)
        with contextlib.suppress(OSError), open(self.cfg.logs / "supervisor.log", "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def note(self, message: str) -> None:
        self.message = message
        self.log(message)

    # -- lifecycle -------------------------------------------------------
    def install_signal_handlers(self) -> None:
        def handler(signum: int, frame: Any) -> None:
            if self.stopping:
                self.hurry = True
            self.stopping = True

        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, handler)

    def prepare(self) -> None:
        cfg = self.cfg
        os.umask(0o077)
        for directory in (cfg.data_dir, cfg.logs, cfg.run_dir, cfg.cloudflared_home):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_fd = os.open(cfg.run_dir / "supervisor.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunning("another HoldUp supervisor is already running (bash scripts/server.sh status)") from None
        previous = ProcRecord.load(record_path(cfg.run_dir, "supervisor"))
        if previous is not None and our_processes(previous):
            raise AlreadyRunning(f"another HoldUp supervisor is already running (pid {previous.pid})")
        self.write_record("supervisor", os.getpid(), pgid=os.getpgrp(), cwd=os.getcwd(),
                          markers=["server_tools.py", "supervise"])
        self.wrote_record = True
        leftovers = [r for name, r in load_records(cfg.run_dir).items() if name != "supervisor"]
        if leftovers:
            stop_recorded(leftovers, cfg.stop_timeout, lambda m: self.log(f"leftover from an earlier run: {m}"))
            for record in leftovers:
                if not our_processes(record):
                    record_path(cfg.run_dir, record.name).unlink(missing_ok=True)
        if not cfg.env_file.exists():
            raise ToolError(f"{cfg.env_file} is missing; run: bash scripts/server.sh setup")
        for change in secure_secrets(cfg.secrets_dir):
            self.log(f"secrets: {change}")
        read_env_file(cfg.env_file)  # a malformed file stops us here, not in a restart loop
        if cfg.network == "testnet":
            state, _ = credentials_status(cfg.credentials)
            if state != "complete":
                raise ToolError(f"no testnet deployment in {cfg.credentials}; run: bash scripts/server.sh testnet")
        if not os.access(cfg.cloudflared, os.X_OK):
            raise ToolError(f"cloudflared is not an executable file at {cfg.cloudflared}")
        if not cfg.cloudflared_config.exists():
            write_private(cfg.cloudflared_config, CLOUDFLARED_CONFIG)
        self.ports, changed = ensure_ports(cfg.ports_file, cfg.port_env)
        for key in changed:
            self.log(f"{key} is now {self.ports[key]} (the saved port was taken or missing)")
        self.no_tunnel_since = time.monotonic()
        self.log(
            f"supervisor {os.getpid()} starting HoldUp ({cfg.network}); app on {LOOPBACK}:{self.ports['HOLDUP_PORT']}, "
            f"tunnel metrics on {self.ports['TUNNEL_METRICS_PORT']} and {self.ports['FIXTURE_METRICS_PORT']}"
        )

    def run(self) -> int:
        self.install_signal_handlers()
        try:
            self.prepare()
        except AlreadyRunning as exc:
            self.out(f"{utc_stamp()} cannot start: {exc}")
            self.release()
            return 1
        except Exception as exc:  # noqa: BLE001 - reported in the state file, which `start` shows
            reason = str(exc) if isinstance(exc, ToolError) else f"{type(exc).__name__}: {exc}"
            self.log(f"cannot start: {reason}")
            self.status, self.message = "failed", reason
            self.write_state(force=True)
            self.release()
            return 1
        last_check = 0.0
        while not self.stopping:
            now = time.monotonic()
            check = now - last_check >= self.cfg.check_interval
            try:
                self.tick(now, check)
                self.update_status()
                self.write_state(force=check)
            except Exception as exc:  # noqa: BLE001 - the app and tunnels keep serving meanwhile
                if time.monotonic() - self._last_error_log > 60:
                    self._last_error_log = time.monotonic()
                    self.log(f"supervisor error ({type(exc).__name__}: {exc}); continuing")
            if check:
                last_check = now
            self.sleep(TICK_S)
        self.shutdown()
        return 0

    def sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < deadline:
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def shutdown(self) -> None:
        self.status, self.status_since = "stopping", time.time()
        self.write_state(force=True)
        self.log("stopping: app and tunnels")
        children = [c for c in (self.app, self.tunnel, self.fixtures) if c.proc is not None]
        procs = [c.proc for c in children if c.proc is not None]
        for child in children:
            self.terminate(child)
        deadline = time.monotonic() + self.cfg.stop_timeout
        while time.monotonic() < deadline and not self.hurry and any(proc.poll() is None for proc in procs):
            time.sleep(0.05)
        for child in children:
            self.finish(child, kill=True)
        self.status, self.status_since, self.message = "stopped", time.time(), "stopped"
        self.write_state(force=True)
        self.log("stopped")
        self.release()

    def release(self) -> None:
        if self.wrote_record:
            record_path(self.cfg.run_dir, "supervisor").unlink(missing_ok=True)
            self.wrote_record = False
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    # -- one step ----------------------------------------------------------
    def tick(self, now: float, check: bool) -> None:
        for tunnel in (self.tunnel, self.fixtures):
            self.step_tunnel(tunnel, now, check)
        if not self.app.answered and now >= self.next_port_guard:
            self.next_port_guard = now + PORT_GUARD_EVERY_S
            self.guard_app_port(now)
        self.step_app(now, check)
        if check:
            self.step_public_check(now)
        if now >= self.next_rotate:
            self.next_rotate = now + ROTATE_EVERY_S
            for name in ("app", "tunnel", "fixtures", "supervisor"):
                rotate_log(self.cfg.logs / f"{name}.log", self.cfg.log_max_bytes, self.cfg.log_keep)

    def step_tunnel(self, tunnel: Tunnel, now: float, check: bool) -> None:
        cfg = self.cfg
        if tunnel.proc is None:
            if now >= tunnel.next_start:
                self.start_tunnel(tunnel, now)
            return
        code = tunnel.proc.poll()
        if code is not None:
            self.note(f"{tunnel.label} exited with code {code}{log_tail(cfg.logs / f'{tunnel.name}.log')}")
            self.finish(tunnel)
            self.schedule(tunnel, now)
            return
        port = self.ports[tunnel.port_key]
        metrics = f"http://{LOOPBACK}:{port}"
        if tunnel.hostname is None:
            # cloudflared opens its metrics port only once Cloudflare has created the tunnel, and any
            # local user can read that port from its command line. Only a socket our cloudflared
            # holds is asked for the hostname; once it does, nobody else can bind the port.
            listening, stranger = self.stranger_on(port, tunnel)
            if stranger is not None:
                self.note(f"{tunnel.label}: port {port} ({tunnel.port_key}) is held by another program "
                          f"(uid {stranger.uid}), whose answers are not trusted; replacing the tunnel")
                self.replace(tunnel, now)
                return
            status, data = get_json(f"{metrics}/quicktunnel", 1.0) if listening else (0, None)
            hostname = data.get("hostname") if status == 200 and isinstance(data, dict) else None
            if isinstance(hostname, str) and TUNNEL_HOST.match(hostname.lower()):
                tunnel.hostname = hostname.lower()
                self.log(f"{tunnel.label} got https://{tunnel.hostname}")
            elif hostname:
                self.note(f"{tunnel.label} reported an unexpected hostname; replacing it")
                self.replace(tunnel, now)
            elif now - tunnel.started > cfg.tunnel_timeout:
                self.note(f"{tunnel.label} got no hostname within {cfg.tunnel_timeout:.0f} s "
                          f"(can this server reach api.trycloudflare.com on port 443?); replacing it")
                self.replace(tunnel, now)
            return
        if not tunnel.ready:
            if get_json(f"{metrics}/ready", 2.0)[0] == 200:
                tunnel.ready, tunnel.healthy_since, tunnel.not_ready_since = True, now, None
                self.log(f"{tunnel.label} is connected to Cloudflare")
            elif now - tunnel.started > cfg.tunnel_timeout:
                self.note(f"{tunnel.label} did not connect to Cloudflare within {cfg.tunnel_timeout:.0f} s "
                          f"(outbound TCP port 7844 blocked?); replacing it")
                self.replace(tunnel, now)
            return
        if not check:
            return
        if get_json(f"{metrics}/ready", 5.0)[0] == 200:
            tunnel.not_ready_since = None
            if tunnel.healthy_since is not None and now - tunnel.healthy_since >= cfg.stable_after:
                tunnel.backoff.reset()
            return
        if tunnel.not_ready_since is None:
            tunnel.not_ready_since = now
            self.log(f"{tunnel.label} lost its connection to Cloudflare; waiting for it to reconnect")
        elif now - tunnel.not_ready_since > cfg.ready_grace:
            self.note(f"{tunnel.label} has been disconnected for {cfg.ready_grace:.0f} s; replacing it")
            self.replace(tunnel, now)

    def wanted_urls(self) -> tuple[str, str] | None:
        if self.tunnel.url and self.fixtures.url:
            return self.tunnel.url, self.fixtures.url
        return None

    def step_app(self, now: float, check: bool) -> None:
        cfg, app = self.cfg, self.app
        want = self.wanted_urls()
        if want is not None:
            self.no_tunnel_since = None
        elif self.no_tunnel_since is None:
            self.no_tunnel_since = now
        if app.proc is None:
            if now < app.next_start:
                return
            if want is not None:
                self.start_app(want, now)
            elif self.no_tunnel_since is not None and now - self.no_tunnel_since > cfg.tunnel_timeout:
                self.note("the tunnels are not both connected; starting the app without public URLs meanwhile "
                          "(Telegram polling still works)")
                self.start_app(None, now)
            return
        code = app.proc.poll()
        if code is not None:
            self.note(f"holdup serve exited with code {code}{log_tail(cfg.logs / 'app.log')}")
            self.finish(app)
            self.schedule(app, now)
            return
        if want is not None and app.urls != want:
            self.note(f"public URLs changed; restarting the app with {want[0]}")
            self.stop_child(app)
            app.next_start = now
            return
        if not check:
            return
        port = self.ports["HOLDUP_PORT"]
        status, data = get_json(f"http://{LOOPBACK}:{port}/health", 5.0)
        if status == 200 and isinstance(data, dict) and data.get("ok"):
            if not app.answered:
                # The first answer counts only if our app holds the port; after that nobody else can.
                _, stranger = self.stranger_on(port, app)
                if stranger is not None:
                    self.app_port_taken(stranger, now)
                    return
                app.python_pid = app_python_pid(app)
            if not app.healthy:
                self.log(f"holdup serve is healthy: {health_line(data)}")
            app.healthy, app.answered, app.health, app.health_failures = True, True, data, 0
            if app.healthy_since is None:
                app.healthy_since = now
            elif now - app.healthy_since >= cfg.stable_after:
                app.backoff.reset()
            return
        if not app.answered:  # still starting
            if now - app.started > cfg.app_timeout:
                self.note(f"holdup serve did not answer /health within {cfg.app_timeout:.0f} s; restarting it"
                          f"{log_tail(cfg.logs / 'app.log')}")
                self.stop_child(app)
                self.schedule(app, now)
            return
        app.health_failures += 1
        app.healthy, app.healthy_since = False, None
        if app.health_failures >= cfg.health_failures:
            self.note(f"/health failed {app.health_failures} times in a row; restarting the app")
            self.stop_child(app)
            self.schedule(app, now)

    def step_public_check(self, now: float) -> None:
        """Every few minutes, fetch /health through Cloudflare; replace the tunnel after repeated failures."""
        cfg = self.cfg
        if self._public_thread is not None and not self._public_thread.is_alive():
            self._public_thread = None
            ok = bool(self._public_result)
            self.public_ok, self.public_checked_at = ok, time.time()
            if ok:
                self.public_failures = 0
            else:
                self.public_failures += 1
                self.log(f"{self.tunnel.url or 'the public URL'} did not answer from the internet "
                         f"({self.public_failures}/{cfg.public_failures})")
                if self.public_failures >= cfg.public_failures and self.tunnel.proc is not None:
                    self.public_failures = 0
                    self.note("the public URL keeps failing; replacing the tunnel")
                    self.replace(self.tunnel, now)
        if cfg.public_check_interval <= 0 or self.status != "running" or self._public_thread is not None:
            return
        if self.next_public_check == 0.0:
            self.next_public_check = now + min(60.0, cfg.public_check_interval)
        if now < self.next_public_check or not self.tunnel.url:
            return
        self.next_public_check = now + cfg.public_check_interval
        url = f"{self.tunnel.url}/health"

        def probe() -> None:
            status, data = get_json(url, 15.0, local=False)
            self._public_result = status == 200 and isinstance(data, dict) and bool(data.get("ok"))

        self._public_thread = threading.Thread(target=probe, name="public-check", daemon=True)
        self._public_thread.start()

    # -- starting and stopping children ------------------------------------
    def schedule(self, child: Child, now: float) -> None:
        delay = child.backoff.failed()
        child.next_start = now + delay
        child.restarts += 1
        self.log(f"{child.label}: next start in {delay:.0f} s")

    def replace(self, tunnel: Tunnel, now: float) -> None:
        self.stop_child(tunnel)
        self.schedule(tunnel, now)

    def stranger_on(self, port: int, child: Child) -> tuple[bool, Socket | None]:
        """(whether anything listens on ``port``, a listener there that ``child``'s processes do not hold)."""
        sockets = listening_sockets(port)
        if not sockets:
            return False, None
        ours = socket_inodes(our_processes(child.record)) if child.record is not None else set()
        uid = os.getuid()
        return True, next((item for item in sockets if item.uid != uid or item.inode not in ours), None)

    def guard_app_port(self, now: float) -> None:
        """While our app does not hold its port, the tunnels must not forward to someone else's program."""
        if self.tunnel.proc is None and self.fixtures.proc is None:
            return
        _, stranger = self.stranger_on(self.ports["HOLDUP_PORT"], self.app)
        if stranger is not None:
            self.app_port_taken(stranger, now)

    def app_port_taken(self, stranger: Socket, now: float) -> None:
        """Another program listens on the app's port: cut the tunnels at once and move to another port."""
        self.note(f"port {self.ports['HOLDUP_PORT']} (HOLDUP_PORT) is held by another program (uid {stranger.uid}); "
                  f"stopping the tunnels so that no public request reaches it, and moving to another port")
        for tunnel in (self.tunnel, self.fixtures):
            if tunnel.proc is not None:
                with contextlib.suppress(ProcessLookupError):
                    tunnel.proc.kill()  # a graceful stop would keep forwarding for its grace period
                self.finish(tunnel)
            tunnel.next_start = now
        self.stop_child(self.app)
        self.recheck_port("HOLDUP_PORT")
        self.app.next_start = now

    def recheck_port(self, key: str) -> bool:
        """Make sure the port for ``key`` is free; pick and save another one if not. True if it changed."""
        busy = busy_checker()
        if not busy(self.ports[key]):
            return False
        old = self.ports[key]
        others = {v for k, v in self.ports.items() if k != key}
        low, high = port_range(self.cfg.port_env)
        new = choose_ports([None], low, high, busy=lambda p: p in others or busy(p))[0]
        self.ports[key] = new
        save_ports(self.cfg.ports_file, self.ports)
        self.log(f"port {old} ({key}) is taken by another program; using {new} from now on")
        return True

    def start_tunnel(self, tunnel: Tunnel, now: float) -> None:
        cfg = self.cfg
        self.recheck_port(tunnel.port_key)
        argv = [
            cfg.cloudflared, "tunnel",
            "--no-autoupdate",
            "--config", str(cfg.cloudflared_config),
            "--metrics", f"{LOOPBACK}:{self.ports[tunnel.port_key]}",
            "--url", f"http://{LOOPBACK}:{self.ports['HOLDUP_PORT']}",
            "--protocol", cfg.protocol,
            "--edge-ip-version", cfg.edge_ip_version,
            "--loglevel", "info",
            "--grace-period", "5s",
        ]
        env = {"HOME": str(cfg.cloudflared_home), "PATH": cfg.path_env, "LANG": "C.UTF-8"}
        markers = ["tunnel", "--metrics", f"{LOOPBACK}:{self.ports[tunnel.port_key]}"]
        if not self.spawn(tunnel, argv, env=env, cwd=cfg.cloudflared_home, markers=markers):
            return
        tunnel.hostname, tunnel.ready, tunnel.not_ready_since, tunnel.healthy_since = None, False, None, None
        tunnel.started = now

    def app_env(self, urls: tuple[str, str] | None) -> dict[str, str]:
        cfg = self.cfg
        env = {"HOME": cfg.user_home, "PATH": cfg.path_env, "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1"}
        for name in ("USER", "LOGNAME"):
            if os.environ.get(name):
                env[name] = os.environ[name]
        env.update(read_env_file(cfg.env_file))
        env.update({
            "HOLDUP_NETWORK": cfg.network,
            "HOLDUP_PORT": str(self.ports["HOLDUP_PORT"]),
            "HOLDUP_DB": str(cfg.data_dir / "holdup.sqlite3"),
            "HOLDUP_CREDENTIALS": str(cfg.credentials),
        })
        for name in ("PUBLIC_BASE_URL", "FIXTURE_BASE_URL"):
            env.pop(name, None)
        if urls is not None:
            env["PUBLIC_BASE_URL"], env["FIXTURE_BASE_URL"] = urls
        else:
            # Without public URLs the training pages still get a host of their own, one only this
            # machine reaches, so a tunnel that is up meanwhile never shows them under its hostname.
            env["FIXTURE_BASE_URL"] = f"http://localhost:{self.ports['HOLDUP_PORT']}"
        return env

    def app_argv(self) -> list[str]:
        network = ["--offline"] if self.cfg.network == "offline" else ["--network", "testnet"]
        return [
            self.cfg.uv, "run", "--locked", "--no-dev", "--no-managed-python", "--no-python-downloads",
            "holdup", "serve", *network, "--host", LOOPBACK, "--port", str(self.ports["HOLDUP_PORT"]),
        ]

    def start_app(self, urls: tuple[str, str] | None, now: float) -> None:
        if self.recheck_port("HOLDUP_PORT"):
            # The tunnels forward to the old port: start them again (new hostnames), then the app.
            for tunnel in (self.tunnel, self.fixtures):
                self.stop_child(tunnel)
                tunnel.next_start = now
            return
        app = self.app
        if not self.spawn(app, self.app_argv(), env=self.app_env(urls), cwd=self.cfg.repo,
                          markers=["holdup", "serve", str(self.ports["HOLDUP_PORT"])]):
            return
        app.urls, app.started, app.health_failures, app.healthy_since = urls, now, 0, None
        app.healthy = app.answered = False
        self.log(f"holdup serve started (pid {app.proc.pid if app.proc else '?'}) "
                 f"with {urls[0] if urls else 'no public URL'}")

    def spawn(self, child: Child, argv: list[str], *, env: dict[str, str], cwd: Path, markers: list[str]) -> bool:
        """Start ``child`` and record it; False (with a retry scheduled) when either fails.

        A child never runs without its record, or ``stop`` could not find it after the
        supervisor itself died.
        """
        log_path = self.cfg.logs / f"{child.name}.log"
        try:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(f"--- {utc_stamp()} starting: {' '.join(argv)}\n")
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        except OSError as exc:
            return self.not_started(child, f"cannot start {child.label}: cannot write {log_path} "
                                           f"({exc.strerror or exc})")
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=fd, stderr=subprocess.STDOUT, cwd=cwd, env=env,
                start_new_session=True, close_fds=True,
            )
        except OSError as exc:
            return self.not_started(child, f"cannot start {child.label}: {exc.strerror or exc}")
        finally:
            os.close(fd)
        try:
            # start_new_session: the child leads a new session and process group of its own.
            record = self.write_record(child.name, proc.pid, pgid=proc.pid, cwd=str(cwd), markers=markers)
        except OSError as exc:
            # That group was just created and the child is not reaped yet, so its pid is still
            # ours: stop the whole group at once rather than leave it running unrecorded.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            return self.not_started(child, f"cannot record {child.label} in {self.cfg.run_dir} "
                                           f"({exc.strerror or exc}); stopped it again")
        child.proc, child.record = proc, record
        return True

    def not_started(self, child: Child, reason: str) -> bool:
        child.proc, child.record = None, None
        self.note(reason)
        self.schedule(child, time.monotonic())
        return False

    def write_record(self, name: str, pid: int, *, pgid: int, cwd: str, markers: list[str]) -> ProcRecord:
        info = proc_info(pid)
        record = ProcRecord(name=name, pid=pid, start=info.start if info else 0, pgid=pgid,
                            cwd=os.path.realpath(cwd), markers=markers)
        write_json(record_path(self.cfg.run_dir, name), asdict(record))
        return record

    def terminate(self, child: Child) -> None:
        if child.proc is not None and child.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                child.proc.terminate()

    def finish(self, child: Child, *, kill: bool = False) -> None:
        """Reap ``child`` (killing it first when asked), stop what is left of its group, forget it."""
        proc, record = child.proc, child.record
        if proc is not None:
            if kill and proc.poll() is None:
                self.log(f"{child.label} did not stop in time; killing it")
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
        if record is not None:
            # uv runs `holdup serve` as its own child: make sure nothing of the group is left.
            left = [pid for pid in our_processes(record) if pid != record.pid]
            if left:
                stop_recorded([record], 2.0 if kill else self.cfg.stop_timeout, self.log)
            record_path(self.cfg.run_dir, child.name).unlink(missing_ok=True)
        child.proc, child.record = None, None
        if isinstance(child, Tunnel):
            child.hostname, child.ready = None, False
        if isinstance(child, App):
            child.healthy, child.answered, child.health, child.python_pid = False, False, None, None

    def stop_child(self, child: Child) -> None:
        if child.proc is None:
            return
        self.terminate(child)
        try:
            child.proc.wait(timeout=self.cfg.stop_timeout)
        except subprocess.TimeoutExpired:
            pass
        self.finish(child, kill=True)

    # -- state -------------------------------------------------------------
    def update_status(self) -> None:
        if self.stopping:
            return
        app_ok = self.app.proc is not None and self.app.healthy
        want = self.wanted_urls()
        if app_ok and want is not None and self.app.urls == want:
            status = "running"
            self.ever_running = True
        elif app_ok:
            status = "degraded"
        else:
            status = "restarting" if self.ever_running else "starting"
        if status != self.status:
            self.status, self.status_since = status, time.time()
            if status == "running":
                self.note(f"HoldUp is running at {self.app.urls[0] if self.app.urls else '?'}")

    def state(self) -> dict[str, Any]:
        def child_state(child: Child) -> dict[str, Any]:
            data: dict[str, Any] = {"pid": child.proc.pid if child.proc else None, "restarts": child.restarts}
            if isinstance(child, Tunnel):
                data.update(hostname=child.hostname, ready=child.ready)
            if isinstance(child, App):
                data.update(healthy=child.healthy, health=child.health, python_pid=child.python_pid,
                            local_only=child.proc is not None and child.urls is None)
            return data

        urls = self.app.urls if self.app.proc is not None else None
        return {
            "status": self.status,
            "since": utc_stamp(self.status_since),
            "updated": utc_stamp(),
            "message": self.message,
            "network": self.cfg.network,
            "supervisor_pid": os.getpid(),
            "public_url": urls[0] if urls else None,
            "fixture_url": urls[1] if urls else None,
            "ports": self.ports,
            "app": child_state(self.app),
            "tunnel": child_state(self.tunnel),
            "fixtures": child_state(self.fixtures),
            "public_check": {"ok": self.public_ok,
                             "at": utc_stamp(self.public_checked_at) if self.public_checked_at else None},
        }

    def write_state(self, *, force: bool = False) -> None:
        state = self.state()
        key = json.dumps({k: v for k, v in state.items() if k != "updated"}, sort_keys=True, default=str)
        if force or key != self._last_state:
            self._last_state = key
            with contextlib.suppress(OSError):
                write_json(self.cfg.run_dir / "state.json", state)


def app_python_pid(app: App) -> int | None:
    """The `holdup serve` process that uv started, for the status page."""
    record = app.record
    if record is None:
        return None
    for pid in our_processes(record):
        info = proc_info(pid)
        if pid != record.pid and info is not None and info.ppid == record.pid:
            return pid
    return None


CLOUDFLARED_CONFIG = """\
# Configuration for HoldUp's quick tunnels, passed with --config so that no
# ~/.cloudflared/config.yml (whose ingress rules would win over --url) is read.
# All settings are on the command line; see scripts/server_tools.py.
{}
"""


def log_tail(path: Path, lines: int = 3) -> str:
    """The last lines of a log, for a message about a process that stopped."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 4096))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    tail = [line.strip() for line in text.splitlines() if line.strip()][-lines:]
    return ("; last log lines: " + " | ".join(tail)) if tail else ""


def rotate_log(path: Path, max_bytes: int, keep: int) -> bool:
    """Copy a log above ``max_bytes`` to ``.1`` (shifting older copies) and empty it.

    Children write with O_APPEND, so they carry on at the start of the emptied file.
    """
    try:
        if path.stat().st_size <= max_bytes:
            return False
    except FileNotFoundError:
        return False
    for index in range(keep, 1, -1):
        older = path.with_name(f"{path.name}.{index - 1}")
        if older.exists():
            os.replace(older, path.with_name(f"{path.name}.{index}"))
    shutil.copyfile(path, path.with_name(f"{path.name}.1"))
    os.chmod(path.with_name(f"{path.name}.1"), 0o600)
    os.truncate(path, 0)
    return True


# ----------------------------------------------------------------------
# credentials, names, Telegram
# ----------------------------------------------------------------------


def credentials_status(path: Path) -> tuple[str, dict[str, Any]]:
    """("none" | "partial" | "complete" | "invalid", the public fields)."""
    if not path.exists():
        return "none", {}
    data = read_json(path)
    if not isinstance(data, dict):
        return "invalid", {}
    public = {key: data[key] for key in PUBLIC_CREDENTIAL_FIELDS if data.get(key)}
    if isinstance(public.get("rpc_url"), str):  # an API key may travel in its query
        parts = urllib.parse.urlsplit(public["rpc_url"])
        public["rpc_url"] = urllib.parse.urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, "", ""))
    complete = all(data.get(key) for key in DEPLOYED_FIELDS) and data.get("network") == "testnet"
    return ("complete" if complete else "partial"), public


def default_root_name() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "holdup-" + "".join(secrets.choice(alphabet) for _ in range(6)) + ".testnet"


def telegram_username(text: str) -> str:
    try:
        reply = json.loads(text)
    except ValueError:
        raise ToolError("Telegram answered with something that is not JSON") from None
    if not isinstance(reply, dict) or not reply.get("ok"):
        description = reply.get("description") if isinstance(reply, dict) else None
        raise ToolError(f"Telegram refused: {description or 'no description'}")
    username = (reply.get("result") or {}).get("username")
    if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]{3,64}", username):
        raise ToolError("Telegram's answer has no bot username")
    return username


# ----------------------------------------------------------------------
# status for people
# ----------------------------------------------------------------------


def status_report(run_dir: Path, ports_file: Path) -> list[str]:
    state = read_json(run_dir / "state.json")
    records = load_records(run_dir)
    supervisor = records.get("supervisor")
    supervisor_alive = supervisor is not None and bool(our_processes(supervisor))
    lines = []
    if supervisor is not None and supervisor_alive and isinstance(state, dict):
        lines.append(f"Supervisor   pid {supervisor.pid}; HoldUp is {state.get('status')} since {state.get('since')}")
        if state.get("message"):
            lines.append(f"Last event   {state['message']}")
    elif isinstance(state, dict) and state.get("status") == "failed":
        lines.append(f"Supervisor   not running; it stopped because: {state.get('message')}")
    else:
        lines.append("Supervisor   not running")
    ports = read_ports(ports_file)
    if all(ports.values()):
        lines.append(
            f"Ports        app {LOOPBACK}:{ports['HOLDUP_PORT']}, tunnel metrics {LOOPBACK}:{ports['TUNNEL_METRICS_PORT']}, "
            f"training tunnel metrics {LOOPBACK}:{ports['FIXTURE_METRICS_PORT']}"
        )
    if not supervisor_alive or not isinstance(state, dict):
        return lines
    lines.append(f"Public URL   {state.get('public_url') or '(none yet)'}")
    lines.append(f"Training URL {state.get('fixture_url') or '(none yet)'}  (training pages only)")
    processes = []
    for name, label in (("app", "app"), ("tunnel", "tunnel"), ("fixtures", "training tunnel")):
        item = state.get(name) or {}
        text = f"{label} {item.get('pid') or '-'}"
        if name == "app" and item.get("python_pid"):
            text += f" (python {item['python_pid']})"
        if item.get("restarts"):
            text += f", {item['restarts']} restart(s)"
        processes.append(text)
    lines.append("Processes    " + "; ".join(processes))
    port = (state.get("ports") or {}).get("HOLDUP_PORT") or ports.get("HOLDUP_PORT")
    status, data = get_json(f"http://{LOOPBACK}:{port}/health", 5.0) if port else (0, None)
    lines.append(f"Health       {health_line(data) if status == 200 else f'no answer from {LOOPBACK}:{port}/health'}")
    check = state.get("public_check") or {}
    if check.get("at"):
        lines.append(f"Public check {'ok' if check.get('ok') else 'FAILED'} at {check['at']}")
    return lines


# ----------------------------------------------------------------------
# command line
# ----------------------------------------------------------------------


def _say(message: str) -> None:
    print(message, flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="server_tools.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("supervise")
    p = sub.add_parser("cleanup")
    p.add_argument("run_dir", type=Path)
    p.add_argument("--timeout", type=float, default=40.0)
    p = sub.add_parser("ports")
    p.add_argument("file", type=Path)
    p = sub.add_parser("ports-busy")
    p.add_argument("file", type=Path)
    p = sub.add_parser("lock-free")
    p.add_argument("file", type=Path)
    p = sub.add_parser("supervisor-pid")
    p.add_argument("run_dir", type=Path)
    p = sub.add_parser("secure-secrets")
    p.add_argument("dir", type=Path)
    p = sub.add_parser("env-merge")
    p.add_argument("file", type=Path)
    p = sub.add_parser("env-get")
    p.add_argument("file", type=Path)
    p.add_argument("key")
    p = sub.add_parser("env-check")
    p.add_argument("file", type=Path)
    sub.add_parser("secret")
    sub.add_parser("root-name")
    p = sub.add_parser("check-root-name")
    p.add_argument("name")
    p = sub.add_parser("credentials")
    p.add_argument("file", type=Path)
    p.add_argument("field", nargs="?")
    p = sub.add_parser("credentials-secret")
    p.add_argument("file", type=Path)
    sub.add_parser("telegram-username")
    p = sub.add_parser("state")
    p.add_argument("file", type=Path)
    p.add_argument("key", nargs="?")
    p = sub.add_parser("status")
    p.add_argument("run_dir", type=Path)
    p.add_argument("ports_file", type=Path)
    sub.add_parser("health")
    args = parser.parse_args(argv)

    try:
        if args.command == "supervise":
            if os.getuid() == 0:
                raise ToolError("refusing to run as root")
            return Supervisor(Config.from_env(os.environ)).run()
        if args.command == "cleanup":
            ok = cleanup(args.run_dir, args.timeout, _say)
            return 0 if ok else 1
        if args.command == "ports":
            ports, changed = ensure_ports(args.file)
            for key in PORT_KEYS:
                _say(f"{key}={ports[key]}{'  (new)' if key in changed else ''}")
            return 0
        if args.command == "ports-busy":
            listening = {item.port for item in listeners()}
            busy = [f"{k}={v}" for k, v in read_ports(args.file).items() if v and v in listening]
            for item in busy:
                _say(item)
            return 1 if busy else 0
        if args.command == "lock-free":
            return 0 if lock_is_free(args.file) else 1
        if args.command == "supervisor-pid":
            record = load_records(args.run_dir).get("supervisor")
            if record is None or not is_ours(record, record.pid):
                return 1
            _say(str(record.pid))
            return 0
        if args.command == "secure-secrets":
            for change in secure_secrets(args.dir):
                _say(change)
            return 0
        if args.command == "env-merge":
            added, kept = merge_env(args.file, read_stdin_pairs(sys.stdin))
            if added:
                _say(f"saved {', '.join(added)}")
            if kept:
                _say(f"kept {', '.join(kept)} (already set)")
            return 0
        if args.command == "env-get":
            value = read_env_file(args.file).get(args.key, "")
            if not value:
                return 1
            sys.stdout.write(value + "\n")
            return 0
        if args.command == "env-check":
            read_env_file(args.file)
            return 0
        if args.command == "secret":
            _say(secrets.token_hex(32))
            return 0
        if args.command == "root-name":
            _say(default_root_name())
            return 0
        if args.command == "check-root-name":
            name = args.name
            return 0 if ACCOUNT_NAME.match(name) and 2 <= len(name) <= ROOT_NAME_MAX else 1
        if args.command == "credentials":
            state, public = credentials_status(args.file)
            if args.field:
                if args.field == "status":
                    _say(state)
                    return 0
                if args.field not in PUBLIC_CREDENTIAL_FIELDS:
                    raise ToolError(f"{args.field} is not a field this tool shows")
                shown = public.get(args.field)
                if not shown:
                    return 1
                _say(",".join(shown) if isinstance(shown, list) else str(shown))
                return 0
            _say(f"status: {state}")
            for key, shown in public.items():
                _say(f"{key}: {','.join(shown) if isinstance(shown, list) else shown}")
            return 0
        if args.command == "credentials-secret":
            # For `secret=$(...)` in server.sh only: the value goes to a variable, never to the screen.
            data = read_json(args.file)
            secret = data.get("secret") if isinstance(data, dict) else None
            if not isinstance(secret, str) or not secret:
                return 1
            sys.stdout.write(secret + "\n")
            return 0
        if args.command == "telegram-username":
            _say(telegram_username(sys.stdin.read()))
            return 0
        if args.command == "state":
            saved = read_json(args.file)
            if not isinstance(saved, dict):
                return 1
            if not args.key:
                _say(json.dumps(saved, indent=1, sort_keys=True))
                return 0
            found: Any = saved
            for part in args.key.split("."):
                found = found.get(part) if isinstance(found, dict) else None
            if found in (None, ""):
                return 1
            _say(found if isinstance(found, str) else json.dumps(found))
            return 0
        if args.command == "status":
            for line in status_report(args.run_dir, args.ports_file):
                _say(line)
            return 0
        if args.command == "health":
            try:
                data = json.loads(sys.stdin.read())
            except ValueError:
                data = None
            _say(health_line(data))
            return 0
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
