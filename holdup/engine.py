"""Channel-agnostic payment engine: the Telegram bot, the web console and the demo drive it.

A payment to a payee the sender has not trusted yet becomes a hold:

1. ``pay`` sends it to the vault, where it waits, and tells the sender.
2. A background task runs the fast rules. If they flag, the oracle adds an
   hour within seconds (the provisional brake).
3. The full check runs (the Otari pipeline when configured) and its verdict
   goes through the oracle. If the check fails or is too slow, the hold is
   extended fail-closed, one hour per failure up to six hours, and retried.
   If an extension itself fails (the RPC node is down, say), it is retried on
   every keeper tick and the keeper does not release that hold meanwhile.
4. The keeper releases the payment when its time comes, unless the sender or
   their guardian cancelled it first.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import io
import itertools
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime
from typing import Any, TypeGuard

from near import is_valid_account_id

from . import messages
from .chain import errors as E
from .chain import links
from .chain.budget import OnboardingCost, near_to_yocto, onboarding_cost
from .chain.errors import VaultError
from .chain.events import first_item
from .chain.keys import derive_user
from .chain.treasury import TREASURY_EMPTY, Treasury
from .config import OFFLINE_SECRET, Settings
from .interfaces import Notifier, RiskPort, VaultPort
from .models import (
    NEAR,
    Finding,
    Hold,
    HoldStatus,
    PaymentContext,
    PaymentOutcome,
    Reason,
    StepRecord,
    TxResult,
    UserAccount,
    Verdict,
    WhyView,
    format_amount,
    reasons_from_bits,
)
from .store import Store
from .workers.keeper import Keeper
from .workers.oracle import Oracle, OracleResult

log = logging.getLogger(__name__)

MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS
CONTEXT_TTL_MS = 15 * MINUTE_MS
PROVISIONAL_HOLD_MS = HOUR_MS
FAIL_CLOSED_STEP_MS = HOUR_MS
FAIL_CLOSED_MAX_MS = 6 * HOUR_MS
MAX_FAILURES = FAIL_CLOSED_MAX_MS // FAIL_CLOSED_STEP_MS
RETRY_BASE_MS = 5 * MINUTE_MS
RETRY_MAX_MS = 30 * MINUTE_MS
CONTEXT_TEXT_MAX = 8_000
PREVIEW_CHARS = 200
FAILURE_REASONS = (Reason.AI_UNAVAILABLE, Reason.BUDGET_EXHAUSTED)

CSV_COLUMNS = [
    "hold_id",
    "created_utc",
    "payee",
    "asset",
    "amount",
    "status",
    "instant",
    "flagged",
    "reasons",
    "release_at_utc",
    "settled_utc",
    "cancelled_by",
    "evidence_hash",
    "hold_page",
    "transactions",
    "explorer_links",
]


def _wall_clock_ms() -> int:
    return int(time.time() * 1000)


def _iso(ms: int | None) -> str:
    if ms is None:
        return ""
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _flags(verdict: Verdict | None) -> TypeGuard[Verdict]:
    return verdict is not None and verdict.brake and verdict.reason_bits != 0


def _is_failure(finding: Finding) -> bool:
    return finding.reason in FAILURE_REASONS


def _adds_nothing(fast: Verdict | None, full: Verdict) -> bool:
    """The full check found only what the quick brake already put on chain, and asks no longer."""
    return _flags(fast) and full.reason_bits & ~fast.reason_bits == 0 and full.hold_for_ms <= PROVISIONAL_HOLD_MS


def _keep_rules_decision(fast: Verdict | None, full: Verdict | None) -> Verdict | None:
    """The AI step can add to a deterministic rules brake, never weaken it.

    Only matters when the rules asked for more than the provisional hour (a
    reported payee, a secret request): that wait stands even if the AI found nothing.
    """
    if full is None or not _flags(fast) or fast.hold_for_ms <= PROVISIONAL_HOLD_MS:
        return full
    extra = [f for f in fast.findings if f not in full.findings]
    hold_for = max(fast.hold_for_ms, full.hold_for_ms if _flags(full) else 0)
    return replace(full, brake=True, hold_for_ms=hold_for, findings=[*full.findings, *extra])


def _is_budget_error(exc: BaseException) -> bool:
    name = type(exc).__name__
    return "Budget" in name or "InsufficientFunds" in name


def _describe_failure(exc: BaseException) -> str:
    if _is_budget_error(exc):
        return "the AI budget is used up"
    return f"the AI service failed with {type(exc).__name__}"


def _failure_verdict(hold: Hold, exc: BaseException) -> Verdict | None:
    """A budget error raised by the pipeline is reported like a BUDGET_EXHAUSTED verdict."""
    if not _is_budget_error(exc):
        return None
    finding = Finding(Reason.BUDGET_EXHAUSTED, _describe_failure(exc), source="fail_closed")
    return Verdict(hold_id=hold.id, brake=True, hold_for_ms=HOUR_MS, findings=[finding])


@dataclass
class _Retry:
    """Fail-closed state of one hold whose check keeps failing."""

    hold_id: int
    user_key: str
    ctx: PaymentContext
    base_release_ms: int
    fast: Verdict | None = None
    failures: int = 0
    next_retry_ms: int = 0
    running: bool = False


@dataclass
class _Remembered:
    ctx: PaymentContext
    expires_ms: int


_brake_numbers = itertools.count()


@dataclass
class _Brake:
    """A braking verdict on its way to the chain. Kept while its transaction keeps failing."""

    user_key: str
    hold: Hold
    verdict: Verdict
    kind: str  # "provisional", "final" or "fail_closed"
    release_at_ms: int | None = None  # an absolute target instead of now + verdict.hold_for_ms
    running: bool = False
    number: int = field(default_factory=lambda: next(_brake_numbers))  # later verdicts win


class Engine:
    """The one API the bot, the web console, the CLI and the demo use."""

    fast_timeout_s = 30.0
    full_timeout_s = 300.0
    deadline_margin_ms = 5_000

    def __init__(
        self,
        settings: Settings,
        vault: VaultPort,
        fast_risk: RiskPort,
        full_risk: RiskPort | None,
        store: Store,
        notifier: Notifier,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.settings = settings
        self.vault = vault
        self.fast_risk = fast_risk
        self.full_risk = full_risk
        self.store = store
        self.notifier = notifier
        self.clock = clock or _wall_clock_ms
        self.oracle = Oracle(vault, store, network=settings.network)
        self.simulated = bool(getattr(vault, "simulated", False))
        # Only the in-memory vault may run without HOLDUP_SECRET; real accounts need a real secret.
        self._secret = settings.secret or (OFFLINE_SECRET if self.simulated else "")
        if not self._secret:
            raise ValueError("HOLDUP_SECRET is required to derive user accounts on a real network")
        if self._secret == OFFLINE_SECRET and not self.simulated:
            raise ValueError("HOLDUP_SECRET is the public offline secret; a real network needs its own")
        self._accounts: dict[str, UserAccount] = {}
        self._contexts: dict[str, _Remembered] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._retries: dict[int, _Retry] = {}
        # Brakes whose extend_hold failed, by hold id: retried on every tick, and the keeper does
        # not release these holds. Anyone else can still call release once a hold is due, so the
        # retries race the clock; they start within seconds of the payment.
        self._pending_brakes: dict[int, _Brake] = {}
        self._alerted: dict[int, int] = {}  # hold id -> reason bits the guardian heard about
        self._start_locks: dict[str, asyncio.Lock] = {}
        # The root account pays for every new user; on testnet it refills itself from the faucet.
        self.treasury = Treasury(settings, vault, store, clock=self.clock)

    # ------------------------------------------------------------------
    # identity
    # ------------------------------------------------------------------

    def account_for(self, user_key: str) -> UserAccount:
        """The user's derived account (no chain call)."""
        account = self._accounts.get(user_key)
        if account is None:
            account = derive_user(self._secret, user_key, self.settings.root_account)
            self._accounts[user_key] = account
        row = self.store.get_user(user_key)
        if row and row["display_name"] and row["display_name"] != account.display_name:
            account = replace(account, display_name=row["display_name"])
            self._accounts[user_key] = account
        return account

    async def start(
        self, user_key: str, display_name: str = "", chat_ref: str | None = None
    ) -> tuple[UserAccount, bool]:
        """Create, register and fund the user's account until all of it is done. Returns (account, created).

        ``created`` is True for the call that finished the setup. A setup that stopped half-way
        (the network did not answer, the treasury ran dry, the app restarted) is finished by a
        later call, which skips the steps already done; the store remembers a finished one.
        The treasury pays for it. When it cannot pay for a new user, this raises
        ``VaultError(TREASURY_EMPTY)`` before sending anything (the channels turn that into a
        "try again later"), and on testnet a faucet top-up is already on its way. A step that
        fails after others went through raises ``ONBOARDING_UNFINISHED`` with its own code.
        """
        lock = self._start_locks.setdefault(user_key, asyncio.Lock())
        async with lock:
            account = self.account_for(user_key)
            self.store.upsert_user(user_key, account.account_id, chat_ref, display_name)
            registered = await self.vault.get_user(account.account_id) is not None
            if registered and self.store.onboarded(account.account_id):
                return account, False
            funds = dict(self.settings.demo_funds)
            if registered:
                # Registering paid the NEAR; only a token transfer after it can be missing.
                held = await self.vault.get_balance(account.account_id)
                funds = {asset: amount for asset, amount in funds.items() if asset != NEAR and not held.get(asset)}
            else:
                await self.treasury.check_onboarding(await self.onboarding_cost())
            try:
                txs = await self.vault.onboard_user(account, funds)
            except VaultError as exc:
                if exc.code == TREASURY_EMPTY:
                    log.warning("onboarding %s stopped: %s", account.account_id, exc.message)
                    self.treasury.request_top_up()
                if registered or await self._exists(account.account_id):
                    raise VaultError(E.ONBOARDING_UNFINISHED, exc.code) from exc
                raise
            for tx in txs:
                self.store.record_tx(None, "onboard", tx.tx_hash)
            self.store.log_event("onboard", {"account_id": account.account_id, "txs": [t.tx_hash for t in txs]})
            return self.account_for(user_key), True

    async def _exists(self, account_id: str) -> bool:
        """Whether the account exists now; False when the network cannot tell."""
        try:
            return bool(await self.vault.account_exists(account_id))
        except Exception:  # noqa: BLE001 - only used to word an error that is raised anyway
            return False

    async def onboarding_cost(self) -> OnboardingCost:
        """What one new user costs the treasury: account gas, registration fee, demo NEAR and gas."""
        config = await self.vault.get_config()
        funds = self.settings.demo_funds
        tokens = sum(1 for asset, amount in funds.items() if asset != NEAR and amount > 0 and asset in config.tokens)
        return onboarding_cost(
            vault_id=self.vault.vault_id,
            user_gas=near_to_yocto(self.settings.user_gas_near),
            registration_fee=config.registration_fee,
            demo_near=funds.get(NEAR, 0),
            tokens=tokens,
        )

    async def balance(self, user_key: str) -> dict[str, int]:
        return await self.vault.get_balance(self.account_for(user_key).account_id)

    # ------------------------------------------------------------------
    # payment context
    # ------------------------------------------------------------------

    def remember_context(self, user_key: str, ctx: PaymentContext) -> None:
        """Keep what the user forwarded for 15 minutes; the next ``pay`` uses it."""
        now = self.clock()
        if ctx.received_ms is None:
            ctx = replace(ctx, received_ms=now)
        current = self._contexts.get(user_key)
        if current is not None and current.expires_ms > now:
            ctx = _merge_contexts(current.ctx, ctx)
        self._contexts[user_key] = _Remembered(ctx, now + CONTEXT_TTL_MS)

    def _context_for_payment(self, user_key: str, ctx: PaymentContext | None) -> PaymentContext:
        """An explicit context wins; otherwise the remembered one if it is still fresh."""
        if ctx is not None:
            return replace(ctx, trusted_payees=list(ctx.trusted_payees))
        remembered = self._contexts.get(user_key)
        if remembered is not None and remembered.expires_ms > self.clock():
            return replace(remembered.ctx, trusted_payees=list(remembered.ctx.trusted_payees))
        return PaymentContext()

    def _memo_hash(self, ctx: PaymentContext) -> str | None:
        """A keyed commitment to the context, so the chain links a hold to it without revealing it."""
        parts: list[bytes] = []
        if ctx.forwarded_text:
            parts.append(ctx.forwarded_text.encode())
        if ctx.image_bytes:
            parts.append(hashlib.sha256(ctx.image_bytes).digest())
        if not parts:
            return None
        return hmac.new(
            self._secret.encode(), b"holdup:memo:" + b"\x00".join(parts), hashlib.sha256
        ).hexdigest()

    # ------------------------------------------------------------------
    # payments
    # ------------------------------------------------------------------

    async def pay(
        self,
        user_key: str,
        payee: str,
        asset_id: str,
        amount: int,
        ctx: PaymentContext | None = None,
    ) -> PaymentOutcome:
        """Send a payment. New payees wait in a hold while the checks run in the background."""
        user = self.account_for(user_key)
        payee = payee.strip().lower()
        context = self._context_for_payment(user_key, ctx)
        try:
            refusal = await self._preflight(user, payee, asset_id, amount, context)
            if refusal is not None:
                return refusal
            sent = await self.vault.send(user, payee, asset_id, amount, self._memo_hash(context))
        except VaultError as exc:
            return self._refused(exc.code, payee=payee, detail=exc.message)

        self._contexts.pop(user_key, None)  # a context belongs to one payment
        hold, tx = sent.hold, sent.tx
        self.store.record_tx(hold.id, "send", tx.tx_hash)
        self.store.save_context(hold.id, _context_summary(context))
        self.store.log_event(
            "hold_created",
            {"hold_id": hold.id, "asset": hold.asset, "amount": str(hold.amount), "instant": hold.instant},
        )
        if hold.instant:
            return await self._instant(user_key, hold, tx)

        now = await self.vault.now_ms()
        text = messages.held(
            self.settings, hold, now_ms=now, tx_text=self._tx_text(tx), page=self._page(hold.id)
        )
        await self._notify(user_key, text, messages.sender_buttons(hold.id))
        self._spawn(self._check_hold(user_key, hold, context), f"holdup-check-{hold.id}")
        return PaymentOutcome(hold=hold, tx=tx, instant=False, message=text)

    async def _preflight(
        self, user: UserAccount, payee: str, asset_id: str, amount: int, ctx: PaymentContext
    ) -> PaymentOutcome | None:
        """Refuse obvious failures before spending gas on them; the contract still decides."""
        if not is_valid_account_id(payee):
            return self._refused(E.INVALID_PAYEE, payee=payee)
        if payee == user.account_id:
            return self._refused(E.E_SELF_PAYMENT)
        if amount <= 0:
            return self._refused(E.E_ZERO_AMOUNT)
        if asset_id not in self.settings.assets:
            return self._refused(E.E_UNKNOWN_ASSET)
        view = await self.vault.get_user(user.account_id)
        if view is None:
            return self._refused(E.E_NOT_REGISTERED)
        if not await self.vault.account_exists(payee):
            return self._refused(E.UNKNOWN_PAYEE, payee=payee)
        balances = await self.vault.get_balance(user.account_id)
        # Every hold also pays the vault's storage fee from the sender's NEAR balance.
        fee = (await self.vault.get_config()).hold_storage_fee
        if asset_id != NEAR and balances.get(asset_id, 0) < amount:
            return self._short(asset_id, have=balances.get(asset_id, 0), need=amount)
        near_needed = fee + (amount if asset_id == NEAR else 0)
        if balances.get(NEAR, 0) < near_needed:
            return self._short(NEAR, have=balances.get(NEAR, 0), need=near_needed, fee=fee)
        now = await self.vault.now_ms()
        trusted = [t.payee for t in view.trusted if t.effective_ms <= now]
        ctx.trusted_payees = sorted(set(ctx.trusted_payees) | set(trusted))
        return None

    def _short(self, asset_id: str, *, have: int, need: int, fee: int = 0) -> PaymentOutcome:
        text = messages.not_enough(self.settings, asset_id, have=have, need=need, fee=fee)
        return PaymentOutcome(hold=None, tx=None, refused=E.E_INSUFFICIENT_BALANCE, message=text)

    def _refused(self, code: str, *, payee: str = "", detail: str = "") -> PaymentOutcome:
        return PaymentOutcome(
            hold=None, tx=None, refused=code, message=messages.refusal(code, payee=payee, detail=detail)
        )

    async def _instant(self, user_key: str, hold: Hold, tx: TxResult) -> PaymentOutcome:
        settled = await self.vault.get_hold(hold.id) or hold
        tx_text = self._tx_text(tx)
        if settled.status == HoldStatus.FAILED:
            self.store.log_event("payout_failed", {"hold_id": hold.id})
            text = messages.payout_failed(self.settings, settled, tx_text=tx_text)
        else:
            self.store.log_event("instant_paid", {"hold_id": hold.id})
            text = messages.instant_paid(self.settings, settled, tx_text=tx_text)
            await self._notify_payee(settled)
        await self._notify(user_key, text)
        return PaymentOutcome(hold=settled, tx=tx, instant=True, message=text)

    # ------------------------------------------------------------------
    # background checks
    # ------------------------------------------------------------------

    def _spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_idle(self) -> None:
        """Wait for every background check (and anything they started) to finish."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _check_hold(self, user_key: str, hold: Hold, ctx: PaymentContext) -> None:
        fast: Verdict | None = None
        try:
            if self.full_risk is not None:
                fast = await self._assess_fast(hold, ctx)
                if _flags(fast):
                    provisional = replace(
                        fast, brake=True, provisional=True, hold_for_ms=PROVISIONAL_HOLD_MS
                    )
                    await self._apply_verdict(user_key, hold, provisional, "provisional")
            verdict, failure = await self._final_check(user_key, hold, ctx, fast)
            if failure is not None:
                await self._fail_closed(user_key, hold, ctx, failure, fast=fast, verdict=verdict)
            else:
                self._retries.pop(hold.id, None)
                await self._finish(user_key, hold, _keep_rules_decision(fast, verdict), fast)
        except Exception:
            log.exception("checks for hold %s crashed", hold.id)
            try:
                await self._fail_closed(user_key, hold, ctx, "an internal error", fast=fast, verdict=None)
            except Exception:
                log.exception("fail-closed handling for hold %s crashed", hold.id)

    async def _assess_fast(self, hold: Hold, ctx: PaymentContext) -> Verdict | None:
        try:
            return await asyncio.wait_for(self.fast_risk.assess(hold, ctx), self.fast_timeout_s)
        except Exception:
            log.exception("fast rules failed for hold %s", hold.id)
            return None

    async def _final_check(
        self, user_key: str, hold: Hold, ctx: PaymentContext, fast: Verdict | None
    ) -> tuple[Verdict | None, str | None]:
        """The deciding verdict and, if the check failed, a short description of why."""
        if self.full_risk is None:
            verdict = await self._assess_fast(hold, ctx)
            return (verdict, None) if verdict is not None else (None, "the rules check failed")
        task = asyncio.ensure_future(self.full_risk.assess(hold, ctx))
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            # If the check would outlast the hold, keep the brake on while it finishes.
            guard_after = await self._seconds_before_release(hold)
            if guard_after < self.full_timeout_s:
                done, _ = await asyncio.wait({task}, timeout=max(guard_after, 0.0))
                if not done:
                    await self._fail_closed(
                        user_key, hold, ctx, "it is still running", fast=fast, verdict=None, slow=True
                    )
            remaining = self.full_timeout_s - (loop.time() - started)
            verdict = await asyncio.wait_for(task, max(remaining, 0.0))
        except TimeoutError:
            return None, "it timed out"
        except Exception as exc:
            task.cancel()
            log.warning("full check failed for hold %s: %r", hold.id, exc)
            return _failure_verdict(hold, exc), _describe_failure(exc)
        failures = [f for f in verdict.findings if _is_failure(f)]
        if failures:
            return verdict, failures[0].detail or "the AI service is unavailable"
        return verdict, None

    async def _seconds_before_release(self, hold: Hold) -> float:
        current = await self.vault.get_hold(hold.id) or hold
        now = await self.vault.now_ms()
        return (current.release_at_ms - now - self.deadline_margin_ms) / 1000

    async def _finish(
        self, user_key: str, hold: Hold, verdict: Verdict | None, fast: Verdict | None = None
    ) -> None:
        if verdict is None:
            return
        if _flags(verdict) and not _adds_nothing(fast, verdict):
            await self._apply_verdict(user_key, hold, verdict, "final")
            return
        # Clean, or nothing beyond the quick brake: no second extension and no second "Brakes on".
        self._save_verdict(hold.id, verdict, "final")
        if hold.id in self._pending_brakes:
            return  # the quick brake has not reached the chain yet; its own message follows when it does
        current = await self.vault.get_hold(hold.id)
        if current is None or not current.active:
            return
        now = await self.vault.now_ms()
        text = messages.checks_clear(
            self.settings, current, now_ms=now, after_brake=current.extensions > 0
        )
        await self._notify(user_key, text)

    async def _apply_verdict(
        self, user_key: str, hold: Hold, verdict: Verdict, kind: str
    ) -> OracleResult | None:
        self._save_verdict(hold.id, verdict, kind)
        return await self._brake(_Brake(user_key, hold, verdict, kind))

    async def _brake(self, brake: _Brake) -> OracleResult | None:
        """Put a brake on chain and tell the sender and their guardian.

        If the transaction fails, the brake is kept for ``tick`` to retry instead of letting
        the payment release at its old time.
        """
        hold, verdict = brake.hold, brake.verdict
        try:
            result = await self.oracle.apply(hold, verdict, release_at_ms=brake.release_at_ms)
        except VaultError as exc:
            await self._keep_brake(brake, exc)
            return None
        self._brake_done(brake)
        if result.skipped in ("inactive", "missing"):
            return result
        if not result.extended and not verdict.reason_bits & ~result.reason_bits:
            return result  # already held at least this long, for these reasons: nothing new to say
        now = await self.vault.now_ms()
        render = messages.provisional_brake if brake.kind == "provisional" else messages.brake
        text = render(
            self.settings,
            hold,
            verdict.findings,
            result.release_at_ms,
            now_ms=now,
            tx_text=self._tx_text(result.tx),
            page=self._page(hold.id),
        )
        await self._notify(brake.user_key, text, messages.sender_buttons(hold.id))
        if result.extended:
            event = {"provisional": "brake_provisional", "fail_closed": "fail_closed"}.get(brake.kind, "brake")
            self.store.log_event(
                event,
                {"hold_id": hold.id, "reason_bits": verdict.reason_bits, "release_at_ms": result.release_at_ms},
            )
        await self._alert_guardian(brake.user_key, hold, verdict, result.release_at_ms, now)
        return result

    async def _keep_brake(self, brake: _Brake, exc: VaultError) -> None:
        """Remember a brake whose transaction failed; warn the sender the first time."""
        log.warning("could not extend hold %s: %s", brake.hold.id, exc)
        self.store.log_event("brake_failed", {"hold_id": brake.hold.id, "code": exc.code})
        pending = self._pending_brakes.get(brake.hold.id)
        if pending is None or pending.number <= brake.number:  # never replace a newer verdict
            self._pending_brakes[brake.hold.id] = brake
        if pending is None:
            text = messages.brake_not_recorded(self.settings, brake.hold, brake.verdict.findings)
            await self._notify(brake.user_key, text, messages.sender_buttons(brake.hold.id))

    def _brake_done(self, brake: _Brake) -> None:
        """``brake`` reached the chain (or no longer matters): stop retrying it and anything older."""
        pending = self._pending_brakes.get(brake.hold.id)
        if pending is not None and pending.number <= brake.number:
            del self._pending_brakes[brake.hold.id]

    def brake_pending(self, hold: Hold) -> bool:
        """Whether a brake for ``hold`` failed to reach the chain and is being retried."""
        return hold.id in self._pending_brakes

    async def _fail_closed(
        self,
        user_key: str,
        hold: Hold,
        ctx: PaymentContext,
        detail: str,
        *,
        fast: Verdict | None,
        verdict: Verdict | None,
        slow: bool = False,
    ) -> None:
        """Keep the brake on when a check fails: +1 h per failure, at most 6 h in total."""
        brake: _Brake | None = None
        try:
            current = await self.vault.get_hold(hold.id)
            if current is None or not current.active:
                self._retries.pop(hold.id, None)
                return
            state = self._retries.get(hold.id)
            if state is None:
                state = _Retry(hold.id, user_key, ctx, base_release_ms=current.release_at_ms, fast=fast)
                self._retries[hold.id] = state
            state.failures += 1
            budget = verdict is not None and bool(verdict.reason_bits & Reason.BUDGET_EXHAUSTED)
            reason = Reason.BUDGET_EXHAUSTED if budget else Reason.AI_UNAVAILABLE
            findings = [Finding(reason, f"AI check failed: {detail}", source="fail_closed")]
            if verdict is not None:
                findings += [f for f in verdict.findings if not _is_failure(f)]
            if _flags(fast):
                findings += [f for f in fast.findings if f not in findings]

            now = await self.vault.now_ms()
            cap = state.base_release_ms + FAIL_CLOSED_MAX_MS
            target = min(
                max(state.base_release_ms + state.failures * FAIL_CLOSED_STEP_MS, now + FAIL_CLOSED_STEP_MS),
                cap,
            )
            if _flags(fast):  # with the AI down, the rules verdict stands as final
                target = max(target, now + fast.hold_for_ms)
            closed = Verdict(
                hold_id=hold.id,
                brake=True,
                hold_for_ms=max(target - now, 0),
                findings=findings,
                steps=list(verdict.steps) if verdict is not None else [],
                summary=f"fail-closed: {detail}",
            )
            self._save_verdict(hold.id, closed, "fail_closed")
            brake = _Brake(user_key, current, closed, "fail_closed", release_at_ms=target)
            result = await self.oracle.apply(current, closed, release_at_ms=target)
            self._brake_done(brake)
            if result.skipped in ("inactive", "missing"):
                self._retries.pop(hold.id, None)
                return
            final = not slow and (state.failures >= MAX_FAILURES or result.release_at_ms >= cap)
            self.store.log_event(
                "fail_closed",
                {"hold_id": hold.id, "failures": state.failures, "release_at_ms": result.release_at_ms},
            )
            tx_text = self._tx_text(result.tx)
            if slow:
                text = messages.check_slow(self.settings, current, result.release_at_ms, now_ms=now, tx_text=tx_text)
            else:
                text = messages.fail_closed(
                    self.settings,
                    current,
                    detail,
                    result.release_at_ms,
                    now_ms=now,
                    budget=budget,
                    final=final,
                    tx_text=tx_text,
                )
            if final:
                self._retries.pop(hold.id, None)
            elif slow:  # the check is still running; retry only if it later fails
                state.next_retry_ms = self.clock() + int(self.full_timeout_s * 1000)
            else:
                delay = min(RETRY_BASE_MS * 2 ** (state.failures - 1), RETRY_MAX_MS)
                state.next_retry_ms = self.clock() + delay
            await self._notify(user_key, text, messages.sender_buttons(hold.id))
        except VaultError as exc:
            if brake is None:
                # The hold could not even be read: keep an hour's brake pending and retry the check later.
                finding = Finding(Reason.AI_UNAVAILABLE, f"AI check failed: {detail}", source="fail_closed")
                closed = Verdict(hold.id, True, FAIL_CLOSED_STEP_MS, [finding], summary=f"fail-closed: {detail}")
                brake = _Brake(user_key, hold, closed, "fail_closed")
                if hold.id not in self._retries:
                    self._retries[hold.id] = _Retry(
                        hold.id,
                        user_key,
                        ctx,
                        base_release_ms=hold.release_at_ms,
                        fast=fast,
                        failures=1,
                        next_retry_ms=self.clock() + RETRY_BASE_MS,
                    )
            await self._keep_brake(brake, exc)

    async def tick(self) -> int:
        """Retry failed brakes, and failed checks whose retry time has come. Called by the keeper loop.

        On testnet it also lets the treasury check its balance (at most every ten minutes) and ask
        the faucet for more when it runs low.
        """
        self.treasury.watch()
        now = self.clock()
        brakes = [b for b in self._pending_brakes.values() if not b.running]
        for brake in brakes:
            brake.running = True
            self._spawn(self._retry_brake(brake), f"holdup-brake-{brake.hold.id}")
        due = [s for s in self._retries.values() if not s.running and s.next_retry_ms <= now]
        for state in due:
            state.running = True
            self._spawn(self._retry(state), f"holdup-retry-{state.hold_id}")
        return len(brakes) + len(due)

    async def _retry_brake(self, brake: _Brake) -> None:
        try:
            if self._pending_brakes.get(brake.hold.id) is not brake:
                return  # a newer verdict replaced it
            current = await self.vault.get_hold(brake.hold.id)
            if current is None or not current.active:
                self._brake_done(brake)
                return
            max_hold_ms = (await self.vault.get_config()).max_hold_ms
            if await self.vault.now_ms() >= current.created_ms + max_hold_ms:
                # No extension can hold it any longer; stop keeping it from the keeper.
                self._brake_done(brake)
                return
            await self._brake(brake)
        except Exception:
            log.exception("retrying the brake on hold %s failed", brake.hold.id)
        finally:
            brake.running = False

    async def _retry(self, state: _Retry) -> None:
        try:
            hold = await self.vault.get_hold(state.hold_id)
            if hold is None or not hold.active:
                self._retries.pop(state.hold_id, None)
                return
            verdict, failure = await self._final_check(state.user_key, hold, state.ctx, state.fast)
            if failure is not None:
                await self._fail_closed(
                    state.user_key, hold, state.ctx, failure, fast=state.fast, verdict=verdict
                )
                return
            self._retries.pop(state.hold_id, None)
            await self._finish(state.user_key, hold, _keep_rules_decision(state.fast, verdict), state.fast)
        except Exception:
            log.exception("retry for hold %s crashed", state.hold_id)
        finally:
            state.running = False

    def _save_verdict(self, hold_id: int, verdict: Verdict, kind: str) -> None:
        record = verdict_to_dict(verdict, kind)
        self.store.save_verdict(hold_id, record)
        self.store.log_event(
            "verdict",
            {
                "hold_id": hold_id,
                "kind": kind,
                "brake": verdict.brake,
                "reason_bits": verdict.reason_bits,
                "steps": len(verdict.steps),
            },
        )

    async def _alert_guardian(
        self, user_key: str, hold: Hold, verdict: Verdict, until_ms: int, now: int
    ) -> None:
        """Tell the sender's guardian, once per new set of reasons."""
        already = self._alerted.get(hold.id, 0)
        if verdict.reason_bits & ~already == 0:
            return
        view = await self.vault.get_user(hold.sender)
        if view is None or not view.guardian:
            return
        guardian = self.store.user_by_account(view.guardian)
        if guardian is None:
            return
        self._alerted[hold.id] = already | verdict.reason_bits
        owner = self._name(user_key)
        text = messages.guardian_alert(
            self.settings, hold, verdict.findings, until_ms, owner, now_ms=now, page=self._page(hold.id)
        )
        await self._notify(guardian["user_key"], text, messages.guardian_buttons(hold.id, owner))
        self.store.log_event("guardian_alert", {"hold_id": hold.id})

    # ------------------------------------------------------------------
    # cancel and release
    # ------------------------------------------------------------------

    async def cancel(self, user_key: str, hold_id: int) -> TxResult:
        """Cancel your own held payment; the money returns to your vault balance."""
        tx = await self.vault.cancel(self.account_for(user_key), hold_id)
        hold = await self._settled(hold_id, tx)
        guardian_was_alerted = hold.id in self._alerted
        self._record_cancel(hold, tx, "cancel")
        await self._notify(user_key, messages.cancelled(self.settings, hold, tx_text=self._tx_text(tx)))
        if guardian_was_alerted:
            view = await self.vault.get_user(hold.sender)
            guardian = self.store.user_by_account(view.guardian) if view and view.guardian else None
            if guardian is not None:
                text = messages.owner_cancelled_flagged(self.settings, hold, self._name(user_key))
                await self._notify(guardian["user_key"], text)
        return tx

    async def guardian_cancel(self, guardian_user_key: str, hold_id: int) -> TxResult:
        """Cancel a payment of someone who chose you as their guardian."""
        tx = await self.vault.guardian_cancel(self.account_for(guardian_user_key), hold_id)
        hold = await self._settled(hold_id, tx)
        self._record_cancel(hold, tx, "guardian_cancel")
        owner = self.store.user_by_account(hold.sender)
        owner_name = owner["display_name"] if owner and owner["display_name"] else hold.sender
        tx_text = self._tx_text(tx)
        await self._notify(
            guardian_user_key, messages.guardian_cancelled(self.settings, hold, owner_name, tx_text=tx_text)
        )
        if owner is not None:
            text = messages.cancelled_by_guardian(
                self.settings, hold, self._name(guardian_user_key), tx_text=tx_text
            )
            await self._notify(owner["user_key"], text)
        return tx

    async def _settled(self, hold_id: int, tx: TxResult) -> Hold:
        hold = await self.vault.get_hold(hold_id)
        if hold is None:  # cannot happen after a successful cancel; keep the type honest
            raise VaultError(E.E_HOLD_NOT_FOUND, f"hold {hold_id} vanished", tx_hash=tx.tx_hash)
        return hold

    def _record_cancel(self, hold: Hold, tx: TxResult, kind: str) -> None:
        self._retries.pop(hold.id, None)
        self._pending_brakes.pop(hold.id, None)
        self._alerted.pop(hold.id, None)
        self.store.record_tx(hold.id, kind, tx.tx_hash)
        self.store.log_event(kind, {"hold_id": hold.id, "flagged": hold.flagged})
        if hold.flagged:
            self.store.log_event("cancel_after_flag", {"hold_id": hold.id})

    async def on_settled(self, hold: Hold, tx: TxResult) -> None:
        """Keeper callback after ``release``: tell the sender and, if they use HoldUp, the payee."""
        self._retries.pop(hold.id, None)
        self._pending_brakes.pop(hold.id, None)
        self._alerted.pop(hold.id, None)
        self.store.record_tx(hold.id, "release", tx.tx_hash)
        sender = self.store.user_by_account(hold.sender)
        tx_text = self._tx_text(tx)
        if hold.status == HoldStatus.FAILED:
            self.store.log_event("payout_failed", {"hold_id": hold.id})
            if sender is not None:
                await self._notify(sender["user_key"], messages.payout_failed(self.settings, hold, tx_text=tx_text))
            return
        self.store.log_event("release", {"hold_id": hold.id, "flagged": hold.flagged})
        if sender is not None:
            await self._notify(sender["user_key"], messages.released(self.settings, hold, tx_text=tx_text))
        await self._notify_payee(hold)

    async def _notify_payee(self, hold: Hold) -> None:
        payee = self.store.user_by_account(hold.payee)
        if payee is None:
            return
        sender = self.store.user_by_account(hold.sender)
        name = sender["display_name"] if sender and sender["display_name"] else hold.sender
        await self._notify(payee["user_key"], messages.received(self.settings, hold, name))

    def make_keeper(self, interval_s: float = 5.0) -> Keeper:
        """A keeper wired to this engine's notifications and retries; it waits for pending brakes."""
        return Keeper(
            self.vault,
            on_settled=self.on_settled,
            on_tick=self.tick,
            skip=self.brake_pending,
            interval_s=interval_s,
        )

    # ------------------------------------------------------------------
    # trust and guardians
    # ------------------------------------------------------------------

    async def trust(self, user_key: str, payee: str) -> TxResult:
        payee = payee.strip().lower()
        if not is_valid_account_id(payee):
            raise VaultError(E.INVALID_PAYEE, f"{payee!r} is not a valid account id")
        if not await self.vault.account_exists(payee):
            raise VaultError(E.UNKNOWN_PAYEE, f"{payee} does not exist")
        tx = await self.vault.add_trusted(self.account_for(user_key), payee)
        now = await self.vault.now_ms()
        event = first_item(tx.events, "trust_added") or {}
        effective = int(event.get("effective_ms", now))
        self.store.record_tx(None, "trust", tx.tx_hash)
        self.store.log_event("trust", {"payee": payee, "effective_ms": effective})
        await self._notify(
            user_key, messages.trust_added(payee, effective, now_ms=now, tx_text=self._tx_text(tx))
        )
        if effective > now:  # someone holding the server's keys could do this too: tell the guardian
            text = messages.guardian_trust_notice(self._name(user_key), payee, effective, now_ms=now)
            await self._notify_guardian_of(user_key, text)
        return tx

    async def untrust(self, user_key: str, payee: str) -> TxResult:
        payee = payee.strip().lower()
        tx = await self.vault.remove_trusted(self.account_for(user_key), payee)
        self.store.record_tx(None, "untrust", tx.tx_hash)
        self.store.log_event("untrust", {"payee": payee})
        await self._notify(user_key, messages.trust_removed(payee, tx_text=self._tx_text(tx)))
        return tx

    async def guardian_invite(self, user_key: str) -> str:
        """A one-time code; the channel turns it into a deep link for the guardian."""
        return self.store.create_invite(user_key)

    async def accept_guardian(self, guardian_user_key: str, code: str) -> TxResult:
        """The invited person opened the link: make them the inviter's guardian."""
        owner_key = self.store.consume_invite(code)
        if owner_key is None:
            raise VaultError(E.INVALID_INVITE, "this guardian link is invalid, used or expired")
        if owner_key == guardian_user_key:
            raise VaultError(E.E_BAD_GUARDIAN, "you cannot be your own guardian")
        guardian, _ = await self.start(guardian_user_key)
        owner = self.account_for(owner_key)
        before = await self.vault.get_user(owner.account_id)
        tx = await self.vault.set_guardian(owner, guardian.account_id)
        now = await self.vault.now_ms()
        event = first_item(tx.events, "guardian_changed") or {}
        effective = int(event.get("effective_ms", now))
        self.store.record_tx(None, "set_guardian", tx.tx_hash)
        self.store.log_event("guardian_set", {"effective_ms": effective})
        current = self.store.user_by_account(before.guardian) if before and before.guardian else None
        if current is not None and current["user_key"] != guardian_user_key:
            text = messages.guardian_change_notice(
                self._name(owner_key), self._name(guardian_user_key), effective, now_ms=now
            )
            await self._notify(current["user_key"], text, messages.guardian_change_buttons(owner.account_id))
        await self._notify(
            owner_key,
            messages.guardian_set(
                self._name(guardian_user_key), effective, now_ms=now, tx_text=self._tx_text(tx)
            ),
        )
        await self._notify(
            guardian_user_key, messages.guardian_accepted(self._name(owner_key), effective, now_ms=now)
        )
        return tx

    async def veto_guardian_change(self, guardian_user_key: str, owner_account: str) -> TxResult:
        """Stay someone's guardian: drop the guardian change they, or whoever has their key, asked for."""
        tx = await self.vault.veto_guardian_change(self.account_for(guardian_user_key), owner_account)
        self.store.record_tx(None, "veto_guardian_change", tx.tx_hash)
        self.store.log_event("guardian_veto", {})
        owner = self.store.user_by_account(owner_account)
        owner_name = owner["display_name"] if owner and owner["display_name"] else owner_account
        await self._notify(
            guardian_user_key, messages.guardian_change_stopped(owner_name, tx_text=self._tx_text(tx))
        )
        if owner is not None:
            await self._notify(owner["user_key"], messages.guardian_change_vetoed(self._name(guardian_user_key)))
        return tx

    # ------------------------------------------------------------------
    # history, explanations, export and stats
    # ------------------------------------------------------------------

    async def holds(self, user_key: str, limit: int = 20) -> list[Hold]:
        return await self.vault.get_holds_for(self.account_for(user_key).account_id, limit)

    async def why(self, hold_id: int) -> WhyView | None:
        """Everything behind a hold's brakes: findings, AI steps, evidence and transactions."""
        hold = await self.vault.get_hold(hold_id)
        if hold is None:
            return None
        findings: list[Finding] = []
        steps: list[StepRecord] = []
        for record in self.store.list_verdicts(hold_id):
            for item in record.get("findings", []):
                finding = finding_from_dict(item)
                if finding not in findings:
                    findings.append(finding)
            for item in record.get("steps", []):
                step = step_from_dict(item)
                if step not in steps:
                    steps.append(step)
        evidence = self.store.get_evidence(hold_id)
        return WhyView(
            hold=hold,
            evidence_json=evidence.evidence_json if evidence else None,
            evidence_hash=hold.evidence_hash or (evidence.evidence_hash if evidence else None),
            findings=findings,
            steps=steps,
            tx_links=[self._tx_link(label, tx_hash) for label, tx_hash in self.store.get_txs(hold_id)],
        )

    async def export_csv(self, user_key: str) -> str:
        """The user's payments as CSV, with explorer links for every transaction."""
        account = self.account_for(user_key)
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
        for hold in await self.vault.get_holds_for(account.account_id, 100):
            info = self.settings.assets.get(hold.asset)
            amount = format_amount(hold.amount, info.decimals, info.decimals) if info else str(hold.amount)
            txs = self.store.get_txs(hold.id)
            urls = [url for _, tx_hash in txs for url in links.tx_urls(self.settings, tx_hash)[:1]]
            writer.writerow(
                [
                    hold.id,
                    _iso(hold.created_ms),
                    hold.payee,
                    info.symbol if info else hold.asset,
                    amount,
                    hold.status.value,
                    "yes" if hold.instant else "no",
                    "yes" if hold.flagged else "no",
                    ";".join(r.name or str(int(r)) for r in reasons_from_bits(hold.reason_bits)),
                    _iso(hold.release_at_ms),
                    _iso(hold.settled_ms),
                    hold.cancelled_by or "",
                    hold.evidence_hash or "",
                    self._page(hold.id),
                    ";".join(f"{label}:{tx_hash}" for label, tx_hash in txs),
                    " ".join(urls),
                ]
            )
        return out.getvalue()

    async def stats(self) -> dict[str, Any]:
        verdicts = self.store.list_verdicts()
        steps = [step for record in verdicts for step in record.get("steps", []) if step.get("model")]
        count = self.store.count_events
        return {
            "users": self.store.count_users(),
            "holds": count("hold_created"),
            "instant": count("instant_paid"),
            "provisional_brakes": count("brake_provisional"),
            "brakes": count("brake"),
            # A payment braked quickly and then again by the full check counts once here.
            "braked_holds": self.store.count_event_holds("brake_provisional", "brake", "fail_closed"),
            "fail_closed": count("fail_closed"),
            "cancels": count("cancel"),
            "guardian_cancels": count("guardian_cancel"),
            "cancels_after_flag": count("cancel_after_flag"),
            "releases": count("release"),
            "payout_failures": count("payout_failed"),
            "ai_checks": sum(1 for record in verdicts if any(s.get("model") for s in record.get("steps", []))),
            "ai_steps": len(steps),
            "fallbacks": sum(1 for step in steps if step.get("fallback")),
            "ai_cost_usd": round(sum(step.get("cost_usd") or 0.0 for step in steps), 6),
        }

    # ------------------------------------------------------------------
    # small helpers
    # ------------------------------------------------------------------

    async def _notify(
        self, user_key: str, text: str, buttons: list[list[tuple[str, str]]] | None = None
    ) -> None:
        self.store.log_event("notify", {"user_key": user_key, "text": text})
        try:
            await self.notifier.notify(user_key, text, buttons)
        except Exception:
            log.exception("could not notify %s", user_key)

    async def _notify_guardian_of(self, user_key: str, text: str) -> None:
        view = await self.vault.get_user(self.account_for(user_key).account_id)
        guardian = self.store.user_by_account(view.guardian) if view and view.guardian else None
        if guardian is not None:
            await self._notify(guardian["user_key"], text)

    def _name(self, user_key: str) -> str:
        row = self.store.get_user(user_key)
        return row["display_name"] if row and row["display_name"] else "your contact"

    def _tx_text(self, tx: TxResult | None) -> str:
        if tx is None:
            return ""
        return links.describe_tx(self.settings, tx.tx_hash, simulated=self.simulated)

    def _tx_link(self, label: str, tx_hash: str) -> tuple[str, str]:
        url = links.tx_url(self.settings, tx_hash)
        if url:
            return label, url
        suffix = " (simulated)" if self.simulated else ""
        return f"{label} {links.short_hash(tx_hash)}{suffix}", ""

    def _page(self, hold_id: int) -> str:
        return links.hold_page(self.settings, hold_id) if self.settings.public_base_url else ""


