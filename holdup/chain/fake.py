"""In-memory HoldVault with the contract's rules, error codes and events.

``HoldVaultModel`` mirrors the Rust contract method by method: synchronous,
with an explicit predecessor account and the same checks in the same order.
Every check runs before any state changes, so a failed call leaves the state
untouched, as a reverted transaction would. Views return the contract's JSON
shapes (U64/U128 as strings), which the adapter parses with the same
``from_view`` helpers the NEAR adapter uses.

``FakeVault`` wraps the model as an async ``VaultPort`` that acts as the user,
the oracle, the keeper or the treasury, like ``NearVault`` does on a real
network, and returns ``TxResult`` objects with simulated transaction ids. It
also models access keys, so the offline proof and attack demos can show the
same rejections nearcore gives for restricted keys.
"""

from __future__ import annotations

import functools
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import base58
from near import Ed25519KeyPair, is_valid_account_id

from ..config import Settings
from ..models import (
    NEAR,
    Hold,
    HoldStatus,
    PayeeStats,
    SendResult,
    TxResult,
    UserAccount,
    UserView,
    VaultConfig,
)
from . import errors as E
from .errors import VaultError
from .events import event_log, make_event
from .keys import KEEPER_METHODS, ORACLE_METHODS, USER_METHODS
from .profiles import YOCTO_PER_NEAR, config_input, vault_config

VERSION = "1.0.0"
DAY_MS = 86_400_000
MAX_EVIDENCE_BYTES = 4096
MAX_MEMO_HASH_BYTES = 64
MAX_TRUSTED = 50
MAX_ORACLES = 3
MAX_TOKENS = 4
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 100
U32_MAX = 2**32 - 1

STORAGE_BYTE_COST = 10**19  # yoctoNEAR per byte, as on NEAR mainnet and testnet
FT_STORAGE_DEPOSIT = 1_250 * 10**18  # 0.00125 NEAR attached to storage_deposit before a token payout
# Storage a hold can still add after send: evidence hash, settled time, canceller, the payee's
# flag and report times, and a new reporter entry (32-byte key + 40 bytes per storage record).
HOLD_SETTLEMENT_BYTES = 32 + 8 + 4 + 64 + 8 + 8 + (32 + 40)
HOLD_STORAGE_FEE = 15 * 10**21  # 0.015 NEAR from the sender's NEAR balance for every hold
ENTRY_STORAGE_FEE = 10**21  # 0.001 NEAR for each new trusted payee and each guardian change
MAX_ACTIVE_HOLDS_LIMIT = 100
MAX_DURATION_MS = 365 * DAY_MS

# Approximate storage footprint of each record in bytes, for the solvency model.
CODE_BYTES = 150_000
STATE_BYTES = 1_000
USER_BYTES = 200
ASSET_ENTRY_BYTES = 80  # balance and instant-usage entry per configured asset
HOLD_BYTES = 300
TRUSTED_BYTES = 110
GUARDIAN_BYTES = 90
PAYEE_STATS_BYTES = 90

DEMO_START_MS = int(datetime(2026, 11, 2, 9, 0, tzinfo=UTC).timestamp() * 1000)


def wall_clock_ms() -> int:
    return int(time.time() * 1000)


class SimClock:
    """A manual millisecond clock for tests and the offline demo."""

    def __init__(self, start_ms: int = DEMO_START_MS) -> None:
        self.ms = start_ms

    def __call__(self) -> int:
        return self.ms

    def advance(self, ms: int) -> int:
        self.ms += ms
        return self.ms


def _fail(code: str, message: str) -> VaultError:
    return VaultError(code, message)


def _is_self_or_parent(caller: str, account_id: str) -> bool:
    """Whether ``caller`` is ``account_id`` itself or the account that created it."""
    return caller == account_id or ("." in account_id and account_id.split(".", 1)[1] == caller)


def _require_account_id(value: Any, what: str) -> str:
    if not isinstance(value, str) or not is_valid_account_id(value):
        raise _fail(E.E_BAD_ARGS, f"{what} is not a valid account id: {value!r}")
    return value


def _validate_config(config: VaultConfig, vault_id: str) -> None:
    def bad(message: str) -> VaultError:
        return _fail(E.E_BAD_CONFIG, message)

    if not 1 <= len(config.oracles) <= MAX_ORACLES:
        raise bad("between 1 and 3 oracles are required")
    if len(config.tokens) > MAX_TOKENS:
        raise bad("at most 4 tokens are allowed")
    for account in [*config.oracles, *config.tokens]:
        _require_account_id(account, "oracle or token")
    if len(set(config.oracles)) != len(config.oracles):
        raise bad("duplicate oracle")
    if len(set(config.tokens)) != len(config.tokens):
        raise bad("duplicate token")
    if vault_id in config.oracles:
        raise bad("the vault cannot be its own oracle")
    if NEAR in config.tokens or vault_id in config.tokens:
        raise bad("a token cannot be named near or be the vault")
    if config.new_payee_hold_ms <= 0:
        raise bad("new_payee_hold_ms must be positive")
    if config.max_hold_ms < config.new_payee_hold_ms:
        raise bad("max_hold_ms must be at least new_payee_hold_ms")
    if max(config.max_hold_ms, config.trust_delay_ms, config.guardian_delay_ms) > MAX_DURATION_MS:
        raise bad("durations are limited to one year")
    if not 1 <= config.max_active_holds <= MAX_ACTIVE_HOLDS_LIMIT:
        raise bad("max_active_holds must be between 1 and 100")
    for asset in config.instant_caps:
        if asset != NEAR and asset not in config.tokens:
            raise bad(f"instant cap for unknown asset {asset}")


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``json.loads`` hook that refuses duplicate keys, as the contract's serde parser does."""
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _settlement_reserve(asset: str) -> int:
    """NEAR set aside when a hold is created so it can always settle without new funds."""
    return HOLD_SETTLEMENT_BYTES * STORAGE_BYTE_COST + _payout_deposit(asset)


