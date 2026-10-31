"""The models' findings are weighed like the rules' signals: checked evidence brakes alone, a judgement only
when something independent agrees, a payee claim only as far as the facts go. The cases come from the first
live runs against Otari (2026-10-31), where single unchecked flags braked honest payments, and from the
reviews of the weighing that followed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from test_pipeline import (
    HERO,
    INVOICE,
    LEGIT_INVOICE,
    TRIAGE_CLEAN,
    Rules,
    answer,
    by,
    investigator_answer,
    make_hold,
    make_risk,
    scenario_dir,
)

from holdup.models import NEAR, Finding, Hold, PaymentContext, Reason, StepRecord, Verdict
from holdup.risk import pipeline as P
from holdup.risk.otari import INVESTIGATOR_MODEL, REVIEWER_MODEL, TRIAGE_MODEL, VISION_MODEL
from holdup.risk.rules import CONTEXT, STRONG, WEAK, facts_read, signal_weight

HOUR = 3_600_000
BITPAY = "https://support.bitpay.com/hc/en-us/articles/360004102011-What-is-testnet-How-do-I-avoid-testnet-scams"
BITPAY_TITLE = "What is testnet? How do I avoid testnet scams? – BitPay Support"
YOUNG = Finding(Reason.NEW_ACCOUNT, "created about 28 h ago", "rules", context=True)
BUSY = Finding(Reason.MULE_PATTERN, "15 different senders paid it in the last 24 h", "rules", context=True)
DEADLINE = Finding(Reason.URGENCY, "a short deadline", "rules", context=True)
RENT = (
    "Hi, it's Maria, your landlord. Rent for November is 950 USDC, same account as always. "
    "The late fee starts tomorrow, so please send it today if you can."
)
KRAKEN = (
    "Forwarded from Kraken:\nHello, this is Kraken. Your account review is complete. To activate withdrawals "
    "please send the 50 USDC activation deposit to kraken-desk.near. It is credited back with your first withdrawal."
)
TASKS = (
    "Hi! Great work on the first three tasks. To unlock the next task batch and your 340 USDC commission, the "
    "platform needs a 95 USDC task deposit. Send it to tasks-desk.near and I will release your commission tonight."
)
BACKUP = (
    "Hi, Ravi here from the wallet team. To restore your access I need the backup card words you got at setup, "
    "all of them in order, then send the 30 USDC fee to ravi-help.near."
)
FAILED = {"by_model": {"*": {"status": 502, "body": {"detail": "All upstream providers failed"}}}}


class ReadRules(Rules):
    """The rules' stand-in, with the sources its payee lookup answered from on record."""

    def __init__(self, *findings: Finding, sources: str = "chain+fastnear+vault", hold_for_ms: int = 0) -> None:
        super().__init__(*findings, hold_for_ms=hold_for_ms)
        self.sources = sources

    async def assess(self, hold: Hold, ctx: PaymentContext) -> Verdict:
        verdict = await super().assess(hold, ctx)
        verdict.steps = [StepRecord(step="precheck", tool_calls=[{"name": "payee_profile", "sources": self.sources}])]
        return verdict


def triage(**signals: bool) -> dict[str, Any]:
    content = {**TRIAGE_CLEAN, "categories": ["payment_request"], "risk": "medium", **signals}
    return {"response": {"headers": {"otari-provider": "deepinfra"}, "content": content}}


def reviewer(brake: bool, *reasons: tuple[str, str], confidence: str = "medium") -> dict[str, Any]:
    content = {
        "brake": brake,
        "reasons": [{"reason": r, "detail": d} for r, d in reasons],
        "amount_check": None,
        "confidence": confidence,
        "summary": "A second opinion.",
    }
    return {"response": answer(content, "0.010085")}


def search(*results: tuple[str, str]) -> dict[str, Any]:
    sources = [{"url": url, "title": title, "published_date": None} for url, title in results]
    return {"id": "otari_ws_1", "type": "web_search", "status": "completed", "query": "q", "sources": sources}


def warning(*urls: str) -> dict[str, Any]:
    return {"reason": "PUBLIC_WARNING", "tool": "web_search", "detail": "public warnings describe this pattern",
            "citations": list(urls)}