# ----------------------------------------------------------------------
# serialisation of verdicts for the store
# ----------------------------------------------------------------------


def verdict_to_dict(verdict: Verdict, kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "hold_id": verdict.hold_id,
        "brake": verdict.brake,
        "hold_for_ms": verdict.hold_for_ms,
        "provisional": verdict.provisional,
        "summary": verdict.summary,
        "reason_bits": verdict.reason_bits,
        "findings": [f.to_dict() for f in verdict.findings],
        "steps": [s.to_dict() for s in verdict.steps],
    }


def finding_from_dict(data: dict[str, Any]) -> Finding:
    name = data.get("reason")
    reason = Reason[str(name)] if name in Reason.__members__ else Reason(int(data.get("bit", 0)))
    return Finding(
        reason=reason,
        detail=data.get("detail") or "",
        source=data.get("source") or "",
        model=data.get("model"),
        citations=list(data.get("citations") or []),
    )


_STEP_FIELDS = {f.name for f in fields(StepRecord)}


def step_from_dict(data: dict[str, Any]) -> StepRecord:
    values = {k: v for k, v in data.items() if k in _STEP_FIELDS}
    values["tool_calls"] = list(values.get("tool_calls") or [])
    return StepRecord(**values)


def _context_summary(ctx: PaymentContext) -> dict[str, Any]:
    """What the UI shows about a payment's context. Stays local; never on-chain."""
    text = ctx.forwarded_text or ""
    return {
        "has_text": bool(text),
        "text_chars": len(text),
        "preview": text[:PREVIEW_CHARS],
        "has_image": ctx.image_bytes is not None,
        "image_mime": ctx.image_mime,
        "received_ms": ctx.received_ms,
    }


def _merge_contexts(old: PaymentContext, new: PaymentContext) -> PaymentContext:
    texts = [t for t in (old.forwarded_text, new.forwarded_text) if t]
    text = "\n\n".join(texts)[-CONTEXT_TEXT_MAX:] if texts else None
    keep_new_image = new.image_bytes is not None
    return PaymentContext(
        forwarded_text=text,
        image_bytes=new.image_bytes if keep_new_image else old.image_bytes,
        image_mime=new.image_mime if keep_new_image else old.image_mime,
        received_ms=new.received_ms,
        trusted_payees=sorted(set(old.trusted_payees) | set(new.trusted_payees)),
    )
