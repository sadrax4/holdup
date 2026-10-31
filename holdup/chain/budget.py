"""What a deployment and each new user cost in NEAR, computed from measured protocol values.

Measured on NEAR testnet on 2026-10-31 (protocol 88, nearcore 2.14.0-rc.5) with the read-only
RPC methods ``EXPERIMENTAL_protocol_config`` and ``gas_price``:

- Storage costs 1e19 yoctoNEAR per byte (1 NEAR per 100 kB). An account record counts 100 bytes
  and every other record (an access key, a contract state entry) 40 bytes on top of its own
  size. An account that uses at most 770 bytes needs no balance for its storage (NEP-448), so
  the demo payees (100 bytes), user accounts (one function-call key, about 320 bytes) and the
  oracle and keeper (about 230 bytes) hold NEAR only to pay for gas.
- Gas burns at the gas price, 1e8 yoctoNEAR per gas (0.0001 NEAR per Tgas), but a transaction
  buys the gas it attaches up front at the minimum purchase price, 1e9 per gas (0.001 NEAR per
  Tgas), and gets back what it does not use. A call with 100 Tgas attached therefore needs
  0.1 NEAR at hand although it burns a small part of that.
- Creating an account costs at least 0.007 NEAR (``account_creation_charge``).

The vault's own costs come from contracts/README.md: every hold pays the vault a 0.015 NEAR
storage fee and every trusted payee or guardian change 0.001 NEAR, from the sender's NEAR balance
in the vault, and registration keeps ``registration_fee``. Each fee covers the storage and the
settlement reserve its record needs, so the vault only needs its code, its initial state and a
margin for ``assert_solvent`` to pass from the first call.

Everything here is an upper bound: a transaction is assumed to burn all the gas it attaches.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from near import Ed25519KeyPair
from near.actions import add_function_call_key, create_account, function_call, transfer
from near.units import ONE_YOCTO, Amount, Gas
from near.wire import Action

from ..models import NEAR
from .keys import USER_METHODS

YOCTO_PER_NEAR = 10**24
MILLINEAR = 10**21
TGAS = 10**12

# --- measured protocol values ---------------------------------------------------------------
STORAGE_BYTE_COST = 10**19  # storage_amount_per_byte
ACCOUNT_RECORD_BYTES = 100  # num_bytes_account
RECORD_OVERHEAD_BYTES = 40  # num_extra_bytes_record
ZERO_BALANCE_ACCOUNT_BYTES = 770  # NEP-448: smaller accounts need no balance for storage
GAS_PRICE = 10**8  # gas_price: what burnt gas costs
GAS_PURCHASE_PRICE = 10**9  # min_gas_purchase_price: what attached gas costs up front
ACCOUNT_CREATION_CHARGE = 7 * MILLINEAR  # account_creation_charge

# Action fees in gas as (send fee, execution fee). The send fee is burnt when the transaction
# becomes a receipt; the execution fee is bought up front with the attached gas.
RECEIPT_FEE = (108_059_500_000, 108_059_500_000)
CREATE_ACCOUNT_FEE = (500_000_000_000, 7_200_000_000_000)
TRANSFER_FEE = (115_123_062_500, 115_123_062_500)
ADD_FULL_ACCESS_KEY_FEE = (101_765_125_000, 101_765_125_000)
ADD_FUNCTION_CALL_KEY_FEE = (102_217_625_000, 102_217_625_000)
DELETE_KEY_FEE = (94_946_625_000, 94_946_625_000)
DEPLOY_CONTRACT_FEE = (184_765_750_000, 184_765_750_000)
FUNCTION_CALL_FEE = (200_000_000_000, 780_000_000_000)
# Per byte (of method names and arguments, of code, of method names), as
# (send fee when the signer is the receiver, send fee otherwise, execution fee).
FUNCTION_CALL_BYTE_FEE = (2_235_934, 47_683_715, 2_235_934)
DEPLOY_CONTRACT_BYTE_FEE = (6_812_999, 47_683_715, 64_572_944)
ADD_FUNCTION_CALL_KEY_BYTE_FEE = (1_925_331, 47_683_715, 1_925_331)

# An ed25519 public key (1 + 32 bytes), the access key's nonce and permission (8 + 1) and the
# record overhead.
FULL_ACCESS_KEY_BYTES = 33 + 9 + RECORD_OVERHEAD_BYTES

# --- gas the adapters attach -----------------------------------------------------------------
# User calls are paid from the user's own account, which starts with only 0.03 NEAR, so they
# attach what the call needs with room to spare rather than the usual 100 Tgas: at the purchase
# price, 20 Tgas already takes 0.02 NEAR. A held send, a cancel or a trust change burns about
# 3 to 5 Tgas (reads and writes a few hundred bytes of state and loads the 325 kB contract).
GAS: dict[str, Gas] = {
    "send": Gas.tgas(20),  # a held payment; see PAYOUT_SEND_GAS for one that may pay out at once
    "cancel": Gas.tgas(20),
    "guardian_cancel": Gas.tgas(20),
    "add_trusted": Gas.tgas(15),
    "remove_trusted": Gas.tgas(15),
    "set_guardian": Gas.tgas(15),
    "veto_guardian_change": Gas.tgas(15),
    "extend_hold": Gas.tgas(30),
    "release": Gas.tgas(100),  # payout promise plus the on_payout callback
    "deposit_near_for": Gas.tgas(30),
    "ft_transfer_call": Gas.tgas(100),
}
# A send to a trusted payee outside the vault may be paid out in the same call, which then
# reserves 20 Tgas for on_payout, and for a token also 10 Tgas for storage_deposit and 15 Tgas
# for ft_transfer, before the send itself and the receipts' fees.
PAYOUT_SEND_GAS: dict[str, Gas] = {NEAR: Gas.tgas(40), "token": Gas.tgas(70)}

# --- what the oracle and the keeper burn per call (estimates with margin) ------------------
# extend_hold with up to 4 kB of evidence: about 3 Tgas (fees, contract loading, hashing the
# evidence, two reads and two writes). release paying a token to an account outside the vault:
# about 12 Tgas across release, storage_deposit, ft_transfer and on_payout; an internal payout
# burns about 4 Tgas.
ORACLE_CALL_GAS = Gas.tgas(5)
KEEPER_CALL_GAS = Gas.tgas(15)
ROLE_CALLS = 2_000
ORACLE_CONCURRENT_CALLS = 3  # brakes for different holds can be in flight together
KEEPER_CONCURRENT_CALLS = 1  # the keeper releases one hold at a time

# --- contract state and margins ------------------------------------------------------------
# The contract tests measured the vault's state after `new` at 286 bytes (one oracle, one token);
# a second token with a 64-character id (testnet USDC) adds it twice, to the token list and the
# caps. The HUSD token's state (owner, supply, storage prefix) plus the owner's balance record
# is about 170 bytes. Both are budgeted at 1,000 bytes.
VAULT_STATE_BYTES = 1_000
TOKEN_STATE_BYTES = 1_000
# Keeps assert_solvent passing while a failed NEAR payout's refund receipt is still on its way
# (contracts/README.md, "Known transient"), for payouts of up to 0.25 NEAR.
VAULT_MARGIN = Amount.near("0.25")
TOKEN_MARGIN = Amount.near("0.05")  # 5 kB more token state than estimated
# What the treasury keeps after deploying, so the first users can join while the faucet refills
# it; the running service asks the faucet for more below the same amount.
TREASURY_FLOAT = Amount.near("1.5")

# Gas top-ups the treasury makes when an account cannot pay for its next transaction.
USER_TOP_UP_LIMIT = Amount.near("0.1")  # the shortfall a user call may have (70 Tgas is 0.071 NEAR)
ROLE_TOP_UP = Amount.near("0.5")  # about 300 releases or 1,000 extensions
ROLE_TOP_UP_LIMIT = Amount.near("0.2")  # the shortfall a role call may have (100 Tgas is 0.1 NEAR)

_PLACEHOLDER_KEY = Ed25519KeyPair(bytes(32)).public_key


def near_to_yocto(text: str) -> int:
    """``"0.03"`` NEAR in yoctoNEAR."""
    return int(Decimal(text) * YOCTO_PER_NEAR)


def near_text(yocto: int, places: int = 4, *, up: bool = True) -> str:
    """``3.2621``: an amount in NEAR, rounded up (a cost) or down (a balance) to ``places`` decimals."""
    quantum = Decimal(1).scaleb(-places)
    rounding = ROUND_CEILING if up else ROUND_FLOOR
    return str((Decimal(yocto) / YOCTO_PER_NEAR).quantize(quantum, rounding=rounding))


def storage_cost(num_bytes: int) -> int:
    return num_bytes * STORAGE_BYTE_COST


def storage_stake(account_bytes: int) -> int:
    """The balance an account of ``account_bytes`` must keep for its storage."""
    return 0 if account_bytes <= ZERO_BALANCE_ACCOUNT_BYTES else storage_cost(account_bytes)


def available_balance(amount: int, locked: int, storage_usage: int) -> int:
    """What an account can spend: its balance minus the storage stake its locked balance does not cover."""
    return max(0, amount - max(0, storage_stake(storage_usage) - locked))


def function_call_key_bytes(receiver: str, methods: Sequence[str]) -> int:
    """Storage of a function-call key without allowance: key, nonce, permission, record overhead."""
    names = 4 + sum(4 + len(name.encode()) for name in methods)
    return 33 + 8 + 1 + 1 + 4 + len(receiver.encode()) + names + RECORD_OVERHEAD_BYTES


# ----------------------------------------------------------------------
# transactions
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class TxCost:
    """What one transaction costs its signer, in yoctoNEAR."""

    deposits: int  # NEAR it moves: transfers and attached deposits
    upfront: int  # gas the signer pays when submitting it; the unused part comes back after it runs
    burnt: int  # upper bound of the gas it finally pays (all attached gas burnt)

    @property
    def at_hand(self) -> int:
        """What the signer must hold to submit it."""
        return self.deposits + self.upfront

    @property
    def spent(self) -> int:
        """What it costs the signer once its refund is back."""
        return self.deposits + self.burnt


def _action_fees(action: Any, sir: bool) -> tuple[int, int]:
    side = 0 if sir else 1
    if isinstance(action, Action.CreateAccount):
        return CREATE_ACCOUNT_FEE
    if isinstance(action, Action.Transfer):
        return TRANSFER_FEE
    if isinstance(action, Action.FunctionCall):
        size = len(action.method_name.encode()) + len(action.args)
        return (
            FUNCTION_CALL_FEE[0] + FUNCTION_CALL_BYTE_FEE[side] * size,
            FUNCTION_CALL_FEE[1] + FUNCTION_CALL_BYTE_FEE[2] * size,
        )
    if isinstance(action, Action.DeployContract):
        size = len(action.code)
        return (
            DEPLOY_CONTRACT_FEE[0] + DEPLOY_CONTRACT_BYTE_FEE[side] * size,
            DEPLOY_CONTRACT_FEE[1] + DEPLOY_CONTRACT_BYTE_FEE[2] * size,
        )
    if isinstance(action, Action.AddKey):
        permission = action.access_key.permission
        methods = getattr(permission, "method_names", None)
        if methods is None:
            return ADD_FULL_ACCESS_KEY_FEE
        size = sum(len(name.encode()) for name in methods)
        return (
            ADD_FUNCTION_CALL_KEY_FEE[0] + ADD_FUNCTION_CALL_KEY_BYTE_FEE[side] * size,
            ADD_FUNCTION_CALL_KEY_FEE[1] + ADD_FUNCTION_CALL_KEY_BYTE_FEE[2] * size,
        )
    if isinstance(action, Action.DeleteKey):
        return DELETE_KEY_FEE
    raise ValueError(f"no fee table for {type(action).__name__}")


def tx_cost(actions: Sequence[Any], *, sir: bool = False) -> TxCost:
    """Cost of a transaction with ``actions``; ``sir`` when the signer is also the receiver."""
    send, execution = RECEIPT_FEE
    attached = deposits = creates = 0
    for action in actions:
        action_send, action_execution = _action_fees(action, sir)
        send += action_send
        execution += action_execution
        if isinstance(action, Action.FunctionCall):
            attached += action.gas
            deposits += action.deposit
        elif isinstance(action, Action.Transfer):
            deposits += action.deposit
        elif isinstance(action, Action.CreateAccount):
            creates += 1
    upfront = send * GAS_PRICE + (execution + attached) * GAS_PURCHASE_PRICE
    burnt = (send + execution + attached) * GAS_PRICE + creates * ACCOUNT_CREATION_CHARGE
    return TxCost(deposits=deposits, upfront=upfront, burnt=burnt)


def call_upfront(method: str, args: dict[str, Any], gas: int | None = None) -> int:
    """Gas a single function call bought up front (no deposit), at most."""
    return tx_cost([function_call(method, args, gas=Gas(gas if gas is not None else GAS[method]))]).upfront


def largest_extension_args() -> dict[str, Any]:
    """``extend_hold`` arguments with the largest evidence the contract accepts (4,096 bytes)."""
    return {"hold_id": "18446744073709551615", "release_at_ms": "18446744073709551615",
            "reason_bits": 4294967295, "evidence": "x" * 4096}


# ----------------------------------------------------------------------
# accounts created by a deployment
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Funding:
    """A transfer to a new account and why it is that much."""

    yocto: int
    note: str


def contract_account_funding(
    name: str, *, code_bytes: int, state_bytes: int, own_txs: Sequence[Sequence[Any]], margin: int
) -> Funding:
    """A contract account that deploys and initialises itself with a temporary key.

    It must hold the storage of its code, state, account record and the temporary key, and the
    gas its own transactions buy up front (deploying and calling ``new``, then deleting the key).
    That gas comes back after each transaction runs and stays as reserve, minus what burns.
    """
    storage_bytes = ACCOUNT_RECORD_BYTES + code_bytes + state_bytes + FULL_ACCESS_KEY_BYTES
    gas = sum(tx_cost(actions, sir=True).upfront for actions in own_txs)
    total = storage_cost(storage_bytes) + gas + margin
    note = (
        f"{name}: storage of code {code_bytes:,} B + state up to {state_bytes:,} B + account and temporary key "
        f"{ACCOUNT_RECORD_BYTES + FULL_ACCESS_KEY_BYTES} B = {near_text(storage_cost(storage_bytes))} NEAR; "
        f"{near_text(gas)} NEAR of gas for its own deploy and key deletion, bought up front; "
        f"{near_text(margin)} NEAR margin"
    )
    return Funding(total, note)


def role_account_funding(
    name: str,
    *,
    method: str,
    calls: int,
    gas_per_call: int,
    concurrent: int,
    account_bytes: int,
    args: dict[str, Any] | None = None,
) -> Funding:
    """The oracle or the keeper: gas for ``calls`` calls, plus room to buy ``concurrent`` calls' gas at once."""
    burn = calls * gas_per_call * GAS_PRICE
    upfront = concurrent * call_upfront(method, args or {"hold_id": "18446744073709551615"})
    stake = storage_stake(account_bytes)
    storage = f"storage {near_text(stake)} NEAR" if stake else f"its {account_bytes} B need no storage balance"
    note = (
        f"{name}: {calls:,} {method} calls burning at most {gas_per_call / TGAS:g} Tgas each "
        f"({near_text(gas_per_call * GAS_PRICE, 5)} NEAR) = {near_text(burn)} NEAR; "
        f"{concurrent} x {GAS[method] / TGAS:g} Tgas bought up front = {near_text(upfront)} NEAR; {storage}"
    )
    return Funding(burn + upfront + stake, note)


