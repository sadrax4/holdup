"""The conversation behind both chat channels: commands, payments, context and buttons.

The Telegram bot and the web console turn their input into calls on a
``Dialog`` and render the replies it returns, so the two channels behave the
same and drive the same engine. A reply answers the person's own action;
anything that happens later (held, brakes on, released) reaches them through
the engine's notifier.

Money only moves after an explicit confirmation: typed payments are parsed by
deterministic rules, shown back, and sent when the person taps Confirm.
Forwarded or pasted messages never become payments; they are remembered for
15 minutes as context for the next one.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from near import is_valid_account_id

from ..chain.errors import VaultError
from ..config import Settings
from ..messages import error_text, fmt_utc
from ..models import AssetInfo, PaymentContext, UserAccount, parse_amount
from . import texts
from .parser import ParseError, PayCommand, parse_pay, payee_problem
from .ratelimit import RateLimiter
from .samples import (
    STABLE_SYMBOLS,
    Sample,
    find_sample,
    for_person,
    friend_account,
    load_samples,
    stable_symbol,
)

log = logging.getLogger(__name__)

Buttons = list[list[tuple[str, str]]]
InviteLinks = Callable[[str], list[tuple[str, str]]]
ExportLink = Callable[[UserAccount], tuple[str, int]]  # (url, expires_ms)

ACTIONS_PER_MINUTE = 20
CONFIRM_TTL_MS = 10 * 60_000
CONTEXT_TTL_MS = 15 * 60_000
CONTEXT_MAX_CHARS = 8_000
MIN_CONTEXT_CHARS = 12
TYPED_COMMAND_MAX_CHARS = 120
MAX_PENDING_PER_USER = 5
DEMO_SAMPLES = ("hero", "invoice")


@dataclass(frozen=True)
class Document:
    filename: str
    content: bytes
    mime_type: str = "text/csv"


@dataclass(frozen=True)
class Reply:
    text: str
    buttons: Buttons | None = None
    document: Document | None = None
    kind: str = ""  # "sample": a training sample, shown like a message from someone else


@dataclass
class _Pending:
    user_key: str
    payee: str
    asset_id: str
    amount: int
    expires_ms: int


@dataclass
class _Context:
    chars: int | None
    has_image: bool
    expires_ms: int


def _wall_ms() -> int:
    return int(time.time() * 1000)


class Dialog:
    """Channel-neutral handling of one chat channel's people."""

    def __init__(
        self,
        settings: Settings,
        engine: Any,
        *,
        invite_links: InviteLinks | None = None,
        export_link: ExportLink | None = None,
        limiter: RateLimiter | None = None,
        clock: Callable[[], int] | None = None,
        samples: Sequence[Sample] | None = None,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.invite_links = invite_links or (lambda code: [])
        self.export_link = export_link
        self.limiter = limiter or RateLimiter(ACTIONS_PER_MINUTE, 60.0)
        self.clock = clock or _wall_ms
        self.samples = list(samples) if samples is not None else load_samples(settings)
        self.symbol = stable_symbol(settings)
        self._pending: dict[str, _Pending] = {}
        self._contexts: dict[str, _Context] = {}
        self._started: set[str] = set()
        self._commands: dict[str, Callable[[str, list[str]], Awaitable[list[Reply]]]] = {
            "help": self._help,
            "balance": self._balance,
            "holds": self._holds,
            "cancel": self._cancel_command,
            "trust": self._trust,
            "untrust": self._untrust,
            "guardian": self._guardian,
            "why": self._why_command,
            "export": self._export,
            "prove": self._prove,
            "demo": self._demo,
            "stats": self._stats,
            "budget": self._budget,
            "pay": self._pay_command,
            "send": self._pay_command,
        }

    @property
    def command_names(self) -> list[str]:
        return ["start", *self._commands]

    def busy_for(self, user_key: str) -> float:
        """Seconds until this person's next action is accepted (0: now), without counting one."""
        return self.limiter.wait(user_key)

    # ------------------------------------------------------------------
    # entry points
    # ------------------------------------------------------------------

    async def start(
        self, user_key: str, *, display_name: str = "", chat_ref: str | None = None, payload: str = ""
    ) -> list[Reply]:
        """/start: create the account if needed, say hello, and accept a guardian invite (``g_<code>``)."""
        return await self._run(user_key, self._start(user_key, display_name, chat_ref, payload), onboard=False)

    async def command(
        self,
        user_key: str,
        name: str,
        args: Sequence[str] = (),
        *,
        display_name: str = "",
        chat_ref: str | None = None,
    ) -> list[Reply]:
        name = name.lower().lstrip("/")
        handler = self._commands.get(name)
        work = handler(user_key, list(args)) if handler is not None else self._unknown(name)
        return await self._run(user_key, work, display_name, chat_ref)

    async def text(
        self, user_key: str, text: str, *, display_name: str = "", chat_ref: str | None = None
    ) -> list[Reply]:
        """Something the person typed or pasted: a payment to confirm, or context for the next one."""
        return await self._run(user_key, self._text(user_key, text), display_name, chat_ref)

    async def context(
        self,
        user_key: str,
        *,
        text: str | None = None,
        image: bytes | None = None,
        image_mime: str | None = None,
        sender: str = "",
        forwarded: bool = True,
        display_name: str = "",
        chat_ref: str | None = None,
    ) -> list[Reply]:
        """A forwarded message or an image: remembered for the next payment, never paid from."""
        body = (text or "").strip()
        if body and sender:
            body = f"Forwarded from {sender}:\n{body}"
        ctx = PaymentContext(
            forwarded_text=body[:CONTEXT_MAX_CHARS] or None,
            image_bytes=image,
            image_mime=image_mime if image is not None else None,
        )
        return await self._run(user_key, self._remember(user_key, ctx, forwarded=forwarded), display_name, chat_ref)

    async def button(
        self, user_key: str, data: str, *, display_name: str = "", chat_ref: str | None = None
    ) -> list[Reply]:
        """A tapped button: ``confirm:``/``drop:<nonce>``, ``cancel:``/``gcancel:``/``why:<id>``, ``sample:<id>``."""
        return await self._run(user_key, self._button(user_key, data), display_name, chat_ref)

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------

    async def _run(
        self,
        user_key: str,
        work: Awaitable[list[Reply]],
        display_name: str = "",
        chat_ref: str | None = None,
        *,
        onboard: bool = True,
    ) -> list[Reply]:
        """Rate-limit, make sure the person has an account, run ``work`` and turn errors into replies."""
        decision = self.limiter.hit(user_key)
        if not decision.allowed:
            _close(work)
            if decision.warn:
                return [Reply(f"Slow down a little: at most {self.limiter.limit} actions a minute.")]
            return []
        try:
            if onboard and user_key not in self._started:
                await self.engine.start(user_key, display_name, chat_ref)
                self._started.add(user_key)
            return await work
        except VaultError as exc:
            return [Reply(error_text(exc.code, exc.message))]
        except Exception:
            log.exception("chat action for %s failed", user_key)
            return [Reply("Something went wrong on our side. Please check /holds before you try again.")]
        finally:
            _close(work)

    def _now(self) -> int:
        return self.clock()

    def _friend(self, user_key: str) -> str:
        return friend_account(self.settings, user_key)

    def _page(self, hold_id: int) -> str:
        base = self.settings.public_base_url
        return f"{base}/h/{hold_id}" if base else ""

    # ------------------------------------------------------------------
    # start, text and context
    # ------------------------------------------------------------------

    async def _start(self, user_key: str, display_name: str, chat_ref: str | None, payload: str) -> list[Reply]:
        account, created = await self.engine.start(user_key, display_name, chat_ref)
        self._started.add(user_key)
        balances = {} if created else await self.engine.balance(user_key)
        replies = [
            Reply(
                texts.welcome(
                    self.settings,
                    account.account_id,
                    name=display_name or account.display_name,
                    created=created,
                    balances=balances,
                    symbol=self.symbol,
                    payee=self._friend(user_key),
                )
            )
        ]
        code = payload[2:] if payload.startswith("g_") else ""
        if code:
            try:
                await self.engine.accept_guardian(user_key, code)  # the engine tells both people
            except VaultError as exc:
                replies.append(Reply(error_text(exc.code, exc.message)))
        return replies

    async def _text(self, user_key: str, text: str) -> list[Reply]:
        typed = "\n" not in text.strip() and len(text) <= TYPED_COMMAND_MAX_CHARS
        parsed = parse_pay(text)
        if isinstance(parsed, PayCommand):
            return await self._offer(user_key, parsed)
        if isinstance(parsed, ParseError) and typed:
            return [Reply(parsed.message)]
        if len(text.strip()) < MIN_CONTEXT_CHARS:
            return [Reply(texts.chatter_text(self.symbol, self._friend(user_key)))]
        ctx = PaymentContext(forwarded_text=text.strip()[:CONTEXT_MAX_CHARS])
        return await self._remember(user_key, ctx, forwarded=False)

    async def _remember(
        self, user_key: str, ctx: PaymentContext, *, forwarded: bool, next_command: str = ""
    ) -> list[Reply]:
        if not ctx.forwarded_text and ctx.image_bytes is None:
            return [Reply(texts.chatter_text(self.symbol, self._friend(user_key)))]
        now = self._now()
        self.engine.remember_context(user_key, replace(ctx, received_ms=ctx.received_ms or now))
        for key, remembered in list(self._contexts.items()):
            if remembered.expires_ms <= now and key != user_key:
                del self._contexts[key]
        chars = len(ctx.forwarded_text) if ctx.forwarded_text else None
        has_image = ctx.image_bytes is not None
        previous = self._contexts.get(user_key)
        if previous is not None and previous.expires_ms > now:  # the engine merges them too
            if previous.chars is not None:
                chars = previous.chars + (chars or 0)
            has_image = has_image or previous.has_image
        self._contexts[user_key] = _Context(chars, has_image, now + CONTEXT_TTL_MS)
        text = texts.context_saved_text(
            chars=len(ctx.forwarded_text or ""),
            has_image=ctx.image_bytes is not None,
            forwarded=forwarded,
            symbol=self.symbol,
            payee=self._friend(user_key),
            next_command=next_command,
        )
        return [Reply(text)]

    def _live_context(self, user_key: str) -> _Context | None:
        context = self._contexts.get(user_key)
        if context is None or context.expires_ms <= self._now():
            self._contexts.pop(user_key, None)
            return None
        return context

    # ------------------------------------------------------------------
    # payments
    # ------------------------------------------------------------------

    async def _pay_command(self, user_key: str, args: list[str]) -> list[Reply]:
        parsed = parse_pay(" ".join(["pay", *args]))
        if isinstance(parsed, PayCommand):
            return await self._offer(user_key, parsed)
        if parsed is not None:
            return [Reply(parsed.message)]
        return [Reply(texts.chatter_text(self.symbol, self._friend(user_key)))]

    def _asset_for(self, symbol: str) -> tuple[AssetInfo | None, str]:
        """The asset to pay in, and a note when it is not the one the person named."""
        info = self.settings.asset_by_symbol(symbol)
        if info is not None:
            return info, ""
        alternative = self.settings.asset_by_symbol(self.symbol)
        if symbol in STABLE_SYMBOLS and alternative is not None and alternative.symbol in STABLE_SYMBOLS:
            note = (
                f"{symbol} is not available on this server, "
                f"so this pays in {alternative.symbol} instead: {alternative.label}."
            )
            if alternative.symbol == "HUSD":
                note += " It is not Circle USDC."
            return alternative, note
        available = ", ".join(asset.symbol for asset in self.settings.assets.values())
        return None, f"I can't send {symbol} here. Available: {available}."

    async def _offer(self, user_key: str, command: PayCommand) -> list[Reply]:
        asset, note = self._asset_for(command.asset_symbol)
        if asset is None:
            return [Reply(note)]
        try:
            amount = parse_amount(command.amount_text, asset.decimals)
        except ValueError as exc:
            return [Reply(f"{asset.symbol} amounts can have at most {asset.decimals} decimals ({exc}).")]
        nonce = self._park(_Pending(user_key, command.payee, asset.asset_id, amount, self._now() + CONFIRM_TTL_MS))
        context = self._live_context(user_key)
        text = texts.confirm_text(
            self.settings,
            asset.asset_id,
            amount,
            command.payee,
            context_chars=context.chars if context else None,
            has_image=bool(context and context.has_image),
            substituted=note,
        )
        return [Reply(text, buttons=[[("Confirm", f"confirm:{nonce}"), ("Don't send", f"drop:{nonce}")]])]

    def _park(self, pending: _Pending) -> str:
        now = self._now()
        for nonce, item in list(self._pending.items()):
            if item.expires_ms <= now:
                del self._pending[nonce]
        mine = [nonce for nonce, item in self._pending.items() if item.user_key == pending.user_key]
        for nonce in mine[: max(0, len(mine) - MAX_PENDING_PER_USER + 1)]:
            del self._pending[nonce]
        nonce = secrets.token_urlsafe(9)
        self._pending[nonce] = pending
        return nonce

    def _take(self, user_key: str, nonce: str) -> _Pending | None:
        pending = self._pending.get(nonce)
        if pending is None or pending.user_key != user_key:
            return None
        del self._pending[nonce]
        return pending if pending.expires_ms > self._now() else None

    async def _confirm(self, user_key: str, nonce: str) -> list[Reply]:
        pending = self._take(user_key, nonce)
        if pending is None:
            return [Reply("This confirmation has expired or was already used. Type the payment again.")]
        outcome = await self.engine.pay(user_key, pending.payee, pending.asset_id, pending.amount)
        if outcome.refused:
            return [Reply(outcome.message or error_text(outcome.refused))]
        self._contexts.pop(user_key, None)  # the engine attached it to this payment
        return []  # the engine has told the person what happened (held, or paid instantly)

    # ------------------------------------------------------------------
    # buttons
    # ------------------------------------------------------------------

    async def _button(self, user_key: str, data: str) -> list[Reply]:
        action, _, arg = data.partition(":")
        if action == "confirm":
            return await self._confirm(user_key, arg)
        if action == "drop":
            self._take(user_key, arg)
            return [Reply("OK, nothing was sent.")]
        if action == "sample":
            return await self._use_sample(user_key, arg)
        if action == "pay":  # the payment a training sample asks for, with this person's own payee
            return await self._pay_sample(user_key, arg)
        if action == "gveto":  # the guardian in charge keeps an owner's guardian change from happening
            if not is_valid_account_id(arg):
                return [Reply("That button is no longer valid.")]
            await self.engine.veto_guardian_change(user_key, arg)
            return []
        hold_id = _hold_id(arg)
        if hold_id is None or action not in ("cancel", "gcancel", "why"):
            return [Reply("That button is no longer valid.")]
        if action == "cancel":
            await self.engine.cancel(user_key, hold_id)
            return []
        if action == "gcancel":
            await self.engine.guardian_cancel(user_key, hold_id)
            return []
        return await self._why(user_key, hold_id)

    async def _use_sample(self, user_key: str, sample_id: str) -> list[Reply]:
        sample = find_sample(self.samples, sample_id)
        if sample is None:
            return [Reply("That practice message is not available.")]
        sample = for_person(sample, self.settings, user_key)
        text = f"Forwarded from {sample.sender}:\n{sample.text}" if sample.sender else sample.text
        return await self._remember(
            user_key, PaymentContext(forwarded_text=text), forwarded=True, next_command=sample.command
        )

    async def _pay_sample(self, user_key: str, sample_id: str) -> list[Reply]:
        sample = find_sample(self.samples, sample_id)
        parsed = parse_pay(for_person(sample, self.settings, user_key).command) if sample is not None else None
        if not isinstance(parsed, PayCommand):
            return [Reply("That practice payment is not available.")]
        return await self._offer(user_key, parsed)

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    async def _unknown(self, name: str) -> list[Reply]:
        return [Reply(f"I don't know /{name}. /help lists what I can do.")]

    async def _help(self, user_key: str, args: list[str]) -> list[Reply]:
        return [Reply(texts.help_text(self.symbol, self._friend(user_key)))]

    async def _balance(self, user_key: str, args: list[str]) -> list[Reply]:
        balances = await self.engine.balance(user_key)
        account = self.engine.account_for(user_key)
        return [Reply(texts.balance_text(self.settings, account.account_id, balances))]

    async def _holds(self, user_key: str, args: list[str]) -> list[Reply]:
        holds = await self.engine.holds(user_key, 10)
        buttons = [
            [(f"Cancel #{hold.id}", f"cancel:{hold.id}"), (f"Why? #{hold.id}", f"why:{hold.id}")]
            for hold in holds
            if hold.active
        ][:5]
        text = texts.holds_text(self.settings, holds, self._now(), self.symbol, self._friend(user_key))
        return [Reply(text, buttons or None)]

    async def _cancel_command(self, user_key: str, args: list[str]) -> list[Reply]:
        hold_id = _hold_id(args[0]) if args else None
        if hold_id is None:
            return [Reply("Which payment? Send /cancel <id>, for example /cancel 12. /holds lists them.")]
        await self.engine.cancel(user_key, hold_id)
        return []

    async def _why_command(self, user_key: str, args: list[str]) -> list[Reply]:
        hold_id = _hold_id(args[0]) if args else None
        if hold_id is None:
            return [Reply("Which payment? Send /why <id>, for example /why 12. /holds lists them.")]
        return await self._why(user_key, hold_id)

    async def _why(self, user_key: str, hold_id: int) -> list[Reply]:
        view = await self.engine.why(hold_id)
        if view is None:
            return [Reply(f"There is no payment #{hold_id}.")]
        base = self.settings.public_base_url
        text = texts.why_text(
            self.settings,
            view,
            self._now(),
            page=self._page(hold_id),
            steps_page=f"{base}/why/{hold_id}" if base else "",
        )
        mine = view.hold.sender == self.engine.account_for(user_key).account_id
        buttons = [[("Cancel & refund", f"cancel:{hold_id}")]] if view.hold.active and mine else None
        return [Reply(text, buttons)]

    async def _trust(self, user_key: str, args: list[str]) -> list[Reply]:
        payee, problem = _payee_arg(args, "trust")
        if problem:
            return [Reply(problem)]
        await self.engine.trust(user_key, payee)
        return []

    async def _untrust(self, user_key: str, args: list[str]) -> list[Reply]:
        payee, problem = _payee_arg(args, "untrust")
        if problem:
            return [Reply(problem)]
        await self.engine.untrust(user_key, payee)
        return []

    async def _guardian(self, user_key: str, args: list[str]) -> list[Reply]:
        code = await self.engine.guardian_invite(user_key)
        return [Reply(texts.invite_text(code, self.invite_links(code)))]

    async def _export(self, user_key: str, args: list[str]) -> list[Reply]:
        account = self.engine.account_for(user_key)
        if self.export_link is not None:
            url, expires_ms = self.export_link(account)
            until = fmt_utc(expires_ms, self._now())
            return [Reply(f"Your payments as CSV, with explorer links (the link works until {until}):\n{url}")]
        content = (await self.engine.export_csv(user_key)).encode()
        document = Document(f"holdup-{account.account_id}.csv", content)
        return [Reply("Your payments as CSV, with an explorer link for every transaction.", document=document)]

    async def _prove(self, user_key: str, args: list[str]) -> list[Reply]:
        return [Reply(texts.prove_text(self.settings))]

    async def _stats(self, user_key: str, args: list[str]) -> list[Reply]:
        return [Reply(texts.stats_text(self.settings, await self.engine.stats()))]

    async def _budget(self, user_key: str, args: list[str]) -> list[Reply]:
        return [Reply(texts.budget_text(self.settings, await self.engine.stats()))]

    async def _demo(self, user_key: str, args: list[str]) -> list[Reply]:
        chosen = [s for s in (find_sample(self.samples, i) for i in DEMO_SAMPLES) if s is not None]
        chosen = [for_person(s, self.settings, user_key) for s in chosen or [s for s in self.samples if s.scam]]
        if not chosen:
            return [Reply("Practice messages are not installed on this server.")]
        replies = [Reply(texts.demo_intro(chosen))]
        for number, sample in enumerate(chosen, 1):
            replies.append(Reply(texts.sample_intro(sample, number)))
            replies.append(
                Reply(sample.text, buttons=[[("Check it with my next payment", f"sample:{sample.id}")]], kind="sample")
            )
        benign = find_sample(self.samples, "benign")
        if benign is not None:
            command = for_person(benign, self.settings, user_key).command
            replies.append(Reply(f"For comparison, an ordinary payment to a friend: {command}"))
        return replies


def _hold_id(text: str) -> int | None:
    text = text.strip().lstrip("#")
    return int(text) if text.isdigit() else None


def _payee_arg(args: list[str], command: str) -> tuple[str, str]:
    if not args:
        return "", f"Which account? Send /{command} <account>, for example /{command} friend.testnet."
    payee = args[0].strip().lower()
    return payee, payee_problem(payee) or ""


def _close(work: Awaitable[Any]) -> None:
    """Close a coroutine that will not be awaited, so Python does not warn about it."""
    close = getattr(work, "close", None)
    if close is not None:
        close()
