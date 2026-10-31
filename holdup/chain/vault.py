"""``VaultPort`` on a real NEAR network (sandbox or testnet) through near-kit.

Who signs what:

- user calls: the user's derived key, a function-call key on the vault for
  ``send``, ``cancel``, ``guardian_cancel``, ``add_trusted``,
  ``remove_trusted``, ``set_guardian`` and ``veto_guardian_change`` (zero
  deposit);
- ``extend_hold``: the oracle's only key;
- ``release``: the keeper's only key;
- onboarding: the treasury (the root account), which creates the user's
  sub-account, registers it in the vault and funds it with demo money.

Every account pays its own gas, bought up front at 0.001 NEAR per Tgas (see
``budget``). A user starts with 0.03 NEAR, so user calls attach 15 to 20 Tgas,
and more only for a send that may pay a trusted outside payee at once. When the
node refuses a user's, the oracle's or the keeper's transaction for lack of
balance, the treasury tops that account up and the transaction goes out once
more; when the treasury itself is short, the call fails with ``TREASURY_EMPTY``.

Contract panics come back as :class:`VaultError` with the contract's ``E_``
code; NEP-297 events from every receipt are parsed into ``TxResult.events``.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from typing import Any

from near import AsyncNear, KeyPairSigner, parse_key
from near.actions import add_function_call_key, create_account, function_call, transfer
from near.errors import (
    AccountNotFoundError,
    ContractPanicError,
    InsufficientBalanceError,
    InvalidAccountIdError,
    NearError,
    RpcError,
)
from near.models import TransactionResult
from near.rpc import NETWORK_RPC_URLS
from near.units import ONE_YOCTO, ZERO, Amount, Gas

from ..config import Settings
from ..models import (
    NEAR,
    Hold,
    PayeeStats,
    SendResult,
    TxResult,
    UserAccount,
    UserView,
    VaultConfig,
)
from . import errors as E
from .budget import (
    GAS,
    PAYOUT_SEND_GAS,
    ROLE_TOP_UP,
    ROLE_TOP_UP_LIMIT,
    USER_TOP_UP_LIMIT,
    available_balance,
    near_text,
    near_to_yocto,
)
from .errors import VaultError, panic_in, parse_panic, rejection_name
from .events import events_from_logs, first_item
from .fake import flatten_balance
from .keys import USER_METHODS
from .rpc import make_client
from .treasury import TREASURY_EMPTY

log = logging.getLogger(__name__)

__all__ = ["GAS", "NearVault", "error_from_near"]

# A payee whose trust takes effect within this long may already be trusted when the send runs.
TRUST_CLOCK_SLACK_MS = 120_000

# Who pays when a transaction is refused for lack of balance (see ``NearVault._execute``).
USER, ROLE, TREASURY = "user", "role", "treasury"


def error_from_near(exc: NearError) -> VaultError:
    """Map a near-kit error to a :class:`VaultError` carrying the most useful code."""
    data = getattr(exc, "data", None)
    tx_hash = _tx_hash_of(data)
    if isinstance(exc, ContractPanicError):
        parsed = parse_panic(exc.panic)
        if parsed:
            return VaultError(parsed[0], parsed[1], tx_hash=tx_hash, data=data)
        if "deserialize" in exc.panic.lower():
            return VaultError(E.E_BAD_ARGS, exc.panic, tx_hash=tx_hash, data=data)
        return VaultError(E.E_TX_FAILED, exc.panic, tx_hash=tx_hash, data=data)
    if isinstance(exc, RpcError):
        info = (data.get("cause") or {}).get("info") if isinstance(data, dict) else None
        texts = [str((info or {}).get("vm_error", "")), str(data.get("data", "")) if isinstance(data, dict) else ""]
        for text in texts:
            if parsed := panic_in(text):
                return VaultError(parsed[0], parsed[1], data=data)
        if getattr(exc, "code", "") == "INVALID_TRANSACTION":
            return VaultError(E.E_TX_REJECTED, f"{rejection_name(data)}: {exc}", data=data)
        return VaultError(E.E_RPC, str(exc), data=data)
    if exc.code in ("INVALID_NONCE", "INSUFFICIENT_BALANCE", "TRANSACTION_EXPIRED", "ACCESS_KEY_NOT_FOUND"):
        return VaultError(E.E_TX_REJECTED, str(exc), data=data)
    return VaultError(E.E_RPC, str(exc), data=data)


def _tx_hash_of(data: Any) -> str | None:
    if isinstance(data, dict):
        tx = data.get("transaction")
        if isinstance(tx, dict) and isinstance(tx.get("hash"), str):
            return tx["hash"]
    return None


def _succeeded(result: dict[str, Any]) -> bool:
    status = result.get("status")
    return isinstance(status, dict) and "SuccessValue" in status


def _network_name(settings: Settings) -> str | None:
    return settings.network if settings.network in NETWORK_RPC_URLS else None


class NearVault:
    """The HoldVault contract on NEAR, behind the ``VaultPort`` protocol."""

    simulated = False

    def __init__(self, settings: Settings, *, near: AsyncNear | None = None) -> None:
        if not settings.vault_account:
            raise ValueError("HOLDUP_VAULT_ACCOUNT is not configured")
        self.settings = settings
        self.vault_id = settings.vault_account
        self._near = near or make_client(_network_name(settings), settings.rpc_url or None)
        self._config: VaultConfig | None = None

    async def aclose(self) -> None:
        await self._near.aclose()

    # ------------------------------------------------------------------
    # signers
    # ------------------------------------------------------------------

    @staticmethod
    def _signer(account_id: str, private_key: str, role: str) -> KeyPairSigner:
        if not account_id or not private_key:
            raise VaultError(E.E_NOT_CONFIGURED, f"no {role} key is configured")
        return KeyPairSigner(account_id=account_id, key_pair=parse_key(private_key))

    def _user_signer(self, user: UserAccount) -> KeyPairSigner:
        return self._signer(user.account_id, user.private_key, "user")

    def _oracle_signer(self) -> KeyPairSigner:
        oracle = self.settings.oracle_accounts[0] if self.settings.oracle_accounts else ""
        return self._signer(oracle, self.settings.oracle_private_key, "oracle")

    def _keeper_signer(self) -> KeyPairSigner:
        return self._signer(self.settings.keeper_account, self.settings.keeper_private_key, "keeper")

    def _treasury_signer(self) -> KeyPairSigner:
        return self._signer(self.settings.root_account, self.settings.treasury_private_key, "treasury")

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------

    async def _view(self, method: str, args: dict[str, Any] | None = None) -> Any:
        try:
            return await self._near.view(self.vault_id, method, args)
        except NearError as exc:
            raise error_from_near(exc) from exc

    async def _send(self, receiver: str, actions: Sequence[Any], signer: KeyPairSigner) -> TxResult:
        """Send one transaction; a refusal for lack of balance is left to the caller."""
        try:
            result = await self._near.send_transaction(receiver, list(actions), signer=signer)
        except InsufficientBalanceError:
            raise
        except ContractPanicError as exc:
            # near-kit raises when any receipt failed. A failed payout transfer is
            # handled by the vault's callback, so the transaction itself succeeded.
            if isinstance(exc.data, dict) and _succeeded(exc.data):
                return self._tx_result(TransactionResult.model_validate(exc.data))
            raise error_from_near(exc) from exc
        except NearError as exc:
            raise error_from_near(exc) from exc
        return self._tx_result(result)

    async def _execute(
        self, receiver: str, actions: Sequence[Any], signer: KeyPairSigner, *, payer: str | None = None
    ) -> TxResult:
        """Send a transaction, handling a refusal for lack of balance by who pays for it.

        A user, the oracle or the keeper short of NEAR for gas gets a top-up from the
        treasury and the transaction is sent once more (``payer`` ``USER`` or ``ROLE``);
        the treasury short of NEAR becomes ``TREASURY_EMPTY``.
        """
        try:
            return await self._send(receiver, actions, signer)
        except InsufficientBalanceError as exc:
            if payer == TREASURY:
                raise VaultError(TREASURY_EMPTY, "the treasury cannot pay for this right now") from exc
            if payer not in (USER, ROLE) or not self.settings.treasury_private_key:
                raise error_from_near(exc) from exc
            await self._top_up(signer.account_id, exc, payer)
        try:
            return await self._send(receiver, actions, signer)
        except InsufficientBalanceError as exc:
            raise error_from_near(exc) from exc

    async def _top_up(self, account_id: str, refused: InsufficientBalanceError, payer: str) -> None:
        """Send ``account_id`` what its refused transaction lacked, plus a refill, from the treasury."""
        if refused.required is not None and refused.available is not None:
            shortfall = max(0, refused.required - refused.available)
        else:
            shortfall = 0
        if payer == USER:
            refill, limit = near_to_yocto(self.settings.user_gas_near), USER_TOP_UP_LIMIT
        else:
            refill, limit = ROLE_TOP_UP, ROLE_TOP_UP_LIMIT
        if shortfall > limit:
            raise VaultError(
                E.E_TX_REJECTED,
                f"{account_id} lacks {near_text(shortfall)} NEAR for this transaction, more than a gas top-up covers",
            ) from refused
        amount = shortfall + refill
        try:
            await self._send(account_id, [transfer(Amount.yocto(amount))], self._treasury_signer())
        except InsufficientBalanceError as exc:
            raise VaultError(TREASURY_EMPTY, "the treasury cannot pay for network fees right now") from exc
        log.info("treasury topped up %s with %s NEAR for gas", account_id, near_text(amount))

    async def _call(
        self,
        signer: KeyPairSigner,
        method: str,
        args: dict[str, Any],
        *,
        deposit: Amount | None = None,
        receiver: str | None = None,
        gas: Gas | None = None,
        payer: str | None = None,
    ) -> TxResult:
        action = function_call(method, args, gas=gas or GAS[method], deposit=deposit or ZERO)
        return await self._execute(receiver or self.vault_id, [action], signer, payer=payer)

    async def _user_call(
        self, user: UserAccount, method: str, args: dict[str, Any], *, gas: Gas | None = None
    ) -> TxResult:
        return await self._call(self._user_signer(user), method, args, gas=gas, payer=USER)

    def _tx_result(self, result: TransactionResult) -> TxResult:
        tx_hash = result.transaction_hash or (
            result.transaction_outcome.id if result.transaction_outcome else ""
        ) or ""
        try:
            value = result.json_value()
        except ValueError:
            value = result.success_value
        logs = result.logs
        return TxResult(
            tx_hash=tx_hash,
            events=events_from_logs(logs),
            logs=logs,
            return_value=value,
            link=self.settings.tx_link(tx_hash),
        )

    # ------------------------------------------------------------------
    # views
    # ------------------------------------------------------------------

    async def get_config(self) -> VaultConfig:
        if self._config is None:  # fixed forever at deployment
            self._config = VaultConfig.from_view(await self._view("get_config"))
        return self._config

    async def get_hold(self, hold_id: int) -> Hold | None:
        view = await self._view("get_hold", {"hold_id": str(hold_id)})
        return Hold.from_view(view) if view else None

    async def get_holds_for(self, account_id: str, limit: int = 50) -> list[Hold]:
        views = await self._view("get_holds_for", {"account_id": account_id, "limit": limit})
        return [Hold.from_view(v) for v in views or []]

    async def get_active_holds(self, account_id: str) -> list[Hold]:
        views = await self._view("get_active_holds", {"account_id": account_id})
        return [Hold.from_view(v) for v in views or []]

    async def get_due_holds(self, limit: int = 50) -> list[Hold]:
        views = await self._view("get_due_holds", {"limit": limit})
        return [Hold.from_view(v) for v in views or []]

    async def get_balance(self, account_id: str) -> dict[str, int]:
        return flatten_balance(await self._view("get_balance", {"account_id": account_id}) or {})

    async def get_user(self, account_id: str) -> UserView | None:
        view = await self._view("get_user", {"account_id": account_id})
        return UserView.from_view(view) if view else None

    async def get_payee_stats(self, account_id: str) -> PayeeStats:
        return PayeeStats.from_view(await self._view("get_payee_stats", {"account_id": account_id}))

    async def is_registered(self, account_id: str) -> bool:
        return bool(await self._view("is_registered", {"account_id": account_id}))

    async def get_solvency(self) -> dict[str, int]:
        view = await self._view("get_solvency")
        return {key: int(value) for key, value in (view or {}).items()}

    async def account_exists(self, account_id: str) -> bool:
        try:
            await self._near.account(account_id)
        except (AccountNotFoundError, InvalidAccountIdError):
            return False
        except NearError as exc:
            raise error_from_near(exc) from exc
        return True

    async def treasury_balance(self) -> int:
        """What the treasury (the root account) can spend, in yoctoNEAR."""
        try:
            view = await self._near.account(self.settings.root_account)
        except NearError as exc:
            raise error_from_near(exc) from exc
        return available_balance(int(view.amount), int(view.locked), view.storage_usage)

    async def now_ms(self) -> int:
        """Timestamp of the latest block, the clock the contract uses."""
        try:
            block = await self._near.rpc("block", {"finality": "optimistic"})
        except NearError as exc:
            raise error_from_near(exc) from exc
        header = block["header"]
        nanos = header.get("timestamp_nanosec") or header["timestamp"]
        return int(nanos) // 1_000_000

    # ------------------------------------------------------------------
    # user calls
    # ------------------------------------------------------------------

    async def send(
        self, user: UserAccount, payee: str, asset: str, amount: int, memo_hash: str | None = None
    ) -> SendResult:
        args = {"payee": payee, "asset": asset, "amount": str(amount), "memo_hash": memo_hash}
        tx = await self._user_call(user, "send", args, gas=await self._send_gas(user, payee, asset))
        value = tx.return_value
        if isinstance(value, dict) and isinstance(value.get("hold"), dict):
            hold = Hold.from_view(value["hold"])
        else:
            created = first_item(tx.events, "hold_created")
            found = await self.get_hold(int(created["hold_id"])) if created else None
            if found is None:
                raise VaultError(E.E_TX_FAILED, "send returned no hold", tx_hash=tx.tx_hash)
            hold = found
        return SendResult(hold=hold, tx=tx)

    async def _send_gas(self, user: UserAccount, payee: str, asset: str) -> Gas:
        """Gas for a send: more when it may pay a trusted payee outside the vault in the same call.

        Whether today's instant cap still allows it is not visible from outside, so a trusted
        payee always gets the larger amount; what the send does not use comes back.
        """
        try:
            view = await self.get_user(user.account_id)
            soon = int(time.time() * 1000) + TRUST_CLOCK_SLACK_MS
            trusted = view is not None and any(t.payee == payee and t.effective_ms <= soon for t in view.trusted)
            if not trusted or await self.is_registered(payee):
                return GAS["send"]
        except VaultError as exc:
            log.info("could not tell whether %s is trusted (%s); attaching gas for a payout", payee, exc)
        return PAYOUT_SEND_GAS[NEAR if asset == NEAR else "token"]

    async def cancel(self, user: UserAccount, hold_id: int) -> TxResult:
        return await self._user_call(user, "cancel", {"hold_id": str(hold_id)})

    async def guardian_cancel(self, guardian: UserAccount, hold_id: int) -> TxResult:
        return await self._user_call(guardian, "guardian_cancel", {"hold_id": str(hold_id)})

    async def add_trusted(self, user: UserAccount, payee: str) -> TxResult:
        return await self._user_call(user, "add_trusted", {"payee": payee})

    async def remove_trusted(self, user: UserAccount, payee: str) -> TxResult:
        return await self._user_call(user, "remove_trusted", {"payee": payee})

    async def set_guardian(self, user: UserAccount, guardian: str | None) -> TxResult:
        return await self._user_call(user, "set_guardian", {"guardian": guardian})

    async def veto_guardian_change(self, guardian: UserAccount, owner: str) -> TxResult:
        return await self._user_call(guardian, "veto_guardian_change", {"owner": owner})

    # ------------------------------------------------------------------
    # oracle, keeper and treasury calls
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
        return await self._call(self._oracle_signer(), "extend_hold", args, payer=ROLE)

    async def release(self, hold_id: int) -> TxResult:
        return await self._call(self._keeper_signer(), "release", {"hold_id": str(hold_id)}, payer=ROLE)

    async def onboard_user(self, user: UserAccount, demo_funds: dict[str, int]) -> list[TxResult]:
        """Create the sub-account with one restricted key, register it and fund it.

        Each step is skipped when already done, so a half-finished onboarding
        can simply be run again. The user account holds one function-call key
        (about 320 bytes), so it needs no balance for storage: its NEAR pays gas.
        A treasury too poor for a step raises ``TREASURY_EMPTY``.
        """
        treasury = self._treasury_signer()
        results: list[TxResult] = []
        if not await self.account_exists(user.account_id):
            actions = [
                create_account(),
                transfer(Amount.yocto(near_to_yocto(self.settings.user_gas_near))),
                add_function_call_key(user.public_key, self.vault_id, USER_METHODS, allowance=None),
            ]
            results.append(await self._execute(user.account_id, actions, treasury, payer=TREASURY))
        if not await self.is_registered(user.account_id):
            config = await self.get_config()
            deposit = Amount.yocto(config.registration_fee + demo_funds.get(NEAR, 0))
            results.append(
                await self._call(
                    treasury, "deposit_near_for", {"account_id": user.account_id}, deposit=deposit, payer=TREASURY
                )
            )
        for token, amount in demo_funds.items():
            if token == NEAR or amount <= 0:
                continue
            msg = json.dumps({"credit_to": user.account_id}, separators=(",", ":"))
            args = {"receiver_id": self.vault_id, "amount": str(amount), "msg": msg}
            results.append(
                await self._call(
                    treasury, "ft_transfer_call", args, deposit=ONE_YOCTO, receiver=token, payer=TREASURY
                )
            )
        return results