# ----------------------------------------------------------------------
# new users
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class OnboardingCost:
    """What the treasury pays for one new user, in yoctoNEAR."""

    steps: tuple[TxCost, ...]  # create the account, register and fund it in the vault, then each token

    @property
    def spent(self) -> int:
        return sum(step.spent for step in self.steps)

    @property
    def at_hand(self) -> int:
        """What the treasury must hold before starting: everything spent plus the gas one step buys up front."""
        return self.spent + max((step.upfront - step.burnt for step in self.steps), default=0)


def onboarding_steps(
    *, vault_id: str, user_gas: int, registration_fee: int, demo_near: int, tokens: int
) -> tuple[TxCost, ...]:
    """The treasury's transactions for one user, as ``NearVault.onboard_user`` sends them."""
    account = "u" * 64  # the longest account id, for the per-byte fees
    create = [
        create_account(),
        transfer(Amount.yocto(user_gas)),
        add_function_call_key(_PLACEHOLDER_KEY, vault_id, USER_METHODS, allowance=None),
    ]
    deposit = [
        function_call("deposit_near_for", {"account_id": account}, gas=GAS["deposit_near_for"],
                      deposit=Amount.yocto(registration_fee + demo_near))
    ]
    token = [
        function_call("ft_transfer_call",
                      {"receiver_id": vault_id, "amount": "340282366920938463463374607431768211455",
                       "msg": '{"credit_to":"' + account + '"}'},
                      gas=GAS["ft_transfer_call"], deposit=ONE_YOCTO)
    ]
    return (tx_cost(create), tx_cost(deposit), *(tx_cost(token) for _ in range(tokens)))


