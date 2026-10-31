"""Try forbidden actions with the oracle, keeper and user keys, and record how they fail.

Each attempt is a transaction its key's permission does not allow. nearcore
rejects it during validation, before anything executes, with an error such
as ``MethodNameMismatch``, ``RequiresFullAccess``, ``ReceiverMismatch`` or
``DepositWithFunctionCall``. The report lists what each key tried and the
exact error the network returned. Every attempt is harmless even if a key
were misconfigured: transfers go to the signer itself, and calls carry
1 yoctoNEAR at most.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from near import AsyncNear, KeyPairSigner, generate_key, parse_key
from near.actions import add_full_access_key, function_call, transfer
from near.errors import ContractPanicError, NearError
from near.units import Amount, Gas

from ..config import Settings
from ..models import UserAccount
from .errors import rejection_name
from .fake import FakeVault

@dataclass(frozen=True)
class Attempt:
    actor: str  # "oracle", "keeper" or "user"
    signer_id: str
    receiver_id: str
    description: str
    actions: tuple[dict[str, Any], ...]  # RPC-style action JSON
    expected: tuple[str, ...]


@dataclass
class AttemptResult:
    actor: str
    description: str
    expected: list[str]
    observed: str
    passed: bool
    detail: str = ""


@dataclass
class ProveReport:
    network: str
    vault_id: str
    results: list[AttemptResult] = field(default_factory=list)
    simulated: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r.passed for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "network": self.network,
            "vault_id": self.vault_id,
            "simulated": self.simulated,
            "ok": self.ok,
            "results": [asdict(r) for r in self.results],
        }


# ----------------------------------------------------------------------
# what to try
# ----------------------------------------------------------------------


def _call(method: str, args: dict[str, Any], deposit: str = "0") -> dict[str, Any]:
    return {"FunctionCall": {"method_name": method, "args": args, "gas": str(int(Gas.tgas(30))), "deposit": deposit}}


def plan_attempts(settings: Settings, user: UserAccount | None = None) -> list[Attempt]:
    vault = settings.vault_account
    oracle = settings.oracle_accounts[0]
    keeper = settings.keeper_account
    other = settings.husd_token or settings.usdc_token or settings.root_account
    extend_args = {"hold_id": "1", "release_at_ms": "1", "reason_bits": 1, "evidence": "{}"}
    new_key = {"AddKey": {"public_key": "", "access_key": {"nonce": 0, "permission": "FullAccess"}}}
    attempts = [
        Attempt("oracle", oracle, vault, "call send on the vault",
                (_call("send", {"payee": oracle, "asset": "near", "amount": "1"}),), ("MethodNameMismatch",)),
        Attempt("oracle", oracle, oracle, "transfer NEAR", ({"Transfer": {"deposit": "1"}},),
                ("RequiresFullAccess",)),
        Attempt("oracle", oracle, other, "call another contract",
                (_call("ft_transfer", {"receiver_id": oracle, "amount": "1"}),), ("ReceiverMismatch",)),
        Attempt("oracle", oracle, vault, "attach a deposit to extend_hold",
                (_call("extend_hold", extend_args, deposit="1"),), ("DepositWithFunctionCall",)),
        Attempt("oracle", oracle, oracle, "add a full-access key to itself", (new_key,),
                ("RequiresFullAccess",)),
        Attempt("keeper", keeper, vault, "call extend_hold", (_call("extend_hold", extend_args),),
                ("MethodNameMismatch",)),
    ]
    if user is not None:
        me = user.account_id
        attempts += [
            Attempt("user", me, vault, "call extend_hold", (_call("extend_hold", extend_args),),
                    ("MethodNameMismatch",)),
            Attempt("user", me, me, "transfer NEAR out of its account", ({"Transfer": {"deposit": "1"}},),
                    ("RequiresFullAccess",)),
            Attempt("user", me, vault, "attach a deposit to send",
                    (_call("send", {"payee": oracle, "asset": "near", "amount": "1"}, deposit="1"),),
                    ("DepositWithFunctionCall",)),
            Attempt("user", me, other, "call another contract",
                    (_call("ft_transfer", {"receiver_id": me, "amount": "1"}),), ("ReceiverMismatch",)),
        ]
    return attempts


def to_near_actions(actions: tuple[dict[str, Any], ...]) -> list[Any]:
    """Build near-kit action models from the RPC-style JSON of an attempt."""
    built: list[Any] = []
    for action in actions:
        kind, body = next(iter(action.items()))
        if kind == "FunctionCall":
            built.append(
                function_call(
                    body["method_name"],
                    body["args"],
                    gas=Gas(int(body["gas"])),
                    deposit=Amount.yocto(int(body["deposit"])),
                )
            )
        elif kind == "Transfer":
            built.append(transfer(Amount.yocto(int(body["deposit"]))))
        elif kind == "AddKey":
            built.append(add_full_access_key(body.get("public_key") or generate_key().public_key))
        else:
            raise ValueError(f"unsupported action {kind}")
    return built


def _result(attempt: Attempt, observed: str, detail: str = "") -> AttemptResult:
    return AttemptResult(
        actor=attempt.actor,
        description=attempt.description,
        expected=list(attempt.expected),
        observed=observed,
        passed=observed in attempt.expected,
        detail=detail,
    )


# ----------------------------------------------------------------------
# running the attempts
# ----------------------------------------------------------------------


def _key_for(settings: Settings, attempt: Attempt, user: UserAccount | None) -> str:
    if attempt.actor == "oracle":
        return settings.oracle_private_key
    if attempt.actor == "keeper":
        return settings.keeper_private_key
    return user.private_key if user is not None else ""


async def prove(
    settings: Settings, *, near: AsyncNear | None = None, user: UserAccount | None = None
) -> ProveReport:
    """Send each forbidden transaction to the network and record the rejection."""
    client = near or AsyncNear(
        settings.network if settings.network in ("testnet", "sandbox") else None,
        rpc_url=settings.rpc_url or None,
    )
    report = ProveReport(settings.network, settings.vault_account)
    try:
        for attempt in plan_attempts(settings, user):
            private_key = _key_for(settings, attempt, user)
            if not private_key:
                report.results.append(_result(attempt, "skipped", f"no {attempt.actor} key configured"))
                continue
            signer = KeyPairSigner(account_id=attempt.signer_id, key_pair=parse_key(private_key))
            try:
                await client.send_transaction(
                    attempt.receiver_id, to_near_actions(attempt.actions), signer=signer
                )
            except ContractPanicError as exc:
                report.results.append(_result(attempt, "executed", f"contract refused: {exc.panic}"))
            except NearError as exc:
                report.results.append(_result(attempt, rejection_name(exc.data or str(exc)), str(exc)))
            else:
                report.results.append(_result(attempt, "accepted", "the key was allowed to do this"))
    finally:
        if near is None:
            await client.aclose()
    return report


def prove_fake(fake: FakeVault, settings: Settings, *, user: UserAccount | None = None) -> ProveReport:
    """The same attempts against the in-memory vault's access-key model (simulated)."""
    report = ProveReport(settings.network, settings.vault_account, simulated=True)
    for attempt in plan_attempts(settings, user):
        error = fake.check_transaction(attempt.signer_id, attempt.receiver_id, list(attempt.actions))
        if error is None:
            report.results.append(_result(attempt, "accepted", "the key was allowed to do this"))
        else:
            report.results.append(_result(attempt, rejection_name(error), error["message"]))
    return report