def _payout_deposit(asset: str) -> int:
    return 0 if asset == NEAR else FT_STORAGE_DEPOSIT + 1


@dataclass
class _User:
    account_id: str
    registered_ms: int
    balances: dict[str, int]
    guardian: str | None = None
    pending: tuple[str | None, int] | None = None  # (guardian, effective_ms)
    trusted: dict[str, int] = field(default_factory=dict)  # payee -> effective_ms
    active_holds: list[int] = field(default_factory=list)  # oldest first
    sent: list[int] = field(default_factory=list)  # every hold this user created
    instant_used: dict[str, tuple[int, int]] = field(default_factory=dict)  # asset -> (day, amount)

    def guardian_at(self, now: int) -> tuple[str | None, tuple[str | None, int] | None]:
        """Effective guardian and pending change at ``now``, applying a due change lazily."""
        if self.pending is not None and self.pending[1] <= now:
            return self.pending[0], None
        return self.guardian, self.pending

    def settle_guardian(self, now: int) -> None:
        self.guardian, self.pending = self.guardian_at(now)

    def instant_used_on(self, asset: str, day: int) -> int:
        used_day, used = self.instant_used.get(asset, (day, 0))
        return used if used_day == day else 0


@dataclass
class World:
    """Accounts and token ledgers outside the vault: what payouts touch."""

    near: dict[str, int] = field(default_factory=dict)  # existing accounts -> NEAR balance
    created_ms: dict[str, int] = field(default_factory=dict)
    tokens: dict[tuple[str, str], int] = field(default_factory=dict)  # (token, account) -> balance
    token_registered: set[tuple[str, str]] = field(default_factory=set)
    rejecting: set[str] = field(default_factory=set)  # payouts to these accounts fail

    def create(self, account_id: str, *, near: int = 0, created_ms: int) -> None:
        self.near.setdefault(account_id, near)
        self.created_ms.setdefault(account_id, created_ms)

    def exists(self, account_id: str) -> bool:
        return account_id in self.near


