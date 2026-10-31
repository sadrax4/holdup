"""Local SQLite store for what should not live on-chain.

Raw message text never goes on-chain. The store keeps the little per-hold
context the UI needs, the evidence documents whose hashes are on-chain,
verdict summaries, transaction ids, guardian invites and an event log.
Accounts and keys are re-derived from the server secret and holds live on
the chain, so losing this file loses history, not money.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

INVITE_TTL_MS = 24 * 60 * 60 * 1000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_key     TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL UNIQUE,
    chat_ref     TEXT,
    display_name TEXT NOT NULL DEFAULT '',
    created_ms   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
    code       TEXT PRIMARY KEY,
    user_key   TEXT NOT NULL,
    created_ms INTEGER NOT NULL,
    expires_ms INTEGER NOT NULL,
    used_ms    INTEGER
);
CREATE TABLE IF NOT EXISTS contexts (
    hold_id    INTEGER PRIMARY KEY,
    summary    TEXT NOT NULL,
    created_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    hold_id       INTEGER NOT NULL,
    evidence_json TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    created_ms    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS evidence_by_hold ON evidence (hold_id);
CREATE INDEX IF NOT EXISTS evidence_by_hash ON evidence (evidence_hash);
CREATE TABLE IF NOT EXISTS verdicts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    hold_id    INTEGER NOT NULL,
    verdict    TEXT NOT NULL,
    created_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS verdicts_by_hold ON verdicts (hold_id);
CREATE TABLE IF NOT EXISTS txs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    hold_id    INTEGER,
    label      TEXT NOT NULL,
    tx_hash    TEXT NOT NULL,
    created_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS txs_by_hold ON txs (hold_id);
CREATE TABLE IF NOT EXISTS mcp_calls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    hold_id    INTEGER NOT NULL,
    tool       TEXT NOT NULL,
    args       TEXT NOT NULL,
    created_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS mcp_calls_by_hold ON mcp_calls (hold_id);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    data       TEXT NOT NULL,
    created_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_kind ON events (kind);
CREATE TABLE IF NOT EXISTS otari_keys (
    user_key    TEXT PRIMARY KEY,
    key_id      TEXT NOT NULL,
    secret_blob BLOB,
    created_ms  INTEGER NOT NULL
);
"""