def onboarding_cost(
    *, vault_id: str, user_gas: int, registration_fee: int, demo_near: int, tokens: int
) -> OnboardingCost:
    return OnboardingCost(
        onboarding_steps(
            vault_id=vault_id, user_gas=user_gas, registration_fee=registration_fee, demo_near=demo_near, tokens=tokens
        )
    )


# ----------------------------------------------------------------------
# the deploy budget
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetLine:
    label: str
    yocto: int
    note: str = ""


@dataclass(frozen=True)
class DeployBudget:
    """Everything a deployment takes from the root account, line by line."""

    network: str
    profile: str
    root: str
    lines: tuple[BudgetLine, ...]

    @property
    def total(self) -> int:
        return sum(line.yocto for line in self.lines)

    def shortfall(self, available: int) -> int:
        return max(0, self.total - available)

    def describe(self) -> str:
        rows = [f"Budget of the {self.profile} deployment under {self.root} on {self.network}, in NEAR:"]
        for line in self.lines:
            rows.append(f"  {near_text(line.yocto):>9}  {line.label}")
            if line.note:
                rows.append(f"  {'':>9}  ({line.note})")
        rows.append(f"  {near_text(self.total):>9}  total")
        return "\n".join(rows)


def deploy_budget(plan: Any) -> DeployBudget:
    """What ``plan`` (a :class:`holdup.chain.deploy.DeployPlan`) needs from the root account.

    Every NEAR the root sends (transfers and attached deposits), the gas of every transaction it
    signs (all attached gas burnt, 0.007 NEAR per account it creates), the largest amount of gas
    one of its transactions buys up front before the unused part comes back, and the float the
    treasury keeps for the first users. The token and the vault pay for their own transactions
    out of what they receive.
    """
    lines: list[BudgetLine] = []
    burnt = 0
    extra_upfront = 0
    root_txs = 0
    creates = 0
    payees = 0
    for tx in plan.txs:
        if tx.signer != "root":
            continue
        cost = tx_cost(tx.actions, sir=tx.receiver == plan.root)
        root_txs += 1
        burnt += cost.burnt
        extra_upfront = max(extra_upfront, cost.upfront - cost.burnt)
        creates += sum(isinstance(action, Action.CreateAccount) for action in tx.actions)
        if getattr(tx, "payee", False):
            payees += 1
            continue
        if cost.deposits:
            lines.append(BudgetLine(tx.label, cost.deposits, getattr(tx, "note", "")))
    if payees:
        lines.append(BudgetLine(
            f"create {payees} demo payees", 0,
            f"keyless accounts of {ACCOUNT_RECORD_BYTES} B need no balance (NEP-448); "
            "only the account creation charge, counted with the root's gas",
        ))
    lines.append(BudgetLine(
        "gas the root pays", burnt,
        f"{root_txs} transactions with all attached gas burnt at {GAS_PRICE:.0e} yoctoNEAR per gas, "
        f"{creates} account creations at {near_text(ACCOUNT_CREATION_CHARGE, 3)} NEAR",
    ))
    lines.append(BudgetLine(
        "gas bought up front, returned", extra_upfront,
        f"the most one transaction buys at {GAS_PURCHASE_PRICE:.0e} yoctoNEAR per gas before its unused gas comes back",
    ))
    lines.append(BudgetLine(
        "treasury float", int(getattr(plan, "treasury_float", TREASURY_FLOAT)),
        "left with the root for the first users; the service asks the faucet for more below this",
    ))
    return DeployBudget(plan.network, plan.profile, plan.root, tuple(lines))