class HoldVaultModel:
    """The HoldVault contract, method by method, in memory."""

    def __init__(
        self,
        config: VaultConfig,
        *,
        vault_id: str,
        clock: Callable[[], int],
        reserve: int = 5 * YOCTO_PER_NEAR,
        code_bytes: int = CODE_BYTES,
    ) -> None:
        _validate_config(config, vault_id)
        self.vault_id = vault_id
        self.config = replace(
            config,
            oracles=list(config.oracles),
            tokens=list(config.tokens),
            instant_caps=dict(config.instant_caps),
            version=VERSION,
        )
        self.clock = clock
        self.users: dict[str, _User] = {}
        self.holds: dict[int, Hold] = {}
        self.active: set[int] = set()
        self.stats: dict[str, PayeeStats] = {}
        self.reporters: set[tuple[str, str]] = set()  # (payee, sender): each sender reports a payee once
        self.next_hold_id = 0
        self.account_balance = reserve
        self.near_liabilities = 0
        self.settlement_reserve = 0
        self.storage_usage = code_bytes + STATE_BYTES
        self.world = World()
        self.async_payouts = False  # True: external payouts wait for settle_payouts()
        self.pending_payouts: list[int] = []
        self.events: list[dict[str, Any]] = []
        self._call_events: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def now(self) -> int:
        return int(self.clock())

    @property
    def assets(self) -> list[str]:
        return [NEAR, *self.config.tokens]

    def take_events(self) -> list[dict[str, Any]]:
        """Events emitted since the last call to this method."""
        events, self._call_events = self._call_events, []
        return events

    def _emit(self, name: str, data: dict[str, Any]) -> None:
        event = make_event(name, data)
        self.events.append(event)
        self._call_events.append(event)

    def _user(self, account_id: str) -> _User:
        user = self.users.get(account_id)
        if user is None:
            raise _fail(E.E_NOT_REGISTERED, f"{account_id} is not registered")
        return user

    def _hold(self, hold_id: int) -> Hold:
        hold = self.holds.get(hold_id)
        if hold is None:
            raise _fail(E.E_HOLD_NOT_FOUND, f"hold {hold_id} does not exist")
        return hold

    def _require_active(self, hold: Hold) -> None:
        if hold.status != HoldStatus.HELD:
            raise _fail(E.E_HOLD_NOT_ACTIVE, "hold is not held")

    def _payee_stats(self, account_id: str) -> PayeeStats:
        if account_id not in self.stats:
            self.storage_usage += PAYEE_STATS_BYTES
        return self.stats.setdefault(account_id, PayeeStats())

    def _require_solvent(self, *, storage: int = 0, balance: int = 0, liabilities: int = 0, reserve: int = 0) -> None:
        """The check the contract runs at the end of methods that add storage or obligations."""
        required = (
            self.near_liabilities + liabilities
            + self.settlement_reserve + reserve
            + (self.storage_usage + storage) * STORAGE_BYTE_COST
        )
        if self.account_balance + balance < required:
            raise _fail(E.E_INSOLVENT, "the vault reserve would no longer cover balances and storage")

    def _hold_event(self, hold: Hold, **extra: Any) -> dict[str, Any]:
        return {
            "hold_id": str(hold.id),
            "sender": hold.sender,
            "payee": hold.payee,
            "asset": hold.asset,
            "amount": str(hold.amount),
            **extra,
        }

    def _close_active(self, hold: Hold) -> None:
        self.active.discard(hold.id)
        self.users[hold.sender].active_holds.remove(hold.id)

    def _credit(self, account_id: str, asset: str, amount: int) -> int:
        balances = self.users[account_id].balances
        balances[asset] = balances.get(asset, 0) + amount
        return balances[asset]

    @staticmethod
    def _require_fee(user: _User, fee: int, what: str) -> None:
        if user.balances.get(NEAR, 0) < fee:
            raise _fail(E.E_INSUFFICIENT_BALANCE, f"{what} needs {fee} yoctoNEAR of vault balance for its storage")

    def _take_fee(self, user: _User, fee: int) -> None:
        """A storage fee leaves the user's balance and the liabilities; the vault keeps it."""
        user.balances[NEAR] -= fee
        self.near_liabilities -= fee

    # ------------------------------------------------------------------
    # deposits
    # ------------------------------------------------------------------

    def deposit_near_for(self, predecessor: str, account_id: str, attached: int) -> str:
        """Payable. Registers ``account_id`` on its first deposit; the fee stays as reserve.

        Anyone may deposit for a registered account, but only the account itself or its
        parent may register it: registration turns later payouts to it into vault credits.
        """
        _require_account_id(account_id, "account_id")
        now = self.now()
        fee = self.config.registration_fee
        newly_registered = account_id not in self.users
        if newly_registered:
            if not _is_self_or_parent(predecessor, account_id):
                raise _fail(
                    E.E_NOT_REGISTERED,
                    f"{account_id} is not registered; only it or its parent account can register it",
                )
            if attached < fee:
                raise _fail(E.E_FEE_TOO_LOW, f"registration needs at least {fee} yoctoNEAR")
            credit = attached - fee
            growth = USER_BYTES + ASSET_ENTRY_BYTES * len(self.assets)
            self._require_solvent(storage=growth, balance=attached, liabilities=credit)
            self.users[account_id] = _User(account_id, now, {asset: 0 for asset in self.assets})
            self.storage_usage += growth
        else:
            credit = attached
        self.account_balance += attached
        self.near_liabilities += credit
        balance = self._credit(account_id, NEAR, credit)
        if newly_registered:
            self._emit(
                "user_registered",
                {"account_id": account_id, "registered_ms": str(now), "fee": str(fee)},
            )
        if credit:
            self._emit(
                "deposit",
                {"account_id": account_id, "asset": NEAR, "amount": str(credit), "balance": str(balance)},
            )
        return str(balance)

    def ft_on_transfer(self, predecessor: str, sender_id: str, amount: int, msg: str) -> str:
        """NEP-141 receiver. Returns the amount the token gives back to ``sender_id``."""
        if predecessor not in self.config.tokens:
            raise _fail(E.E_NOT_TOKEN, "this token is not accepted by the vault")
        target = sender_id
        if msg:
            try:
                parsed = json.loads(msg, object_pairs_hook=_unique_keys)
            except ValueError:
                parsed = None
            if (
                not isinstance(parsed, dict)
                or set(parsed) != {"credit_to"}
                or not isinstance(parsed["credit_to"], str)
                or not is_valid_account_id(parsed["credit_to"])
            ):
                raise _fail(E.E_BAD_MSG, 'msg must be empty or {"credit_to":"<account>"}')
            target = parsed["credit_to"]
        if target not in self.users:
            return str(amount)
        balance = self._credit(target, predecessor, amount)
        self._emit(
            "deposit",
            {"account_id": target, "asset": predecessor, "amount": str(amount), "balance": str(balance)},
        )
        return "0"

    # ------------------------------------------------------------------
    # payments
    # ------------------------------------------------------------------

    def send(
        self, predecessor: str, payee: str, asset: str, amount: int, memo_hash: str | None = None
    ) -> dict[str, Any]:
        _require_account_id(payee, "payee")
        user = self._user(predecessor)
        if amount <= 0:
            raise _fail(E.E_ZERO_AMOUNT, "amount must be positive")
        if asset not in self.assets:
            raise _fail(E.E_UNKNOWN_ASSET, f"{asset} is not accepted by this vault")
        if payee in (predecessor, self.vault_id):
            raise _fail(E.E_SELF_PAYMENT, "cannot pay yourself or the vault")
        near_needed = HOLD_STORAGE_FEE + (amount if asset == NEAR else 0)
        if user.balances.get(asset, 0) < amount or user.balances.get(NEAR, 0) < near_needed:
            raise _fail(E.E_INSUFFICIENT_BALANCE, "balance is lower than the amount plus the hold storage fee in NEAR")
        if len(user.active_holds) >= self.config.max_active_holds:
            raise _fail(E.E_TOO_MANY_HOLDS, "too many held payments; cancel one or wait for a release")
        if memo_hash is not None and len(memo_hash.encode()) > MAX_MEMO_HASH_BYTES:
            raise _fail(E.E_MEMO_TOO_LONG, "memo_hash is limited to 64 bytes")

        now = self.now()
        day = now // DAY_MS
        effective_ms = user.trusted.get(payee)
        instant = (
            effective_ms is not None
            and effective_ms <= now
            and user.instant_used_on(asset, day) + amount <= self.config.instant_caps.get(asset, 0)
        )
        stats_growth = 0 if instant or payee in self.stats else PAYEE_STATS_BYTES
        self._require_solvent(
            storage=HOLD_BYTES + stats_growth, liabilities=-HOLD_STORAGE_FEE, reserve=_settlement_reserve(asset)
        )

        hold = Hold(
            id=self.next_hold_id,
            sender=predecessor,
            payee=payee,
            asset=asset,
            amount=amount,
            created_ms=now,
            release_at_ms=now if instant else now + self.config.new_payee_hold_ms,
            status=HoldStatus.HELD,
            instant=instant,
            memo_hash=memo_hash,
        )
        self.next_hold_id += 1
        self.holds[hold.id] = hold
        self.storage_usage += HOLD_BYTES
        user.balances[asset] -= amount
        self._take_fee(user, HOLD_STORAGE_FEE)
        user.sent.append(hold.id)
        if instant:
            user.instant_used[asset] = (day, user.instant_used_on(asset, day) + amount)
        else:
            user.active_holds.append(hold.id)
            self.active.add(hold.id)
            self._payee_stats(payee).holds += 1
        self.settlement_reserve += _settlement_reserve(asset)
        self._emit(
            "hold_created",
            self._hold_event(
                hold,
                created_ms=str(hold.created_ms),
                release_at_ms=str(hold.release_at_ms),
                instant=instant,
                memo_hash=memo_hash,
                storage_fee=str(HOLD_STORAGE_FEE),
            ),
        )
        settle = self._pay_out(hold) if instant else None
        view = hold.to_view()  # an external instant payout is still "releasing" here
        if settle is not None:
            settle()
        return {"hold": view}

    def cancel(self, predecessor: str, hold_id: int) -> dict[str, Any]:
        hold = self._hold(hold_id)
        if predecessor != hold.sender:
            raise _fail(E.E_NOT_SENDER, "only the sender can cancel this hold")
        return self._cancel(hold, predecessor, "sender")

    def guardian_cancel(self, predecessor: str, hold_id: int) -> dict[str, Any]:
        hold = self._hold(hold_id)
        sender = self.users.get(hold.sender)
        guardian, _ = sender.guardian_at(self.now()) if sender else (None, None)
        if guardian is None or predecessor != guardian:
            raise _fail(E.E_NOT_GUARDIAN, "only the sender's guardian can cancel this hold")
        return self._cancel(hold, predecessor, "guardian")

    def _cancel(self, hold: Hold, caller: str, role: str) -> dict[str, Any]:
        self._require_active(hold)
        self._close_active(hold)
        self._credit(hold.sender, hold.asset, hold.amount)
        now = self.now()
        hold.status = HoldStatus.CANCELLED
        hold.cancelled_by = caller
        hold.settled_ms = now
        self.settlement_reserve -= _settlement_reserve(hold.asset)
        # A cancel after a flag is a report, but each sender counts once per payee.
        report = hold.flagged and (hold.payee, hold.sender) not in self.reporters
        if report:
            self.reporters.add((hold.payee, hold.sender))
        stats = self._payee_stats(hold.payee)
        stats.cancelled += 1
        if report:
            stats.cancelled_after_flag += 1
            stats.last_report_ms = now
        self._emit(
            "hold_cancelled",
            self._hold_event(hold, by=role, cancelled_by=caller, flagged=hold.flagged, report=report),
        )
        return hold.to_view()

    def extend_hold(
        self, predecessor: str, hold_id: int, release_at_ms: int, reason_bits: int, evidence: str
    ) -> dict[str, Any]:
        if not 0 <= reason_bits <= U32_MAX:
            raise _fail(E.E_BAD_ARGS, "reason_bits must fit in a u32")
        if predecessor not in self.config.oracles:
            raise _fail(E.E_NOT_ORACLE, "only an oracle can extend a hold")
        hold = self._hold(hold_id)
        self._require_active(hold)
        if release_at_ms <= hold.release_at_ms:
            raise _fail(E.E_ONLY_LONGER, "a hold can only be made longer")
        if release_at_ms > hold.created_ms + self.config.max_hold_ms:
            raise _fail(E.E_ABOVE_MAX_HOLD, "release_at_ms is past created_ms + max_hold_ms")
        if len(evidence.encode()) > MAX_EVIDENCE_BYTES:
            raise _fail(E.E_EVIDENCE_TOO_LONG, "evidence is limited to 4096 bytes")
        if reason_bits == 0:
            raise _fail(E.E_NO_REASON, "at least one reason bit is required")

        previous = hold.release_at_ms
        hold.release_at_ms = release_at_ms
        hold.reason_bits |= reason_bits
        hold.extensions += 1
        hold.evidence_hash = hashlib.sha256(evidence.encode()).hexdigest()
        if not hold.flagged:
            hold.flagged = True
            stats = self._payee_stats(hold.payee)
            stats.flagged += 1
            stats.last_flag_ms = self.now()
        self._emit(
            "hold_extended",
            self._hold_event(
                hold,
                previous_release_at_ms=str(previous),
                release_at_ms=str(release_at_ms),
                reason_bits=hold.reason_bits,
                evidence_hash=hold.evidence_hash,
                extensions=hold.extensions,
            ),
        )
        return hold.to_view()

    def release(self, predecessor: str, hold_id: int) -> dict[str, Any]:
        """Anyone may release a hold once its time has passed. Returns the settled view."""
        hold = self._hold(hold_id)
        self._require_active(hold)
        if self.now() < hold.release_at_ms:
            raise _fail(E.E_NOT_DUE, f"hold {hold.id} is held until {hold.release_at_ms}")
        self._close_active(hold)
        settle = self._pay_out(hold)
        if settle is not None:
            settle()
        return hold.to_view()

    def _pay_out(self, hold: Hold) -> Callable[[], None] | None:
        """Pay a hold that just left ``held``. Returns the payout callback for external payees."""
        if hold.payee in self.users:
            self._credit(hold.payee, hold.asset, hold.amount)
            hold.status = HoldStatus.RELEASED
            hold.settled_ms = self.now()
            self.settlement_reserve -= _settlement_reserve(hold.asset)
            if not hold.instant:
                self._payee_stats(hold.payee).released += 1
            self._emit("hold_released", self._hold_event(hold, internal=True, instant=hold.instant))
            return None

        hold.status = HoldStatus.RELEASING
        if hold.asset == NEAR:
            self.near_liabilities -= hold.amount
            self.account_balance -= hold.amount
        else:
            self.settlement_reserve -= _payout_deposit(hold.asset)
            key = (hold.asset, hold.payee)
            if key not in self.world.token_registered:  # a registered payee gets the deposit back
                self.world.token_registered.add(key)
                self.account_balance -= FT_STORAGE_DEPOSIT
            self.account_balance -= 1  # the yoctoNEAR ft_transfer requires
        if self.async_payouts:
            self.pending_payouts.append(hold.id)
            return None
        return lambda: self._on_payout(hold)

    def _transfer_succeeds(self, hold: Hold) -> bool:
        world = self.world
        if hold.payee in world.rejecting:
            return False
        if hold.asset == NEAR:
            if not world.exists(hold.payee):
                return False
            world.near[hold.payee] += hold.amount
        else:
            key = (hold.asset, hold.payee)
            world.tokens[key] = world.tokens.get(key, 0) + hold.amount
        return True

    def _on_payout(self, hold: Hold) -> None:
        """The ``on_payout`` callback: settle an external payout once its transfer finished."""
        hold.settled_ms = self.now()
        self.settlement_reserve -= HOLD_SETTLEMENT_BYTES * STORAGE_BYTE_COST
        if self._transfer_succeeds(hold):
            hold.status = HoldStatus.RELEASED
            if not hold.instant:
                self._payee_stats(hold.payee).released += 1
            self._emit("hold_released", self._hold_event(hold, internal=False, instant=hold.instant))
            return
        self._credit(hold.sender, hold.asset, hold.amount)
        if hold.asset == NEAR:  # the failed transfer's deposit comes back as a refund
            self.near_liabilities += hold.amount
            self.account_balance += hold.amount
        hold.status = HoldStatus.FAILED
        self._emit("hold_failed", self._hold_event(hold, refunded_to=hold.sender))

    def settle_payouts(self) -> int:
        """Run the callbacks of external payouts started while ``async_payouts`` was on."""
        pending, self.pending_payouts = self.pending_payouts, []
        for hold_id in pending:
            self._on_payout(self.holds[hold_id])
        return len(pending)

    # ------------------------------------------------------------------
    # trust and guardians
    # ------------------------------------------------------------------

    def add_trusted(self, predecessor: str, payee: str) -> dict[str, Any]:
        """Re-adding a payee keeps its entry: the delay is neither restarted nor shortened."""
        _require_account_id(payee, "payee")
        user = self._user(predecessor)
        if payee in (predecessor, self.vault_id):
            raise _fail(E.E_SELF_PAYMENT, "cannot trust yourself or the vault")
        effective_ms = user.trusted.get(payee)
        fee = 0
        if effective_ms is None:
            if len(user.trusted) >= MAX_TRUSTED:
                raise _fail(E.E_TOO_MANY_TRUSTED, "at most 50 trusted payees")
            fee = ENTRY_STORAGE_FEE
            self._require_fee(user, fee, "a trusted payee")
            self._require_solvent(storage=TRUSTED_BYTES, liabilities=-fee)
            self._take_fee(user, fee)
            effective_ms = self.now() + self.config.trust_delay_ms
            user.trusted[payee] = effective_ms
            self.storage_usage += TRUSTED_BYTES
        self._emit(
            "trust_added",
            {"owner": predecessor, "payee": payee, "effective_ms": str(effective_ms), "storage_fee": str(fee)},
        )
        return {"payee": payee, "effective_ms": str(effective_ms)}

    def remove_trusted(self, predecessor: str, payee: str) -> bool:
        user = self._user(predecessor)
        if user.trusted.pop(payee, None) is None:
            return False
        self.storage_usage -= TRUSTED_BYTES
        self._emit("trust_removed", {"owner": predecessor, "payee": payee})
        return True

    def set_guardian(self, predecessor: str, guardian: str | None) -> dict[str, Any]:
        if guardian is not None:
            _require_account_id(guardian, "guardian")
        user = self._user(predecessor)
        if guardian is not None and guardian in (predecessor, self.vault_id):
            raise _fail(E.E_BAD_GUARDIAN, "you cannot be your own guardian")
        self._require_fee(user, ENTRY_STORAGE_FEE, "a guardian change")
        now = self.now()
        guardian_now, pending_now = user.guardian_at(now)
        growth = GUARDIAN_BYTES if guardian is not None and guardian_now is None and pending_now is None else 0
        self._require_solvent(storage=growth, liabilities=-ENTRY_STORAGE_FEE)
        self._take_fee(user, ENTRY_STORAGE_FEE)
        user.settle_guardian(now)
        if user.guardian is None and user.pending is None:
            effective_ms = now
            user.guardian = guardian
        else:
            effective_ms = now + self.config.guardian_delay_ms
            user.pending = (guardian, effective_ms)
        self.storage_usage += growth
        self._emit(
            "guardian_changed",
            {
                "owner": predecessor,
                "guardian": guardian,
                "effective_ms": str(effective_ms),
                "storage_fee": str(ENTRY_STORAGE_FEE),
            },
        )
        return self._user_view(user, now)

    def veto_guardian_change(self, predecessor: str, owner: str) -> dict[str, Any]:
        """The guardian in charge of ``owner`` drops their pending guardian change."""
        _require_account_id(owner, "owner")
        user = self._user(owner)
        now = self.now()
        guardian, pending = user.guardian_at(now)
        if guardian is None or predecessor != guardian:
            raise _fail(E.E_NOT_GUARDIAN, "only the guardian in charge can veto a guardian change")
        if pending is None:
            raise _fail(E.E_BAD_GUARDIAN, "no guardian change is pending")
        user.guardian, user.pending = guardian, None
        self._emit("guardian_change_vetoed", {"owner": owner, "guardian": guardian, "vetoed": pending[0]})
        return self._user_view(user, now)

    # ------------------------------------------------------------------
    # views (JSON shapes, exactly as the contract returns them)
    # ------------------------------------------------------------------

    def get_config(self) -> dict[str, Any]:
        view = config_input(self.config)
        view["instant_caps"] = dict(sorted(view["instant_caps"].items()))
        return {
            **view,
            "hold_storage_fee": str(HOLD_STORAGE_FEE),
            "entry_storage_fee": str(ENTRY_STORAGE_FEE),
            "version": VERSION,
        }

    def get_hold(self, hold_id: int) -> dict[str, Any] | None:
        hold = self.holds.get(hold_id)
        return hold.to_view() if hold else None

    def get_holds_for(self, account_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Holds sent by ``account_id``, newest first (default 50, at most 100)."""
        user = self.users.get(account_id)
        if user is None:
            return []
        newest_first = list(reversed(user.sent))[: _clamp_limit(limit)]
        return [self.holds[hold_id].to_view() for hold_id in newest_first]

    def get_active_holds(self, account_id: str) -> list[dict[str, Any]]:
        """The sender's holds that are still held, oldest first."""
        user = self.users.get(account_id)
        return [self.holds[hold_id].to_view() for hold_id in user.active_holds] if user else []

    def get_due_holds(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Held payments whose time has passed, earliest release first."""
        now = self.now()
        due = [self.holds[i] for i in self.active if self.holds[i].release_at_ms <= now]
        due.sort(key=lambda hold: (hold.release_at_ms, hold.id))
        return [hold.to_view() for hold in due[: _clamp_limit(limit)]]

    def get_balance(self, account_id: str) -> dict[str, Any]:
        user = self.users.get(account_id)
        balances = user.balances if user else {}
        return {
            "near": str(balances.get(NEAR, 0)),
            "tokens": {token: str(balances.get(token, 0)) for token in sorted(self.config.tokens)},
        }

    def get_user(self, account_id: str) -> dict[str, Any] | None:
        user = self.users.get(account_id)
        return self._user_view(user, self.now()) if user else None

    def _user_view(self, user: _User, now: int) -> dict[str, Any]:
        guardian, pending = user.guardian_at(now)
        return {
            "account_id": user.account_id,
            "registered_ms": str(user.registered_ms),
            "guardian": guardian,
            "pending_guardian": (
                {"guardian": pending[0], "effective_ms": str(pending[1])} if pending else None
            ),
            "trusted": [
                {"payee": payee, "effective_ms": str(ms)} for payee, ms in sorted(user.trusted.items())
            ],
            "active_holds": len(user.active_holds),
        }

    def get_payee_stats(self, account_id: str) -> dict[str, Any]:
        stats = self.stats.get(account_id, PayeeStats())
        return {
            "holds": stats.holds,
            "flagged": stats.flagged,
            "cancelled": stats.cancelled,
            "cancelled_after_flag": stats.cancelled_after_flag,
            "released": stats.released,
            "last_flag_ms": None if stats.last_flag_ms is None else str(stats.last_flag_ms),
            "last_report_ms": None if stats.last_report_ms is None else str(stats.last_report_ms),
        }

    def is_registered(self, account_id: str) -> bool:
        return account_id in self.users

    def get_solvency(self) -> dict[str, str]:
        storage_cost = self.storage_usage * STORAGE_BYTE_COST
        required = self.near_liabilities + self.settlement_reserve + storage_cost
        return {
            "account_balance": str(self.account_balance),
            "locked": "0",
            "near_liabilities": str(self.near_liabilities),
            "settlement_reserve": str(self.settlement_reserve),
            "storage_usage": str(self.storage_usage),
            "storage_cost": str(storage_cost),
            "surplus": str(max(self.account_balance - required, 0)),
        }


def _clamp_limit(limit: int | None) -> int:
    return DEFAULT_LIST_LIMIT if limit is None else max(0, min(limit, MAX_LIST_LIMIT))


# ----------------------------------------------------------------------
# Access keys (enough of nearcore's rules for the proof and attack demos)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class AccessKeyModel:
    public_key: str
    receiver_id: str | None = None  # None means a full-access key
    method_names: tuple[str, ...] = ()
    allowance: int | None = None

    @property
    def full_access(self) -> bool:
        return self.receiver_id is None

    def view(self) -> dict[str, Any]:
        """One entry of a ``view_access_key_list`` result."""
        if self.full_access:
            permission: Any = "FullAccess"
        else:
            permission = {
                "FunctionCall": {
                    "allowance": None if self.allowance is None else str(self.allowance),
                    "receiver_id": self.receiver_id,
                    "method_names": list(self.method_names),
                }
            }
        return {"public_key": self.public_key, "access_key": {"nonce": 0, "permission": permission}}


def _invalid_tx(error: Any) -> dict[str, Any]:
    """An ``INVALID_TRANSACTION`` RPC error payload, shaped like nearcore's."""
    data = {"TxExecutionError": {"InvalidTxError": error}}
    return {
        "name": "HANDLER_ERROR",
        "cause": {"name": "INVALID_TRANSACTION", "info": {}},
        "code": -32000,
        "message": "Server error",
        "data": data,
    }


def placeholder_public_key(account_id: str) -> str:
    """A stable public key for accounts the in-memory world creates."""
    seed = hashlib.sha256(f"holdup:fake-key:{account_id}".encode()).digest()
    return str(Ed25519KeyPair(seed).public_key)


SAMPLE_CODE_HASH = base58.b58encode(hashlib.sha256(b"holdvault sample code").digest()).decode()


# ----------------------------------------------------------------------
# The VaultPort adapter
# ----------------------------------------------------------------------


@dataclass
class SimulatedTx:
    tx_hash: str
    signer_id: str
    receiver_id: str
    method: str
    args: dict[str, Any]
    ok: bool
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


def default_config(root: str) -> VaultConfig:
    settings = Settings(oracle_accounts=[f"oracle.{root}"])
    return vault_config(settings, "demo")


class FakeVault:
    """``VaultPort`` over :class:`HoldVaultModel`, with simulated transaction ids."""

    simulated = True

    def __init__(
        self,
        config: VaultConfig | None = None,
        *,
        vault_id: str = "vault.holdup.testnet",
        clock: Callable[[], int] | None = None,
        keeper_id: str | None = None,
        treasury_id: str | None = None,
        reserve: int = 5 * YOCTO_PER_NEAR,
        user_gas: int = YOCTO_PER_NEAR // 20,
        code_hash: str | None = None,
    ) -> None:
        root = vault_id.split(".", 1)[1] if "." in vault_id else vault_id
        self.clock = clock or wall_clock_ms
        self.model = HoldVaultModel(
            config or default_config(root), vault_id=vault_id, clock=self.clock, reserve=reserve
        )
        self.vault_id = vault_id
        self.oracle_id = self.model.config.oracles[0]
        self.keeper_id = keeper_id or f"keeper.{root}"
        self.treasury_id = treasury_id or root
        self.user_gas = user_gas
        self.code_hash = code_hash or SAMPLE_CODE_HASH
        self.transactions: list[SimulatedTx] = []
        self.keys: dict[str, list[AccessKeyModel]] = {}
        self._seq = 0

        now = self.clock()
        world = self.model.world
        for account in {self.treasury_id, self.keeper_id, *self.model.config.oracles}:
            world.create(account, near=10 * YOCTO_PER_NEAR, created_ms=now)
        world.create(vault_id, near=reserve, created_ms=now)
        for token in self.model.config.tokens:
            world.create(token, near=3 * YOCTO_PER_NEAR, created_ms=now)
        self.keys[self.treasury_id] = [AccessKeyModel(placeholder_public_key(self.treasury_id))]
        self.keys[vault_id] = []
        for oracle in self.model.config.oracles:
            self.keys[oracle] = [
                AccessKeyModel(placeholder_public_key(oracle), vault_id, ORACLE_METHODS)
            ]
        self.keys[self.keeper_id] = [
            AccessKeyModel(placeholder_public_key(self.keeper_id), vault_id, KEEPER_METHODS)
        ]

    @classmethod
    def from_settings(
        cls, settings: Settings, *, profile: str = "demo", clock: Callable[[], int] | None = None, **kwargs: Any
    ) -> FakeVault:
        return cls(
            vault_config(settings, profile),
            vault_id=settings.vault_account,
            clock=clock,
            keeper_id=settings.keeper_account,
            treasury_id=settings.root_account,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # the simulated world around the vault
    # ------------------------------------------------------------------

    def create_account(
        self, account_id: str, *, near: int = YOCTO_PER_NEAR, age_ms: int = 0
    ) -> None:
        """Make an account exist outside the vault (a payee, for instance)."""
        _require_account_id(account_id, "account_id")
        self.model.world.create(account_id, near=near, created_ms=self.clock() - age_ms)

    def reject_payouts_to(self, account_id: str) -> None:
        """Make payouts to ``account_id`` fail, as a rejecting receiver would."""
        self.model.world.rejecting.add(account_id)

    def external_balance(self, account_id: str, asset: str = NEAR) -> int:
        world = self.model.world
        if asset == NEAR:
            return world.near.get(account_id, 0)
        return world.tokens.get((asset, account_id), 0)

    def account_created_ms(self, account_id: str) -> int | None:
        return self.model.world.created_ms.get(account_id)

    # ------------------------------------------------------------------
    # transactions
    # ------------------------------------------------------------------

    def _next_hash(self, *parts: Any) -> str:
        self._seq += 1
        material = json.dumps([self._seq, *parts], sort_keys=True, default=str).encode()
        return base58.b58encode(hashlib.sha256(material).digest()).decode()

    def _run(
        self,
        signer: str,
        method: str,
        args: dict[str, Any],
        call: Callable[[], Any],
        *,
        receiver: str | None = None,
    ) -> TxResult:
        receiver = receiver or self.vault_id
        tx_hash = self._next_hash(signer, receiver, method, args)
        self.model.take_events()
        try:
            value = call()
        except VaultError as exc:
            self.model.take_events()
            exc.tx_hash = tx_hash
            self.transactions.append(
                SimulatedTx(tx_hash, signer, receiver, method, args, ok=False, error=exc.code)
            )
            raise
        events = self.model.take_events()
        self.transactions.append(
            SimulatedTx(tx_hash, signer, receiver, method, args, ok=True, events=events)
        )
        return TxResult(
            tx_hash=tx_hash,
            events=events,
            logs=[event_log(event) for event in events],
            return_value=value,
        )

    # ------------------------------------------------------------------
    # VaultPort: views
    # ------------------------------------------------------------------

    async def get_config(self) -> VaultConfig:
        return VaultConfig.from_view(self.model.get_config())

    async def get_hold(self, hold_id: int) -> Hold | None:
        view = self.model.get_hold(hold_id)
        return Hold.from_view(view) if view else None

    async def get_holds_for(self, account_id: str, limit: int = 50) -> list[Hold]:
        return [Hold.from_view(v) for v in self.model.get_holds_for(account_id, limit)]

    async def get_active_holds(self, account_id: str) -> list[Hold]:
        return [Hold.from_view(v) for v in self.model.get_active_holds(account_id)]

    async def get_due_holds(self, limit: int = 50) -> list[Hold]:
        return [Hold.from_view(v) for v in self.model.get_due_holds(limit)]

    async def get_balance(self, account_id: str) -> dict[str, int]:
        return flatten_balance(self.model.get_balance(account_id))

    async def get_user(self, account_id: str) -> UserView | None:
        view = self.model.get_user(account_id)
        return UserView.from_view(view) if view else None

    async def get_payee_stats(self, account_id: str) -> PayeeStats:
        return PayeeStats.from_view(self.model.get_payee_stats(account_id))

    async def account_exists(self, account_id: str) -> bool:
        return self.model.world.exists(account_id)

    async def now_ms(self) -> int:
        return self.model.now()

    async def is_registered(self, account_id: str) -> bool:
        return self.model.is_registered(account_id)

    async def get_solvency(self) -> dict[str, int]:
        return {k: int(v) for k, v in self.model.get_solvency().items()}

    # ------------------------------------------------------------------
    # VaultPort: user calls
    # ------------------------------------------------------------------

    async def send(
        self, user: UserAccount, payee: str, asset: str, amount: int, memo_hash: str | None = None
    ) -> SendResult:
        args = {"payee": payee, "asset": asset, "amount": str(amount), "memo_hash": memo_hash}
        tx = self._run(
            user.account_id,
            "send",
            args,
            lambda: self.model.send(user.account_id, payee, asset, amount, memo_hash),
        )
        return SendResult(hold=Hold.from_view(tx.return_value["hold"]), tx=tx)

    async def cancel(self, user: UserAccount, hold_id: int) -> TxResult:
        return self._run(
            user.account_id,
            "cancel",
            {"hold_id": str(hold_id)},
            lambda: self.model.cancel(user.account_id, hold_id),
        )

    async def guardian_cancel(self, guardian: UserAccount, hold_id: int) -> TxResult:
        return self._run(
            guardian.account_id,
            "guardian_cancel",
            {"hold_id": str(hold_id)},
            lambda: self.model.guardian_cancel(guardian.account_id, hold_id),
        )

    async def add_trusted(self, user: UserAccount, payee: str) -> TxResult:
        return self._run(
            user.account_id,
            "add_trusted",
            {"payee": payee},
            lambda: self.model.add_trusted(user.account_id, payee),
        )

    async def remove_trusted(self, user: UserAccount, payee: str) -> TxResult:
        return self._run(
            user.account_id,
            "remove_trusted",
            {"payee": payee},
            lambda: self.model.remove_trusted(user.account_id, payee),
        )

    async def set_guardian(self, user: UserAccount, guardian: str | None) -> TxResult:
        return self._run(
            user.account_id,
            "set_guardian",
            {"guardian": guardian},
            lambda: self.model.set_guardian(user.account_id, guardian),
        )

    async def veto_guardian_change(self, guardian: UserAccount, owner: str) -> TxResult:
        return self._run(
            guardian.account_id,
            "veto_guardian_change",
            {"owner": owner},
            lambda: self.model.veto_guardian_change(guardian.account_id, owner),
        )

    # ------------------------------------------------------------------
    # VaultPort: oracle, keeper, treasury
    # ------------------------------------------------------------------

    async def extend_hold(
        self, hold_id: int, release_at_ms: int, reason_bits: int, evidence: str
    ) -> TxResult:
        args = {
            "hold_id": str(hold_id),
            "release_at_ms": str(release_at_ms),
            "reason_bits": reason_bits,
            "evidence": evidence,
        }
        return self._run(
            self.oracle_id,
            "extend_hold",
            args,
            lambda: self.model.extend_hold(self.oracle_id, hold_id, release_at_ms, reason_bits, evidence),
        )

    async def release(self, hold_id: int) -> TxResult:
        return self._run(
            self.keeper_id,
            "release",
            {"hold_id": str(hold_id)},
            lambda: self.model.release(self.keeper_id, hold_id),
        )

    async def onboard_user(self, user: UserAccount, demo_funds: dict[str, int]) -> list[TxResult]:
        """Create, register and fund a user, like the treasury does on chain."""
        results: list[TxResult] = []
        world = self.model.world
        if not world.exists(user.account_id):
            world.create(user.account_id, near=self.user_gas, created_ms=self.clock())
            self.keys[user.account_id] = [
                AccessKeyModel(user.public_key, self.vault_id, USER_METHODS)
            ]
            results.append(
                self._run(
                    self.treasury_id,
                    "create_account",
                    {"public_key": user.public_key, "methods": list(USER_METHODS)},
                    lambda: None,
                    receiver=user.account_id,
                )
            )
        if not self.model.is_registered(user.account_id):
            attached = self.model.config.registration_fee + demo_funds.get(NEAR, 0)
            results.append(
                self._run(
                    self.treasury_id,
                    "deposit_near_for",
                    {"account_id": user.account_id, "attached": str(attached)},
                    lambda: self.model.deposit_near_for(self.treasury_id, user.account_id, attached),
                )
            )
        for token, amount in demo_funds.items():
            if token == NEAR or amount <= 0 or token not in self.model.config.tokens:
                continue
            msg = json.dumps({"credit_to": user.account_id}, separators=(",", ":"))
            results.append(
                self._run(
                    self.treasury_id,
                    "ft_transfer_call",
                    {"receiver_id": self.vault_id, "amount": str(amount), "msg": msg},
                    functools.partial(self.model.ft_on_transfer, token, self.treasury_id, amount, msg),
                    receiver=token,
                )
            )
        return results

    # ------------------------------------------------------------------
    # RPC-shaped data for the proof and verification pages
    # ------------------------------------------------------------------

    def view_access_key_list(self, account_id: str) -> dict[str, Any]:
        return {
            "keys": [key.view() for key in self.keys.get(account_id, [])],
            "block_height": 1,
            "block_hash": "11111111111111111111111111111111",
        }

    def view_account(self, account_id: str) -> dict[str, Any]:
        world = self.model.world
        code_hash = self.code_hash if account_id == self.vault_id else "11111111111111111111111111111111"
        balance = self.model.account_balance if account_id == self.vault_id else world.near.get(account_id, 0)
        return {
            "amount": str(balance),
            "locked": "0",
            "code_hash": code_hash,
            "storage_usage": self.model.storage_usage if account_id == self.vault_id else 182,
            "storage_paid_at": 0,
            "block_height": 1,
            "block_hash": "11111111111111111111111111111111",
        }

    def check_transaction(
        self, signer_id: str, receiver_id: str, actions: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Apply nearcore's access-key rules; return the RPC error payload or None if allowed."""
        keys = self.keys.get(signer_id, [])
        if not keys:
            return _invalid_tx(
                {"InvalidAccessKeyError": {"AccessKeyNotFound": {"account_id": signer_id, "public_key": ""}}}
            )
        key = keys[0]
        if key.full_access:
            return None
        if len(actions) != 1 or "FunctionCall" not in actions[0]:
            return _invalid_tx({"InvalidAccessKeyError": "RequiresFullAccess"})
        call = actions[0]["FunctionCall"]
        if int(call.get("deposit", 0)) > 0:
            return _invalid_tx({"InvalidAccessKeyError": "DepositWithFunctionCall"})
        if receiver_id != key.receiver_id:
            return _invalid_tx(
                {
                    "InvalidAccessKeyError": {
                        "ReceiverMismatch": {"tx_receiver": receiver_id, "ak_receiver": key.receiver_id}
                    }
                }
            )
        if key.method_names and call.get("method_name") not in key.method_names:
            return _invalid_tx(
                {"InvalidAccessKeyError": {"MethodNameMismatch": {"method_name": call.get("method_name")}}}
            )
        return None


def flatten_balance(view: dict[str, Any]) -> dict[str, int]:
    """``{"near": ..., "tokens": {...}}`` to ``{"near": int, "<token>": int}``."""
    out = {NEAR: int(view.get("near", 0))}
    for token, amount in (view.get("tokens") or {}).items():
        out[token] = int(amount)
    return out


def near_to_yocto(text: str) -> int:
    return int(Decimal(text) * YOCTO_PER_NEAR)