class EvidenceRecord(NamedTuple):
    evidence_json: str
    evidence_hash: str


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class Store:
    """Thread-safe SQLite store. Use ``":memory:"`` in tests."""

    def __init__(
        self, path: str, *, secret: str | None = None, clock: Callable[[], int] | None = None
    ) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._cipher = _secret_cipher(secret) if secret else None

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, params)

    def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, params).fetchone()

    # ------------------------------------------------------------------
    # users
    # ------------------------------------------------------------------

    def upsert_user(
        self, user_key: str, account_id: str, chat_ref: str | None = None, display_name: str = ""
    ) -> None:
        self._exec(
            """
            INSERT INTO users (user_key, account_id, chat_ref, display_name, created_ms)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (user_key) DO UPDATE SET
                chat_ref = COALESCE(excluded.chat_ref, users.chat_ref),
                display_name = CASE WHEN excluded.display_name != ''
                                    THEN excluded.display_name ELSE users.display_name END
            """,
            (user_key, account_id, chat_ref, display_name or "", self._clock()),
        )

    def get_user(self, user_key: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM users WHERE user_key = ?", (user_key,))
        return dict(row) if row else None

    def user_by_account(self, account_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM users WHERE account_id = ?", (account_id,))
        return dict(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._all("SELECT * FROM users ORDER BY created_ms")]

    def count_users(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM users")
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------
    # guardian invites (one-time deep-link codes)
    # ------------------------------------------------------------------

    def create_invite(self, user_key: str, ttl_ms: int = INVITE_TTL_MS) -> str:
        code = secrets.token_urlsafe(12)
        now = self._clock()
        self._exec(
            "INSERT INTO invites (code, user_key, created_ms, expires_ms) VALUES (?, ?, ?, ?)",
            (code, user_key, now, now + ttl_ms),
        )
        return code

    def consume_invite(self, code: str) -> str | None:
        """Mark an invite used and return who created it; None if unknown, used or expired."""
        now = self._clock()
        with self._lock:
            row = self._db.execute(
                "UPDATE invites SET used_ms = ? WHERE code = ? AND used_ms IS NULL AND expires_ms > ?"
                " RETURNING user_key",
                (now, code, now),
            ).fetchone()
        return row["user_key"] if row else None

    # ------------------------------------------------------------------
    # per-hold context, evidence and verdicts
    # ------------------------------------------------------------------

    def save_context(self, hold_id: int, summary: dict[str, Any]) -> None:
        self._exec(
            "INSERT OR REPLACE INTO contexts (hold_id, summary, created_ms) VALUES (?, ?, ?)",
            (hold_id, _dumps(summary), self._clock()),
        )

    def get_context(self, hold_id: int) -> dict[str, Any] | None:
        row = self._one("SELECT summary FROM contexts WHERE hold_id = ?", (hold_id,))
        return json.loads(row["summary"]) if row else None

    def save_evidence(self, hold_id: int, evidence_json: str, evidence_hash: str) -> None:
        self._exec(
            "INSERT INTO evidence (hold_id, evidence_json, evidence_hash, created_ms) VALUES (?, ?, ?, ?)",
            (hold_id, evidence_json, evidence_hash, self._clock()),
        )

    def get_evidence(self, hold_id: int) -> EvidenceRecord | None:
        """The latest evidence for a hold: the one whose hash is on-chain."""
        row = self._one(
            "SELECT evidence_json, evidence_hash FROM evidence WHERE hold_id = ? ORDER BY id DESC LIMIT 1",
            (hold_id,),
        )
        return EvidenceRecord(row["evidence_json"], row["evidence_hash"]) if row else None

    def list_evidence(self, hold_id: int) -> list[EvidenceRecord]:
        rows = self._all(
            "SELECT evidence_json, evidence_hash FROM evidence WHERE hold_id = ? ORDER BY id",
            (hold_id,),
        )
        return [EvidenceRecord(r["evidence_json"], r["evidence_hash"]) for r in rows]

    def evidence_by_hash(self, evidence_hash: str) -> tuple[int, str] | None:
        row = self._one(
            "SELECT hold_id, evidence_json FROM evidence WHERE evidence_hash = ? ORDER BY id DESC LIMIT 1",
            (evidence_hash,),
        )
        return (int(row["hold_id"]), row["evidence_json"]) if row else None

    def save_verdict(self, hold_id: int, verdict: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO verdicts (hold_id, verdict, created_ms) VALUES (?, ?, ?)",
            (hold_id, _dumps(verdict), self._clock()),
        )

    def get_verdict(self, hold_id: int) -> dict[str, Any] | None:
        row = self._one(
            "SELECT verdict FROM verdicts WHERE hold_id = ? ORDER BY id DESC LIMIT 1", (hold_id,)
        )
        return json.loads(row["verdict"]) if row else None

    def list_verdicts(self, hold_id: int | None = None) -> list[dict[str, Any]]:
        if hold_id is None:
            rows = self._all("SELECT verdict FROM verdicts ORDER BY id")
        else:
            rows = self._all("SELECT verdict FROM verdicts WHERE hold_id = ? ORDER BY id", (hold_id,))
        return [json.loads(r["verdict"]) for r in rows]

    # ------------------------------------------------------------------
    # transactions, MCP calls and the event log
    # ------------------------------------------------------------------

    def record_tx(self, hold_id: int | None, label: str, tx_hash: str) -> None:
        self._exec(
            "INSERT INTO txs (hold_id, label, tx_hash, created_ms) VALUES (?, ?, ?, ?)",
            (hold_id, label, tx_hash, self._clock()),
        )

    def get_txs(self, hold_id: int) -> list[tuple[str, str]]:
        rows = self._all("SELECT label, tx_hash FROM txs WHERE hold_id = ? ORDER BY id", (hold_id,))
        return [(r["label"], r["tx_hash"]) for r in rows]

    def record_mcp_call(self, hold_id: int, tool: str, args: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO mcp_calls (hold_id, tool, args, created_ms) VALUES (?, ?, ?, ?)",
            (hold_id, tool, _dumps(args), self._clock()),
        )

    def get_mcp_calls(self, hold_id: int) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT tool, args, created_ms FROM mcp_calls WHERE hold_id = ? ORDER BY id", (hold_id,)
        )
        return [
            {"tool": r["tool"], "args": json.loads(r["args"]), "created_ms": r["created_ms"]}
            for r in rows
        ]

    def log_event(self, kind: str, data: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO events (kind, data, created_ms) VALUES (?, ?, ?)",
            (kind, _dumps(data), self._clock()),
        )

    def events(self, kind: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Newest first."""
        if kind is None:
            rows = self._all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self._all(
                "SELECT * FROM events WHERE kind = ? ORDER BY id DESC LIMIT ?", (kind, limit)
            )
        return [
            {"kind": r["kind"], "data": json.loads(r["data"]), "created_ms": r["created_ms"]}
            for r in rows
        ]

    def count_events(self, kind: str) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM events WHERE kind = ?", (kind,))
        return int(row["n"]) if row else 0

    def count_event_holds(self, *kinds: str) -> int:
        """How many different holds have at least one event of these kinds."""
        marks = ", ".join("?" for _ in kinds)
        row = self._one(
            f"SELECT COUNT(DISTINCT json_extract(data, '$.hold_id')) AS n FROM events WHERE kind IN ({marks})",
            kinds,
        )
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------
    # per-user Otari keys
    # ------------------------------------------------------------------

    def save_otari_key(self, user_key: str, key_id: str, secret: str | None = None) -> None:
        """Store a user's Otari key id, and its secret encrypted when the store has a secret."""
        blob = None
        if secret is not None:
            if self._cipher is None:
                raise RuntimeError("store opened without a secret; keep Otari key secrets in the environment")
            nonce = os.urandom(12)
            blob = nonce + self._cipher.encrypt(nonce, secret.encode(), user_key.encode())
        self._exec(
            "INSERT OR REPLACE INTO otari_keys (user_key, key_id, secret_blob, created_ms) VALUES (?, ?, ?, ?)",
            (user_key, key_id, blob, self._clock()),
        )

    def get_otari_key(self, user_key: str) -> tuple[str, str | None] | None:
        row = self._one("SELECT key_id, secret_blob FROM otari_keys WHERE user_key = ?", (user_key,))
        if row is None:
            return None
        blob = row["secret_blob"]
        if blob is None or self._cipher is None:
            return row["key_id"], None
        try:
            plain = self._cipher.decrypt(blob[:12], blob[12:], user_key.encode())
        except InvalidTag:
            return row["key_id"], None
        return row["key_id"], plain.decode()


def _secret_cipher(secret: str) -> AESGCM:
    key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=b"holdup-store-v1", info=b"otari-key-secrets"
    ).derive(secret.encode())
    return AESGCM(key)