def investigation(*findings: dict[str, Any], calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    content = {"findings": list(findings), "computations": [], "amount_check": None, "brake": bool(findings),
               "summary": "Checked the payee and the message."}
    return investigator_answer(content, calls)


def found(verdict: Any) -> list[tuple[Reason, str, bool]]:
    return [(f.reason, f.source, f.context) for f in verdict.findings]


def reasons(verdict: Any) -> set[Reason]:
    return {f.reason for f in verdict.findings if not f.context}


async def check(tmp_path: Path, steps: dict[str, Any], text: str, payee: str, units: float, **kwargs: Any) -> Any:
    """One payment checked with these step answers; each call gets its own folder, since fixtures are cached."""
    folder = tmp_path / f"case{len(list(tmp_path.iterdir()))}"
    folder.mkdir()
    risk, transport = make_risk("case", fixtures_dir=scenario_dir(folder, "case", steps), **kwargs)
    verdict = await risk.assess(make_hold(payee, units), PaymentContext(forwarded_text=text))
    return verdict, transport


# ----------------------------------------------------------------------
# what the first live runs got wrong
# ----------------------------------------------------------------------


async def test_a_single_impersonation_flag_on_an_honest_invoice_is_context(tmp_path: Path) -> None:
    """Live: triage called the supplier's own invoice impersonation; the investigator and the reviewer were clean."""
    steps = {"triage": triage(impersonation=True), "reviewer": reviewer(False)}
    risk, transport = make_risk("invoice1", fixtures_dir=scenario_dir(tmp_path, "invoice1", steps))
    ctx = PaymentContext(forwarded_text=LEGIT_INVOICE, trusted_payees=["northwindpaper.testnet"])
    verdict = await risk.assess(make_hold("northwindpaper.testnet", 1150), ctx)

    assert not verdict.brake and verdict.hold_for_ms == 0 and verdict.reason_bits == 0
    assert found(verdict) == [(Reason.IMPERSONATION, "triage", True)]  # still shown, labelled as context
    assert "triage: nothing found, 1 as context" in verdict.summary


async def test_a_generic_search_result_is_no_public_warning_on_its_own(tmp_path: Path) -> None:
    """Live: a search for the payee returned a page on testnet scams that names neither it nor Northwind."""
    calls = [search((BITPAY, BITPAY_TITLE))]
    steps = {"triage": triage(impersonation=True), "investigator": investigation(warning(BITPAY), calls=calls)}
    verdict, _ = await check(tmp_path, steps, LEGIT_INVOICE, "northwindpaper.testnet", 50)

    # Two judgements of one model family and a clean reviewer: neither brakes.
    assert not verdict.brake and verdict.reason_bits == 0
    assert set(found(verdict)) == {
        (Reason.IMPERSONATION, "triage", True),
        (Reason.PUBLIC_WARNING, "web_search", True),
    }
    investigator = next(s for s in verdict.steps if s.step == "investigator")
    assert investigator.tool_calls[0]["titles"] == {BITPAY: BITPAY_TITLE}


async def test_a_warning_that_names_the_payee_brakes_alone(tmp_path: Path) -> None:
    page = "https://scam-reports.example/accounts/northwind-paper-testnet"
    calls = [search((page, "Wallet northwind-paper.testnet reported for invoice redirection"))]
    steps = {"investigator": investigation(warning(page), calls=calls)}
    verdict, _ = await check(tmp_path, steps, INVOICE, "northwind-paper.testnet", 50)
    assert verdict.brake and verdict.hold_for_ms == P.DAY_MS
    assert found(verdict) == [(Reason.PUBLIC_WARNING, "web_search", False)]


async def test_a_reviewer_claim_the_payee_facts_contradict_is_left_out(tmp_path: Path) -> None:
    """Live (sb-scam-064): the reviewer called a 28-hour-old payee with five senders a money mule (the rules need
    eight). A claim HoldUp's own lookup contradicts is no context either: no page may show it as a fact."""
    review = reviewer(
        True,
        ("MULE_PATTERN", "Payee is 28 hours old with five distinct senders."),
        ("NEW_ACCOUNT", "The account is only about a day old."),
    )
    aged, _ = await check(tmp_path, {"reviewer": review}, RENT, "fastship-agent-31.testnet", 600, rules=ReadRules())
    assert not aged.brake and aged.reason_bits == 0
    assert found(aged) == []  # both claims contradicted: the payee is neither young nor busy
    assert "reviewer: nothing found, 2 the payee facts contradict" in aged.summary

    # The account is young, as the reviewer says: its answer that the payment should wait and the young payee
    # are two independent signals (review of the weighing), so the payment brakes, on the true claim only.
    young, _ = await check(
        tmp_path, {"reviewer": review}, RENT, "fastship-agent-31.testnet", 600, rules=ReadRules(YOUNG)
    )
    assert young.brake and young.hold_for_ms == P.DAY_MS
    assert set(found(young)) == {(Reason.NEW_ACCOUNT, "reviewer", False), (Reason.NEW_ACCOUNT, "rules", True)}


async def test_a_reviewer_that_sees_no_reason_to_hold_gives_no_judgement(tmp_path: Path) -> None:
    """Live (sb-benign-012): brake false, confidence low, yet two reasons listed; they used to count."""
    review = reviewer(
        False,
        ("URGENCY", "Message cites a late fee starting tomorrow to hurry the payer."),
        ("DETAILS_CHANGED", "Asks for USDC in an amount matching the HUSD payment, differing from stated token."),
        confidence="low",
    )
    urgent = investigation({"reason": "URGENCY", "tool": "message", "detail": "a late fee"})
    steps = {"triage": triage(urgency=True), "investigator": urgent, "reviewer": review}
    verdict, transport = await check(tmp_path, steps, RENT, "maria-lets.testnet", 950)

    # Triage and the investigator are one model family reading one message: no second opinion.
    assert not verdict.brake and verdict.reason_bits == 0
    assert all(f.context for f in verdict.findings)
    assert {(f.reason, f.source) for f in verdict.findings} == {
        (Reason.URGENCY, "triage"),
        (Reason.URGENCY, "investigator"),
        (Reason.URGENCY, "reviewer"),
        (Reason.DETAILS_CHANGED, "reviewer"),
    }
    assert [r.step for r in transport.requests].count("reviewer") == 1


async def test_a_reviewer_that_answers_no_still_keeps_a_request_for_a_secret(tmp_path: Path) -> None:
    """Saying brake false is one word an injected message could win; what the message asks for stays."""
    review = reviewer(False, ("SECRET_REQUEST", "Asks for the backup words of the wallet."), confidence="low")
    verdict, _ = await check(tmp_path, {"reviewer": review}, BACKUP, "ravi-help.near", 300)
    assert verdict.brake and verdict.hold_for_ms == P.MAX_HOLD_MS
    assert found(verdict) == [(Reason.SECRET_REQUEST, "reviewer", False)]


async def test_the_reviewers_changed_details_need_another_family(tmp_path: Path) -> None:
    """Live: every changed-details claim of the reviewer misread the payment (a demo token, an overpayment)."""
    review = reviewer(True, ("DETAILS_CHANGED", "Asks for USDC while the payment is in HUSD."))
    alone, _ = await check(tmp_path, {"reviewer": review}, RENT, "maria-lets.testnet", 950)
    assert not alone.brake and found(alone) == [(Reason.DETAILS_CHANGED, "reviewer", True)]

    steps = {"triage": triage(urgency=True), "reviewer": review}
    paired, _ = await check(tmp_path, steps, RENT, "maria-lets.testnet", 950)
    assert paired.brake and reasons(paired) == {Reason.DETAILS_CHANGED, Reason.URGENCY, Reason.SECOND_OPINION}

    steps = {"triage": triage(details_changed=True)}
    from_triage, _ = await check(tmp_path, steps, RENT, "maria-lets.testnet", 950)
    assert from_triage.brake and reasons(from_triage) == {Reason.DETAILS_CHANGED}


# ----------------------------------------------------------------------
# two model families that each see a scam agree, whatever they call it
# ----------------------------------------------------------------------


async def test_a_model_of_another_family_agreeing_on_the_reason_makes_it_one(tmp_path: Path) -> None:
    review = reviewer(True, ("URGENCY", "Deadline pressure pushes a rushed transfer."))
    steps = {"triage": triage(urgency=True), "reviewer": review}
    verdict, transport = await check(tmp_path, steps, RENT, "maria-lets.testnet", 12.5)

    assert [r.step for r in transport.requests] == ["triage", "investigator", "reviewer"]  # they disagreed
    assert verdict.brake and verdict.hold_for_ms == P.DAY_MS
    assert found(verdict) == [
        (Reason.URGENCY, "triage", False),
        (Reason.URGENCY, "reviewer", False),
        (Reason.SECOND_OPINION, "reviewer", False),
    ]


async def test_another_family_agrees_when_it_names_the_scam_differently(tmp_path: Path) -> None:
    """Review: triage read impersonation, the reviewer a payee that is not the exchange's; both see a scam."""
    lookalike = reviewer(True, ("LOOKALIKE_PAYEE", "The payee is not an account of the exchange it claims."))
    steps = {"triage": triage(impersonation=True), "reviewer": lookalike}
    verdict, _ = await check(tmp_path, steps, KRAKEN, "kraken-desk.near", 50, rules=ReadRules())
    assert verdict.brake and verdict.hold_for_ms == P.DAY_MS
    assert reasons(verdict) == {Reason.IMPERSONATION, Reason.LOOKALIKE_PAYEE, Reason.SECOND_OPINION}

    steps = {"triage": triage(urgency=True), "reviewer": reviewer(True, ("IMPERSONATION", "Poses as a platform."))}
    verdict, _ = await check(tmp_path, steps, TASKS, "tasks-desk.near", 95)
    assert verdict.brake and reasons(verdict) == {Reason.URGENCY, Reason.IMPERSONATION, Reason.SECOND_OPINION}


async def test_three_models_of_two_families_flagging_brake(tmp_path: Path) -> None:
    impersonation = {"reason": "IMPERSONATION", "tool": "message", "detail": "poses as a platform"}
    page = "https://consumer.example/task-scams"
    steps = {
        "triage": triage(impersonation=True),
        "investigator": investigation(impersonation, warning(page), calls=[search((page, "Job scams: pay to work"))]),
        "reviewer": reviewer(True, ("URGENCY", "Pushes to pay tonight.")),
    }
    verdict, _ = await check(tmp_path, steps, TASKS, "tasks-desk.near", 95)
    assert verdict.brake and verdict.hold_for_ms == P.MAX_HOLD_MS  # three reasons
    assert reasons(verdict) == {
        Reason.IMPERSONATION, Reason.PUBLIC_WARNING, Reason.URGENCY, Reason.SECOND_OPINION
    }


async def test_the_reviewer_alone_is_one_model(tmp_path: Path) -> None:
    """A large payment both gpt-oss steps found clean: the reviewer's own readings wait for a second family."""
    review = reviewer(True, ("IMPERSONATION", "Poses as a task platform."), ("URGENCY", "Pushes to pay tonight."))
    verdict, _ = await check(tmp_path, {"reviewer": review}, TASKS, "tasks-desk.near", 500)
    assert not verdict.brake
    assert set(found(verdict)) == {(Reason.IMPERSONATION, "reviewer", True), (Reason.URGENCY, "reviewer", True)}


async def test_a_judgement_that_waits_for_agreement_gets_the_reviewer_on_a_small_payment(tmp_path: Path) -> None:
    impersonation = {"reason": "IMPERSONATION", "tool": "message", "detail": "claims to be the bank"}
    review = reviewer(True, ("IMPERSONATION", "The sender claims to be the bank but writes from a new account."))
    steps = {"triage": triage(impersonation=True), "investigator": investigation(impersonation), "reviewer": review}
    verdict, transport = await check(tmp_path, steps, RENT, "ravi.testnet", 12.5)

    # Triage and the investigator agree, so they do not disagree; the reviewer is asked all the same.
    assert [r.step for r in transport.requests] == ["triage", "investigator", "reviewer"]
    assert verdict.brake and verdict.reason_bits == int(Reason.IMPERSONATION | Reason.SECOND_OPINION)


async def test_when_triage_falls_back_to_the_reviewers_family_a_third_family_reviews(tmp_path: Path) -> None:
    """Review: triage served by deepseek, then a deepseek reviewer, could never agree across families."""
    impersonation = triage(impersonation=True)["response"]
    down = {"by_model": {TRIAGE_MODEL: {"status": 502, "body": {"detail": "down"}}, "*": impersonation}}
    steps = {"triage": down, "reviewer": reviewer(True, ("IMPERSONATION", "Poses as the exchange."))}
    verdict, transport = await check(tmp_path, steps, KRAKEN, "kraken-desk.near", 50)

    served = {r.step: r.model for r in transport.requests}
    assert served["triage"] == REVIEWER_MODEL and served["reviewer"] == VISION_MODEL
    assert verdict.brake and reasons(verdict) == {Reason.IMPERSONATION, Reason.SECOND_OPINION}


async def test_the_reviewers_answer_that_the_payment_should_wait_counts_whatever_it_lists(tmp_path: Path) -> None:
    """Red-team review (RO1a, J1b, Q1a): deepseek answered brake=true naming the payee's age, nothing, or a reason
    it has no tool to show; the judgement it was asked to settle stayed context and the payment released."""
    answers = (
        reviewer(True, ("NEW_ACCOUNT", "The account is only a day old.")),  # contradicted: the payee is not young
        reviewer(True),
        reviewer(True, ("PUBLIC_WARNING", "Matches known task scams.")),  # no tool to show it: dropped
    )
    for review in answers:
        steps = {"triage": triage(urgency=True), "reviewer": review}
        verdict, transport = await check(tmp_path, steps, TASKS, "tasks-desk.near", 95, rules=ReadRules())
        assert "reviewer" in [r.step for r in transport.requests]
        assert verdict.brake and verdict.hold_for_ms == P.DAY_MS, review
        assert reasons(verdict) == {Reason.URGENCY, Reason.SECOND_OPINION}

    steps = {"triage": triage(urgency=True), "reviewer": reviewer(False)}
    released, _ = await check(tmp_path, steps, TASKS, "tasks-desk.near", 95, rules=ReadRules())
    assert not released.brake


async def test_a_young_payee_backs_the_reviewer_when_the_gpt_oss_steps_read_nothing(tmp_path: Path) -> None:
    """Red-team review (sb-scam-075): 800 USDC to repay a mistaken deposit, payee 9 h old, triage clean."""
    text = "Hi, I sent you 800 USDC by mistake. Please send it back to my other wallet tasks-desk.near today."
    review = reviewer(True, ("URGENCY", "Pressure to return a deposit at once."))
    young, _ = await check(tmp_path, {"reviewer": review}, text, "tasks-desk.near", 800, rules=ReadRules(YOUNG))
    assert young.brake and young.hold_for_ms == P.DAY_MS
    assert found(young) == [(Reason.NEW_ACCOUNT, "rules", True), (Reason.URGENCY, "reviewer", False)]

    aged, _ = await check(tmp_path, {"reviewer": review}, text, "tasks-desk.near", 800, rules=ReadRules())
    assert not aged.brake  # the reviewer alone is one model


async def test_a_step_a_fallback_served_waits_for_the_reviewer(tmp_path: Path) -> None:
    """Honest review (H12): gpt-oss-120b down, the investigator served by deepseek repeated triage's reading of a
    piano tuner's due date, and the two families braked 24 h without any reviewer being asked."""
    urgent = investigation({"reason": "URGENCY", "tool": "message", "detail": "a due date"})["response"]
    fallback = {"by_model": {INVESTIGATOR_MODEL: {"status": 502, "body": {"detail": "down"}}, "*": urgent}}
    for review, brakes in ((reviewer(False), False), (reviewer(True), True)):
        steps = {"triage": triage(urgency=True), "investigator": fallback, "reviewer": review}
        verdict, transport = await check(tmp_path, steps, RENT, "ravi.testnet", 12.5)
        served = {r.step: r.model for r in transport.requests}
        assert served["investigator"] == REVIEWER_MODEL and served["reviewer"] == VISION_MODEL  # a third family
        assert verdict.brake is brakes and all(f.context for f in verdict.findings) is not brakes


async def test_a_link_python_refuses_to_split_no_longer_fails_the_check(tmp_path: Path) -> None:
    """Code review: a junk link with a full-width mark made the check fail closed, so a secret request that held
    72 h released at +6 h with only AI_UNAVAILABLE on chain."""
    text = BACKUP + " Ref https://wallet-help\uff03.com/restore"
    verdict, _ = await check(tmp_path, {"triage": triage(secret_request=True)}, text, "ravi-help.near", 30)
    assert verdict.brake and verdict.hold_for_ms == P.MAX_HOLD_MS
    assert reasons(verdict) == {Reason.SECRET_REQUEST}

    honest = "\u623f\u79df950\uff0c\u4f60\u770b\u8fc7https://shop.example.com\uff1f\u8c22\u8c22"
    steps = {"triage": triage(urgency=True), "reviewer": reviewer(False)}
    verdict, _ = await check(tmp_path, steps, honest, "maria-lets.testnet", 12.5)
    assert not verdict.brake and not verdict.summary.startswith("fail-closed")


async def test_a_warning_that_names_the_telegram_page_of_the_message_brakes(tmp_path: Path) -> None:
    """Red-team review (S3a): the report names the exact t.me handle the message sends the sender to."""
    text = "Binance Support here. Pay the 60 USDC fee to unlock-desk-24.near, then message t.me/BinanceHelpDesk_247"
    page = "https://scam-reports.example/r/8812"
    calls = [search((page, "t.me/BinanceHelpDesk_247 - fake Binance support, took my fee"))]
    steps = {"triage": triage(impersonation=True), "investigator": investigation(warning(page), calls=calls),
             "reviewer": reviewer(False)}
    verdict, _ = await check(tmp_path, steps, text, "unlock-desk-24.near", 60, rules=ReadRules())
    assert verdict.brake and verdict.hold_for_ms == P.DAY_MS
    assert reasons(verdict) == {Reason.PUBLIC_WARNING, Reason.IMPERSONATION}


async def test_a_weighing_that_raises_counts_every_finding(tmp_path: Path, monkeypatch: Any) -> None:
    """Defence in depth (code review): an error while weighing leaves the brake on every finding, as before
    findings were weighed, rather than failing the whole check closed for at most six hours."""

    def broken(*args: Any, **kwargs: Any) -> list[Finding]:
        raise ValueError("a bug in the weighing")

    monkeypatch.setattr(P, "weigh_claims", broken)
    steps = {"triage": triage(urgency=True), "reviewer": reviewer(False)}
    verdict, _ = await check(tmp_path, steps, RENT, "maria-lets.testnet", 12.5)
    assert verdict.brake and verdict.hold_for_ms == P.DAY_MS and not verdict.summary.startswith("fail-closed")
    assert reasons(verdict) == {Reason.URGENCY}


# ----------------------------------------------------------------------
# the rules backing a judgement
# ----------------------------------------------------------------------


async def test_a_rules_signal_of_another_reason_backs_a_judgement(tmp_path: Path) -> None:
    folder = scenario_dir(tmp_path, "pair", {"triage": triage(impersonation=True)})
    risk, _ = make_risk("pair", fixtures_dir=folder, rules=ReadRules(DEADLINE))
    verdict = await risk.assess(make_hold("ravi.testnet", 12.5), PaymentContext(forwarded_text=RENT))
    assert verdict.brake and verdict.hold_for_ms == P.DAY_MS
    assert found(verdict) == [(Reason.IMPERSONATION, "triage", False), (Reason.URGENCY, "rules", True)]


async def test_the_same_deadline_seen_twice_young_or_busy_payees_back_no_judgement(tmp_path: Path) -> None:
    """Fresh probes: a gym fee due within 24 hours, a new photographer's wallet, a busy food bank."""
    folder = scenario_dir(tmp_path, "pair", {"triage": triage(urgency=True)})
    for rules in (ReadRules(DEADLINE), ReadRules(YOUNG), ReadRules(BUSY)):
        risk, transport = make_risk("pair", fixtures_dir=folder, rules=rules)
        verdict = await risk.assess(make_hold("ravi.testnet", 12.5), PaymentContext(forwarded_text=RENT))
        assert not verdict.brake and all(f.context for f in verdict.findings), rules.findings
        assert [r.step for r in transport.requests] == ["triage", "investigator", "reviewer"]  # it waited


async def test_what_the_message_says_still_brakes_alone(tmp_path: Path) -> None:
    for signal in ("secret_request", "details_changed", "injection_suspected", "impossible_returns"):
        folder = scenario_dir(tmp_path, signal, {"triage": triage(**{signal: True})})
        risk, _ = make_risk(signal, fixtures_dir=folder)
        verdict = await risk.assess(make_hold("ravi.testnet", 12.5), PaymentContext(forwarded_text=RENT))
        assert verdict.brake and not any(f.context for f in verdict.findings), signal


async def test_a_strong_finding_makes_the_judgements_beside_it_reasons() -> None:
    risk, _ = make_risk("hero")
    verdict = await risk.assess(make_hold("safe-wallet-77.testnet", 50), PaymentContext(forwarded_text=HERO))
    triage_found = {(f.reason, f.context) for f in by(verdict, "triage")}
    assert triage_found == {(Reason.URGENCY, False), (Reason.IMPERSONATION, False), (Reason.CHECKER_INJECTION, False)}
    (public,) = by(verdict, "web_search")  # names nothing of this payment, but a phishing page is there
    assert public.reason == Reason.PUBLIC_WARNING and not public.context


async def test_a_failed_step_with_only_context_findings_fails_closed(tmp_path: Path) -> None:
    steps = {"triage": triage(impersonation=True, urgency=True), "investigator": FAILED}
    verdict, _ = await check(tmp_path, steps, RENT, "ravi.testnet", 12.5, rules=Rules(BUSY, hold_for_ms=0))
    # The engine retries first; the day is what it holds once the retries are spent (test_engine_failclosed).
    assert verdict.brake and verdict.hold_for_ms == P.DAY_MS and verdict.summary.startswith("fail-closed")
    assert verdict.summary.endswith("a judgement waited for the failed step")
    assert verdict.findings[0].reason == Reason.AI_UNAVAILABLE
    assert {f.reason for f in verdict.findings[1:] if f.context} == {
        Reason.IMPERSONATION, Reason.URGENCY, Reason.MULE_PATTERN
    }

    clean, _ = await check(tmp_path, {"investigator": FAILED}, RENT, "ravi.testnet", 12.5)
    assert clean.brake and clean.hold_for_ms == HOUR  # nothing waited: the engine's retries alone


async def test_a_judgement_that_waits_for_a_reviewer_that_fails_fails_closed(tmp_path: Path) -> None:
    """Review (O1a): deepseek and its fallback down; the judgement used to brake 24 h at once."""
    steps = {"triage": triage(impersonation=True), "reviewer": FAILED}
    verdict, transport = await check(tmp_path, steps, RENT, "ravi.testnet", 12.5)
    assert "reviewer" in [r.step for r in transport.requests]
    assert verdict.brake and verdict.hold_for_ms == P.DAY_MS and verdict.summary.startswith("fail-closed")
    assert verdict.findings[0].reason == Reason.AI_UNAVAILABLE and verdict.reason_bits == int(Reason.AI_UNAVAILABLE)


# ----------------------------------------------------------------------
# claims about the payee
# ----------------------------------------------------------------------


async def test_payee_claims_the_rules_could_not_read_wait_for_another_family(tmp_path: Path) -> None:
    """Review: the transaction index and the vault timed out at rules time; near-intel answered the investigator."""
    reported = investigation(
        {"reason": "PAYEE_REPORTED", "tool": "near-intel", "detail": "four senders reported it this week"},
        {"reason": "NEW_ACCOUNT", "tool": "near-intel", "detail": "created three hours ago"},
    )
    agrees = reviewer(True, ("PAYEE_REPORTED", "Other senders reported this payee."))
    unread = ReadRules(sources="chain")
    verdict, transport = await check(tmp_path, {"investigator": reported, "reviewer": agrees}, TASKS,
                                     "tasks-desk.near", 95, rules=unread)
    assert "reviewer" in [r.step for r in transport.requests]
    assert verdict.brake and {Reason.PAYEE_REPORTED, Reason.NEW_ACCOUNT} <= reasons(verdict)

    verdict, _ = await check(tmp_path, {"investigator": reported, "reviewer": agrees}, TASKS, "tasks-desk.near", 95,
                             rules=ReadRules())
    assert not verdict.brake  # the rules read the facts and found neither: the claims are contradicted


async def test_a_brand_lookalike_payee_is_a_judgement(tmp_path: Path) -> None:
    """Review: binance-refunds.near is no trusted payee's look-alike, but two families call it the exchange's fake."""
    text = "Hello from the Binance refunds team. A 120 USDC processing fee applies; send it to binance-refunds.near."
    lookalike = {"reason": "LOOKALIKE_PAYEE", "tool": "message", "detail": "imitates the exchange"}
    agrees = reviewer(True, ("LOOKALIKE_PAYEE", "Payee imitates the exchange it claims to be."))
    verdict, _ = await check(tmp_path, {"investigator": investigation(lookalike), "reviewer": agrees}, text,
                             "binance-refunds.near", 120, rules=ReadRules())
    assert verdict.brake and Reason.LOOKALIKE_PAYEE in reasons(verdict)

    alone, _ = await check(tmp_path, {"investigator": investigation(lookalike)}, text, "binance-refunds.near", 120,
                           rules=ReadRules())
    assert not alone.brake


# ----------------------------------------------------------------------
# the payment facts
# ----------------------------------------------------------------------


async def test_the_facts_say_that_the_demo_token_stands_in_for_usdc() -> None:
    """Live: told only that the payment was in HUSD, the reviewer read USDC in the message as changed details."""
    risk, transport = make_risk("benign")
    await risk.assess(make_hold("ravi.testnet", 12.5), PaymentContext(forwarded_text=RENT))
    facts = transport.requests[0].body["messages"][1]["content"].split("\n\n", 1)[0]
    line = next(x for x in facts.splitlines() if x.startswith("- amount:"))
    stand_in = "HUSD is the HoldUp demo token that stands in for USDC: a message asking for USDC means it"
    assert line == f"- amount: 12.5 HUSD ({stand_in})"
    assert "'" not in facts and '"' not in facts and chr(0x60) not in facts  # nothing for the gateway firewall

    risk, transport = make_risk("benign")
    await risk.assess(make_hold("ravi.testnet", 2, asset=NEAR), PaymentContext(forwarded_text=RENT))
    assert "- amount: 2 NEAR\n" in transport.requests[0].body["messages"][1]["content"]


# ----------------------------------------------------------------------
# the parts
# ----------------------------------------------------------------------


def claim(reason: Reason, source: str, model: str | None = None) -> Finding:
    return Finding(reason, "", source, model)


def test_judgements_need_something_independent() -> None:
    urgency = claim(Reason.URGENCY, "triage", TRIAGE_MODEL)
    assert P.weigh_claims([(urgency, P.JUDGEMENT)], [])[0].context
    secret = claim(Reason.SECRET_REQUEST, "web_fetch", INVESTIGATOR_MODEL)
    assert [f.context for f in P.weigh_claims([(urgency, P.JUDGEMENT), (secret, STRONG)], [])] == [False, False]
    reported = Finding(Reason.PAYEE_REPORTED, "2 reports", "rules")
    assert not P.weigh_claims([(urgency, P.JUDGEMENT)], [reported])[0].context  # the rules brake anyway

    same = claim(Reason.URGENCY, "investigator", INVESTIGATOR_MODEL)
    assert all(f.context for f in P.weigh_claims([(urgency, P.JUDGEMENT), (same, P.JUDGEMENT)], []))
    # Two steps of other families that are not the reviewer (vision and triage, or a step a fallback served)
    # wait for it: the vision prompt calls any business claim it cannot verify impersonation (honest review).
    other = claim(Reason.URGENCY, "vision", VISION_MODEL)
    assert all(f.context for f in P.weigh_claims([(urgency, P.JUDGEMENT), (other, P.JUDGEMENT)], []))
    different = claim(Reason.IMPERSONATION, "reviewer", REVIEWER_MODEL)
    assert not any(f.context for f in P.weigh_claims([(urgency, P.JUDGEMENT), (different, P.JUDGEMENT)], []))
    weighed = P.weigh_claims([(urgency, P.JUDGEMENT), (other, P.JUDGEMENT)], [], [REVIEWER_MODEL])
    assert not any(f.context for f in weighed)
    dismissed = claim(Reason.URGENCY, "reviewer", REVIEWER_MODEL)
    assert all(f.context for f in P.weigh_claims([(urgency, P.JUDGEMENT), (dismissed, CONTEXT)], []))


def test_a_reviewer_that_answers_that_the_payment_should_wait_backs_a_judgement_whatever_it_lists() -> None:
    """Red-team review: a deepseek brake=true naming only payee facts, a reason it cannot show, or none at all,
    counted for nothing, and the judgement it was asked to settle released the payment."""
    urgency = claim(Reason.URGENCY, "triage", TRIAGE_MODEL)
    assert not P.weigh_claims([(urgency, P.JUDGEMENT)], [], [REVIEWER_MODEL])[0].context
    assert P.weigh_claims([(urgency, P.JUDGEMENT)], [], [INVESTIGATOR_MODEL])[0].context  # one family
    young = claim(Reason.NEW_ACCOUNT, "reviewer", REVIEWER_MODEL)
    marked = P.weigh_claims([(urgency, P.JUDGEMENT), (young, WEAK)], [YOUNG], [REVIEWER_MODEL])
    assert [f.context for f in marked] == [False, False]


def test_a_young_payee_backs_the_reviewer_only() -> None:
    """Red-team review (sb-scam-075): 800 USDC to a 9 h old payee that only the reviewer read as a scam."""
    urgency = claim(Reason.URGENCY, "reviewer", REVIEWER_MODEL)
    assert not P.weigh_claims([(urgency, P.JUDGEMENT)], [YOUNG], [REVIEWER_MODEL])[0].context
    assert P.weigh_claims([(urgency, P.JUDGEMENT)], [], [REVIEWER_MODEL])[0].context  # the reviewer alone
    from_triage = claim(Reason.URGENCY, "triage", TRIAGE_MODEL)
    assert P.weigh_claims([(from_triage, P.JUDGEMENT)], [YOUNG])[0].context
    assert P.rules_agree(urgency, [YOUNG], young=True) and not P.rules_agree(urgency, [YOUNG])


def test_a_weak_rules_signal_backs_a_judgement_of_another_reason_only() -> None:
    urgency = claim(Reason.URGENCY, "triage", TRIAGE_MODEL)
    impersonation = claim(Reason.IMPERSONATION, "triage", TRIAGE_MODEL)
    assert P.rules_agree(impersonation, [DEADLINE])
    assert not P.rules_agree(urgency, [DEADLINE])  # one deadline, seen twice
    assert not P.rules_agree(urgency, [YOUNG])  # payments to new accounts are what HoldUp holds
    assert not P.rules_agree(urgency, [BUSY])
    assert P.weigh_claims([(impersonation, P.JUDGEMENT)], [DEADLINE, YOUNG])[0].context is False


def test_payee_facts_count_once_the_payment_brakes_for_something_else() -> None:
    young = claim(Reason.NEW_ACCOUNT, "mcp", INVESTIGATOR_MODEL)
    assert P.weigh_claims([(young, WEAK)], [YOUNG])[0].context  # the same young account, said twice
    impersonation = claim(Reason.IMPERSONATION, "triage", TRIAGE_MODEL)
    marked = P.weigh_claims([(young, WEAK), (impersonation, P.JUDGEMENT)], [YOUNG, DEADLINE])
    assert [f.context for f in marked] == [False, False]
    busy = claim(Reason.MULE_PATTERN, "mcp", INVESTIGATOR_MODEL)
    assert P.weigh_claims([(busy, CONTEXT), (impersonation, P.JUDGEMENT)], [YOUNG, DEADLINE])[0].context


def test_the_weight_of_a_rules_finding_before_it_was_weighed() -> None:
    assert signal_weight(YOUNG) == WEAK  # a weak signal that met no second one
    assert signal_weight(BUSY) == CONTEXT
    approval = Finding(Reason.CHECKER_INJECTION, "says the payment is pre-approved", "rules", context=True)
    assert signal_weight(approval) == CONTEXT
    assert signal_weight(Finding(Reason.PAYEE_REPORTED, "2 reports", "rules")) == STRONG
    assert signal_weight(Finding(Reason.MULE_PATTERN, "next to a new account", "rules")) == WEAK


def test_the_payee_facts_the_rules_could_read() -> None:
    def record(sources: str, error: str | None = None) -> list[StepRecord]:
        return [StepRecord(step="precheck", error=error, tool_calls=[{"name": "payee_profile", "sources": sources}])]

    everything = {Reason.NEW_ACCOUNT, Reason.MULE_PATTERN, Reason.PAYEE_REPORTED}
    assert facts_read(record("chain+fastnear+vault")) == everything
    assert facts_read(record("fixture+vault")) == everything
    assert facts_read(record("chain+vault")) == {Reason.PAYEE_REPORTED}  # the transaction index timed out
    assert facts_read(record("")) == set()  # the lookup raised
    assert facts_read(record("chain+fastnear+vault", error="TimeoutError")) == set()
    assert facts_read([StepRecord(step="precheck")]) == set()


def test_a_warning_must_name_the_payee_or_a_link() -> None:
    names = P.payment_names(
        "safe-wallet-77.testnet",
        ["https://coinhaven-support.example/verify"],
        "Forwarded from CoinHaven Support: move your USDC now.",
    )
    assert names == ["safe wallet 77 testnet", "coinhaven support example"]
    assert not P.names_payment(BITPAY, BITPAY_TITLE, names)
    assert P.names_payment("https://r.example/a", "SAFE-WALLET-77.TESTNET flagged", names)
    assert P.names_payment("https://reports.example/domains/coinhaven-support.example", "Report", names)
    assert P.names_payment("https://r.example/search?q=safe-wallet-77.testnet", "Search results", names)
    assert P.names_payment("https://r.example/address/safe%2Dwallet%2D77.testnet", "Report", names)
    assert not P.names_payment("https://coinhaven-support.example/about", "About us", names)  # the host is not enough
    # The gateway gives a page without a title its address as the title: the host still does not count.
    page = "https://coinhaven-support.example/about"
    assert not P.names_payment(page, page, names)
    # A brand names the real business as much as its impostors (a dental practice warning of fake texts).
    assert not P.names_payment("https://news.example/a", "CoinHaven warns of fake support accounts", names)
    # Live: a search for fastship-agent-31.testnet returned pages about a game character called Agent 31.
    agent = P.payment_names("fastship-agent-31.testnet", [], "")
    assert not P.names_payment("https://www.valorant-spind.de/en/valorant-agent-31.html", "Valorant Agent 31", agent)


def test_a_warning_may_name_the_account_above_the_payee_or_the_links_domain() -> None:
    """Review: the holder of coinhaven.near created pay7.coinhaven.near; claim.x.com belongs to x.com."""
    assert P.payment_names("pay7.coinhaven.near", [], "") == ["pay7 coinhaven near", "coinhaven near"]
    assert P.payment_names("alice.near", [], "") == ["alice near"]
    assert P.payment_names("a1b2c3", [], "") == ["a1b2c3"]

    names = P.payment_names("claims-sam.near", ["https://claim.coinhaven-app.com/drop"], "")
    assert names == ["claims sam near", "claim coinhaven app com", "coinhaven app com"]
    scamadviser = "https://www.scamadviser.com/check-website/coinhaven-app.com"
    assert P.names_payment(scamadviser, "Reviews | scam, legit or safe check", names)

    written = P.payment_names("claims-sam.near", [], "Pay claims-sam.near, details at coinhaven-app.com/drop")
    assert written == ["claims sam near", "coinhaven app com"]  # no https://, still a link; the account is no site
    assert P.payment_names("x.near", ["https://shop.example.co.uk/pay"], "")[-1] == "example co uk"


def test_a_link_to_a_shared_site_names_only_the_posters_page() -> None:
    names = P.payment_names("xyz.near", ["https://t.me/coinhaven_help", "https://docs.google.com/forms/d/1"], "")
    assert names == ["xyz near", "t me coinhaven help"]
    assert not P.names_payment("https://news.example/t-me-scams", "Scams on t.me links", names)
    assert P.payment_names("xyz.near", ["http://127.0.0.1:8000/verify"], "") == ["xyz near"]
    assert P.registered_domain("claim.coinhaven-app.com") == "coinhaven-app.com"
    assert P.registered_domain("localhost") == ""


def test_a_warning_may_name_a_page_on_a_shared_site() -> None:
    """Red-team review (S3a, S2c): reports naming the exact t.me handle or github.io site of the message."""
    text = "Support is on Telegram: t.me/BinanceHelpDesk_247 (or join t.me/joinchat/AbCdEf)"
    names = P.payment_names("unlock-desk-24.near", [], text)
    assert names == ["unlock desk 24 near", "t me binancehelpdesk 247", "t me abcdef"]
    assert P.names_payment("https://r.example/1", "t.me/BinanceHelpDesk_247 - fake Binance support", names)
    assert not P.names_payment("https://r.example/2", "Fake Binance support on Telegram", names)

    names = P.payment_names("ledger-sync-fee.near", ["https://ledger-live-sync.github.io/confirm"], "")
    assert names == ["ledger sync fee near", "ledger live sync github io"]
    assert P.names_payment("https://r.example/3", "ledger-live-sync.github.io - phishing site reported", names)
    assert not P.names_payment("https://r.example/4", "Phishing pages hosted on github.io", names)
    assert P.registered_domain("ledger-live-sync.github.io") == "ledger-live-sync.github.io"
    assert P.registered_domain("coinhaven.xn--p1ai") == "coinhaven.xn--p1ai"  # .рф


def test_file_names_and_short_payee_names_name_nothing() -> None:
    """Honest review (X3): a plumber's invoice.pdf made a consumer article on fake PDF invoices name the payment."""
    names = P.payment_names("kowalski-plumbing.testnet", [], "as on the attached invoice.pdf and photo.JPG")
    assert names == ["kowalski plumbing testnet"]
    assert not P.names_payment("https://consumer.example/fake-invoice-pdf", "Fake invoice PDF emails", names)
    assert P.payment_names("a.near", [], "") == []  # a near-miss is prose
    # The title and the address are read apart: a title ending in Alice and a path starting near-wallet.
    alice = P.payment_names("alice.near", [], "")
    assert not P.names_payment("https://blog.example/near-wallet-scams", "Reported by Alice", alice)
    assert not P.names_payment("https://x.example/a", "Alice https://blog.example/near-wallet-scams", alice)


def test_a_link_python_refuses_to_split_names_nothing() -> None:
    """Code review: a full-width ？ or ＃ in a link's host made urlsplit raise and the whole check fail closed."""
    for link in ("https://wallet-help\uff03.com/restore", "https://shop.example.com\uff1f\u623f\u79df950"):
        assert P.payment_names("ravi.near", [link], "") == ["ravi near"]
        assert not P.names_payment(link, f"Report {link}", ["ravi near"])
        assert P._url_key(link) == link.lower()
    assert P.names_payment("https://r.example/a", "ravi.near reported https://x\uff03y.com/z", ["ravi near"])
    # Written in a message, the host before the full-width mark is still a site the message names.
    assert P.payment_names("ravi.near", [], "\u4f60\u770b\u8fc7https://shop.example.com\uff1f") == [
        "ravi near", "shop example com", "example com"
    ]


def test_a_models_context_finding_does_not_lengthen_the_hold() -> None:
    reason = Finding(Reason.SECRET_REQUEST, "asks for a recovery phrase", "web_fetch", INVESTIGATOR_MODEL)
    assert P.hold_for([reason]) == P.MAX_HOLD_MS
    urgency = Finding(Reason.URGENCY, "a deadline", "triage", TRIAGE_MODEL)
    unagreed = [
        Finding(Reason.IMPERSONATION, "claims to be a business", "triage", TRIAGE_MODEL, context=True),
        Finding(Reason.MULE_PATTERN, "many senders", "reviewer", REVIEWER_MODEL, context=True),
    ]
    assert P.hold_for([urgency, *unagreed]) == P.DAY_MS
    assert P.hold_for([urgency, YOUNG, BUSY]) == P.MAX_HOLD_MS  # what the rules pass on still counts
