#!/usr/bin/env python3
"""Copy the NEAR settings from a `holdup deploy` credentials file into the server's env file.

    python3 deploy/credentials_to_env.py ~/work/secrets/credentials.json ~/work/secrets/holdup.env

Each setting replaces the KEY= line of the same name in the env file, or is appended. Settings
that are missing or empty in the credentials file leave the env file alone. HOLDUP_SECRET is
copied only when the env file has none: every user's account is derived from it, so an existing
secret is never replaced. Values are never printed. The env file is rewritten in one step and
keeps mode 600. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

# credentials.json key -> environment variable read by holdup/config.py
SETTINGS = (
    ("network", "HOLDUP_NETWORK"),
    ("rpc_url", "NEAR_RPC_URL"),
    ("root_account", "HOLDUP_ROOT_ACCOUNT"),
    ("vault_account", "HOLDUP_VAULT_ACCOUNT"),
    ("oracle_accounts", "HOLDUP_ORACLE_ACCOUNTS"),
    ("keeper_account", "HOLDUP_KEEPER_ACCOUNT"),
    ("husd_token", "HOLDUP_HUSD_TOKEN"),
    ("usdc_token", "HOLDUP_USDC_TOKEN"),
    ("treasury_private_key", "HOLDUP_TREASURY_KEY"),
    ("oracle_private_key", "HOLDUP_ORACLE_KEY"),
    ("keeper_private_key", "HOLDUP_KEEPER_KEY"),
)
SECRET_KEY, SECRET_VAR = "secret", "HOLDUP_SECRET"

# Characters that need no quoting in a Docker Compose env file (no spaces, quotes, $, # or \).
SAFE_VALUE = re.compile(r"[A-Za-z0-9._:,/@+=~-]+")
ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


def current_value(raw: str) -> str:
    """The value of an env-file assignment, without quotes or an inline comment."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw.split(" #", 1)[0].strip()


def plan(creds: dict, lines: list[str]) -> tuple[dict[str, str], list[str]]:
    """Values to write, and notes for the user (names only)."""
    existing: dict[str, str] = {}
    for line in lines:
        match = ASSIGNMENT.match(line)
        if match:
            existing[match.group(1)] = current_value(match.group(2))

    updates: dict[str, str] = {}
    notes: list[str] = []
    for key, var in SETTINGS:
        value = as_text(creds.get(key))
        if value:
            updates[var] = value
    secret = as_text(creds.get(SECRET_KEY))
    if secret:
        if not existing.get(SECRET_VAR):
            updates[SECRET_VAR] = secret
        elif existing[SECRET_VAR] != secret:
            notes.append(
                f"kept the existing {SECRET_VAR}; it differs from the credentials file. If users were "
                "onboarded with the credentials file's secret, put that one in by hand instead."
            )
    for var, value in updates.items():
        if not SAFE_VALUE.fullmatch(value):
            raise ValueError(f"{var} has characters that would need quoting; edit the env file by hand")
    return updates, notes


def apply(lines: list[str], updates: dict[str, str]) -> list[str]:
    out: list[str] = []
    done: set[str] = set()
    for line in lines:
        match = ASSIGNMENT.match(line)
        var = match.group(1) if match else None
        if var in updates:
            if var not in done:
                out.append(f"{var}={updates[var]}\n")
                done.add(var)
            continue  # drop later duplicates so the new value is the one that counts
        out.append(line if line.endswith("\n") else line + "\n")
    missing = [var for var in updates if var not in done]
    if missing:
        out.append("\n# From the `holdup deploy` credentials file\n")
        out.extend(f"{var}={updates[var]}\n" for var in missing)
    return out


def write_private(path: Path, lines: list[str]) -> None:
    """Replace path atomically with a mode 600 file, keeping its owner when run as root."""
    stat = path.stat() if path.exists() else None
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.writelines(lines)
        os.chmod(tmp, 0o600)
        if stat is not None and os.geteuid() == 0:
            os.chown(tmp, stat.st_uid, stat.st_gid)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("credentials", type=Path, help="credentials JSON written by `holdup deploy`")
    parser.add_argument("env_file", type=Path, help="env file to update, e.g. ~/work/secrets/holdup.env")
    parser.add_argument("--dry-run", action="store_true", help="show which variables would change")
    args = parser.parse_args(argv)

    try:
        creds = json.loads(args.credentials.expanduser().read_text())
    except (OSError, ValueError) as exc:
        print(f"error: cannot read {args.credentials}: {exc}", file=sys.stderr)
        return 1
    if not isinstance(creds, dict):
        print(f"error: {args.credentials} is not a JSON object", file=sys.stderr)
        return 1

    # Follow symlinks (deploy/.env points at the real file) so the link itself stays a link.
    env_path = args.env_file.expanduser().resolve()
    lines = env_path.read_text().splitlines(keepends=True) if env_path.exists() else []
    try:
        updates, notes = plan(creds, lines)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not updates:
        print("Nothing to copy: the credentials file has none of the expected settings.")
        return 1

    action = "Would set" if args.dry_run else "Set"
    print(f"{action} in {env_path}: {', '.join(updates)}")
    for note in notes:
        print(f"note: {note}")
    if not args.dry_run:
        write_private(env_path, apply(lines, updates))
    return 0


if __name__ == "__main__":
    sys.exit(main())
