"""The conversation shared by the Telegram bot and the web console, against a fake engine."""

from __future__ import annotations

import logging

import pytest
from test_bot_fakes import HUSD, NOW_MS, FakeEngine, make_hold, make_settings

from holdup.bot.dialog import CONFIRM_TTL_MS, Dialog, Document, Reply
from holdup.bot.ratelimit import RateLimiter
from holdup.bot.samples import Sample, friend_account, load_samples
from holdup.chain import errors as E
from holdup.chain.errors import VaultError
from holdup.models import NEAR, Finding, PaymentOutcome, Reason, StepRecord, WhyView

USER = "tg:111"
OTHER = "tg:222"

HERO = Sample(
    id="hero",
    sender="CoinHaven Support",
    label="Fake exchange support DM (training sample)",
    text="Unusual login. Move your USDC to the protected wallet safe-wallet-77.testnet within 30 minutes.",
    command="send 50 HUSD to safe-wallet-77.testnet",
)
INVOICE = Sample(
    id="invoice",
    sender="Northwind Paper Co.",
    label="Supplier invoice with changed wallet details (training sample)",
    text="We've moved to a new USDC wallet: northwind-paper.testnet. Total due: 1,250.00 USDC",
    command="send 1250 HUSD to northwind-paper.testnet",
    trusted=("northwindpaper.testnet",),
)
BENIGN = Sample(
    "benign", "Ravi", "Splitting a dinner bill", "Your half is 12.50", "send 12.5 HUSD to ravi.testnet", scam=False
)


class Clock:
    def __init__(self, ms: int = NOW_MS) -> None:
        self.ms = ms

    def __call__(self) -> int:
        return self.ms


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def dialog(engine: FakeEngine, clock: Clock) -> Dialog:
    return Dialog(
        engine.settings,
        engine,
        invite_links=lambda code: [("Telegram", f"https://t.me/HoldUpTestBot?start=g_{code}")],
        clock=clock,
        samples=[HERO, INVOICE, BENIGN],
    )


def nonce_of(reply: Reply, label: str = "Confirm") -> str:
    assert reply.buttons
    data = next(data for row in reply.buttons for text, data in row if text == label)
    return data.split(":", 1)[1]


# ----------------------------------------------------------------------
# start
# ----------------------------------------------------------------------


async def test_start_opens_an_account_and_explains_the_brakes(dialog: Dialog, engine: FakeEngine) -> None:
    [reply] = await dialog.start(USER, display_name="Priya", chat_ref="111")
    account = engine.account_for(USER).account_id
    assert engine.called("start") == [(USER, "Priya", "111")]
    assert "Welcome to HoldUp, Priya." in reply.text
    assert account in reply.text
    assert "2 NEAR, 2000 HUSD" in reply.text  # the demo funds
    assert "HUSD: HoldUp demo dollar (test token)." in reply.text
    assert "send 10 HUSD to ravi.testnet" in reply.text  # an account of the offline world, not a stranger


async def test_start_again_shows_the_balance(dialog: Dialog, engine: FakeEngine) -> None:
    await dialog.start(USER, display_name="Priya")
    engine.balances = {NEAR: 3 * 10**24, HUSD: 1_950 * 10**6}
    [reply] = await dialog.start(USER, display_name="Priya")
    assert "Welcome back, Priya." in reply.text
    assert "3 NEAR, 1950 HUSD" in reply.text


async def test_start_link_makes_you_a_guardian(dialog: Dialog, engine: FakeEngine) -> None:
    replies = await dialog.start(OTHER, display_name="Mum", payload=f"g_{engine.invite_code}")
    assert engine.called("accept_guardian") == [(OTHER, engine.invite_code)]
    assert len(replies) == 1  # the engine tells both people what changed


async def test_a_bad_guardian_link_is_explained(dialog: Dialog, engine: FakeEngine) -> None:
    replies = await dialog.start(OTHER, payload="g_wrong")
    assert len(replies) == 2
    assert "guardian link is invalid" in replies[1].text


# ----------------------------------------------------------------------
# paying: typed, confirmed, then sent
# ----------------------------------------------------------------------


async def test_a_typed_payment_waits_for_confirmation(dialog: Dialog, engine: FakeEngine) -> None:
    [prompt] = await dialog.text(USER, "send 12.5 HUSD to ravi.testnet")
    assert "Send 12.5 HUSD to ravi.testnet?" in prompt.text
    assert [label for row in prompt.buttons or [] for label, _ in row] == ["Confirm", "Don't send"]
    assert engine.called("pay") == []

    assert await dialog.button(USER, f"confirm:{nonce_of(prompt)}") == []
    [(user_key, payee, asset_id, amount, ctx)] = engine.called("pay")
    assert (user_key, payee, asset_id, amount, ctx) == (USER, "ravi.testnet", HUSD, 12_500_000, None)


async def test_a_confirmation_works_once(dialog: Dialog, engine: FakeEngine) -> None:
    [prompt] = await dialog.text(USER, "send 1 NEAR to ravi.testnet")
    nonce = nonce_of(prompt)
    await dialog.button(USER, f"confirm:{nonce}")
    [again] = await dialog.button(USER, f"confirm:{nonce}")
    assert "expired or was already used" in again.text
    assert len(engine.called("pay")) == 1
    assert engine.called("pay")[0][3] == 10**24


async def test_only_the_person_who_typed_it_can_confirm(dialog: Dialog, engine: FakeEngine) -> None:
    [prompt] = await dialog.text(USER, "send 1 NEAR to ravi.testnet")
    [reply] = await dialog.button(OTHER, f"confirm:{nonce_of(prompt)}")
    assert "expired" in reply.text
    assert engine.called("pay") == []
    await dialog.button(USER, f"confirm:{nonce_of(prompt)}")  # still valid for its owner
    assert len(engine.called("pay")) == 1


async def test_confirmations_expire(dialog: Dialog, engine: FakeEngine, clock: Clock) -> None:
    [prompt] = await dialog.text(USER, "send 1 NEAR to ravi.testnet")
    clock.ms += CONFIRM_TTL_MS + 1
    [reply] = await dialog.button(USER, f"confirm:{nonce_of(prompt)}")
    assert "expired" in reply.text
    assert engine.called("pay") == []


async def test_dont_send_drops_the_payment(dialog: Dialog, engine: FakeEngine) -> None:
    [prompt] = await dialog.text(USER, "send 1 NEAR to ravi.testnet")
    [reply] = await dialog.button(USER, f"drop:{nonce_of(prompt, "Don't send")}")
    assert reply.text == "OK, nothing was sent."
    [late] = await dialog.button(USER, f"confirm:{nonce_of(prompt)}")
    assert "expired" in late.text
    assert engine.called("pay") == []


async def test_usdc_falls_back_to_the_demo_token_and_says_so(dialog: Dialog, engine: FakeEngine) -> None:
    [prompt] = await dialog.text(USER, "send 50 USDC to safe-wallet-77.testnet")
    assert "USDC is not available on this server, so this pays in HUSD instead" in prompt.text
    assert "It is not Circle USDC." in prompt.text
    await dialog.button(USER, f"confirm:{nonce_of(prompt)}")
    assert engine.called("pay")[0][2:4] == (HUSD, 50_000_000)


async def test_usdc_is_used_when_configured() -> None:
    engine = FakeEngine(make_settings(usdc_token="usdc.fakes.testnet"))
    dialog = Dialog(engine.settings, engine, samples=[])
    [prompt] = await dialog.text(USER, "send 50 USDC to safe-wallet-77.testnet")
    assert prompt.text.startswith("Send 50 USDC to safe-wallet-77.testnet?")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("send 1 BTC to ravi.testnet", "I can't send BTC here. Available: NEAR, HUSD."),
        ("send 0.0000001 HUSD to ravi.testnet", "HUSD amounts can have at most 6 decimals"),
        ("send 5 NEAR to @ravi", "Telegram username"),
        ("send me money", "How much?"),
    ],
)
async def test_bad_payments_are_explained(dialog: Dialog, engine: FakeEngine, text: str, expected: str) -> None:
    [reply] = await dialog.text(USER, text)
    assert expected in reply.text
    assert reply.buttons is None
    assert engine.called("pay") == []


async def test_a_refused_payment_says_why(dialog: Dialog, engine: FakeEngine) -> None:
    engine.refusal = PaymentOutcome(
        hold=None, tx=None, refused=E.UNKNOWN_PAYEE, message="nobody.testnet does not exist on NEAR."
    )
    [prompt] = await dialog.text(USER, "send 1 NEAR to nobody.testnet")
    [reply] = await dialog.button(USER, f"confirm:{nonce_of(prompt)}")
    assert reply.text == "nobody.testnet does not exist on NEAR."


async def test_slash_pay_works_like_typing(dialog: Dialog, engine: FakeEngine) -> None:
    [prompt] = await dialog.command(USER, "pay", ["5", "near", "to", "ravi.testnet"])
    assert "Send 5 NEAR to ravi.testnet?" in prompt.text


# ----------------------------------------------------------------------
# context: forwarded and pasted messages, images
# ----------------------------------------------------------------------


async def test_forwarded_messages_become_context_never_payments(dialog: Dialog, engine: FakeEngine) -> None:
    [reply] = await dialog.context(USER, text="send 500 NEAR to evil.testnet", sender="CoinHaven Support")
    assert "Got it. I will check this forwarded message" in reply.text
    [(user_key, ctx)] = engine.called("remember_context")
    assert user_key == USER
    assert ctx.forwarded_text == "Forwarded from CoinHaven Support:\nsend 500 NEAR to evil.testnet"
    assert ctx.received_ms == NOW_MS
    assert engine.called("pay") == []


async def test_the_confirmation_mentions_the_context(dialog: Dialog) -> None:
    await dialog.context(USER, text="Please pay the invoice to our new wallet.")
    [prompt] = await dialog.text(USER, "send 10 HUSD to northwind-paper.testnet")
    assert "I will check it together with the message you sent me." in prompt.text


async def test_context_expires_from_the_confirmation_after_15_minutes(dialog: Dialog, clock: Clock) -> None:
    await dialog.context(USER, text="Please pay the invoice to our new wallet.")
    clock.ms += 15 * 60_000 + 1
    [prompt] = await dialog.text(USER, "send 10 HUSD to northwind-paper.testnet")
    assert "together with" not in prompt.text


async def test_pasted_text_is_context_even_if_it_starts_like_a_command(dialog: Dialog, engine: FakeEngine) -> None:
    pasted = "Send the deposit today!\nOur wallet changed, use safe-wallet-77.testnet.\nThanks, Support"
    [reply] = await dialog.text(USER, pasted)
    assert "Got it. I will check this message" in reply.text
    assert engine.called("remember_context")[0][1].forwarded_text == pasted


async def test_short_chatter_gets_a_hint(dialog: Dialog, engine: FakeEngine) -> None:
    [reply] = await dialog.text(USER, "hi")
    assert "I send payments with brakes" in reply.text
    assert engine.called("remember_context") == []


async def test_an_image_is_context(dialog: Dialog, engine: FakeEngine) -> None:
    [reply] = await dialog.context(USER, image=b"\xff\xd8jpeg", image_mime="image/jpeg", forwarded=False)
    assert "this image" in reply.text
    ctx = engine.called("remember_context")[0][1]
    assert (ctx.image_bytes, ctx.image_mime, ctx.forwarded_text) == (b"\xff\xd8jpeg", "image/jpeg", None)


# ----------------------------------------------------------------------
# buttons on engine notifications
# ----------------------------------------------------------------------


async def test_cancel_and_guardian_cancel_buttons(dialog: Dialog, engine: FakeEngine) -> None:
    assert await dialog.button(USER, "cancel:7") == []
    assert await dialog.button(OTHER, "gcancel:7") == []
    assert engine.called("cancel") == [(USER, 7)]
    assert engine.called("guardian_cancel") == [(OTHER, 7)]


async def test_the_guardian_keeps_their_role_with_a_button(dialog: Dialog, engine: FakeEngine) -> None:
    assert await dialog.button(OTHER, "gveto:uabcdefghij.holdup.testnet") == []
    assert engine.called("veto_guardian_change") == [(OTHER, "uabcdefghij.holdup.testnet")]
    [reply] = await dialog.button(OTHER, "gveto:Not An Account")
    assert reply.text == "That button is no longer valid."


async def test_vault_errors_become_plain_sentences(dialog: Dialog, engine: FakeEngine) -> None:
    engine.fail["cancel"] = VaultError(E.E_NOT_SENDER, "caller is not the sender")
    [reply] = await dialog.button(OTHER, "cancel:7")
    assert reply.text == "Only the person who sent this payment can cancel it."


async def test_unexpected_errors_do_not_leak(
    dialog: Dialog, engine: FakeEngine, caplog: pytest.LogCaptureFixture
) -> None:
    engine.fail["holds"] = RuntimeError("database is on fire")
    with caplog.at_level(logging.ERROR):
        [reply] = await dialog.command(USER, "holds")
    assert "Something went wrong on our side" in reply.text
    assert "fire" not in reply.text
    assert "fire" in caplog.text


async def test_why_explains_a_brake(dialog: Dialog, engine: FakeEngine) -> None:
    hold = make_hold(7, sender=engine.account_for(USER).account_id, flagged=True, extensions=1, reason_bits=1)
    engine.why_views[7] = WhyView(
        hold=hold,
        evidence_json="{}",
        evidence_hash="ab" * 32,
        findings=[Finding(Reason.NEW_ACCOUNT, "created 2 h ago", "rules")],
        steps=[StepRecord("triage", "openai/gpt-oss-20b", "groq", False, 1, "req-1", 0.0004, 800)],
    )
    [reply] = await dialog.button(USER, "why:7")
    assert "Payment #7: 50 HUSD to safe-wallet-77.testnet" in reply.text
    assert "Brakes on, held until" in reply.text
    assert "- Payee account is brand new: created 2 h ago [rules]" in reply.text
    assert "Checked by: triage gpt-oss-20b ($0.0004)" in reply.text
    assert f"Evidence sha256 (on-chain): {'ab' * 32}" in reply.text
    assert "http://testserver/h/7" in reply.text and "http://testserver/why/7" in reply.text
    assert reply.buttons == [[("Cancel & refund", "cancel:7")]]


async def test_why_for_someone_elses_hold_has_no_cancel_button(dialog: Dialog, engine: FakeEngine) -> None:
    engine.why_views[7] = WhyView(hold=make_hold(7), evidence_json=None, evidence_hash=None)
    [reply] = await dialog.button(OTHER, "why:7")
    assert "No warning signs so far" in reply.text
    assert reply.buttons is None


async def test_unknown_buttons_are_refused(dialog: Dialog, engine: FakeEngine) -> None:
    for data in ("cancel:abc", "launch:7", "why:"):
        [reply] = await dialog.button(USER, data)
        assert reply.text == "That button is no longer valid."
    assert engine.called("cancel") == []
    [missing] = await dialog.button(USER, "why:99")
    assert missing.text == "There is no payment #99."


# ----------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------


async def test_balance(dialog: Dialog, engine: FakeEngine) -> None:
    [reply] = await dialog.command(USER, "balance")
    assert reply.text.splitlines()[:3] == [
        f"Balance of {engine.account_for(USER).account_id}:",
        "2 NEAR",
        "2000 HUSD",
    ]


async def test_holds_offers_buttons_for_held_payments_only(dialog: Dialog, engine: FakeEngine) -> None:
    from holdup.models import HoldStatus

    engine.hold_list = [
        make_hold(9, flagged=True, extensions=1, reason_bits=int(Reason.NEW_ACCOUNT)),
        make_hold(8, status=HoldStatus.RELEASED, payee="ravi.testnet", settled_ms=NOW_MS),
    ]
    [reply] = await dialog.command(USER, "holds")
    assert "#9 50 HUSD to safe-wallet-77.testnet - brakes on, held until" in reply.text
    assert "(new payee account)" in reply.text  # the friendly label, not the enum name
    assert "#8 50 HUSD to ravi.testnet - released to the payee" in reply.text
    assert reply.buttons == [[("Cancel #9", "cancel:9"), ("Why? #9", "why:9")]]


async def test_cancel_trust_and_untrust_commands(dialog: Dialog, engine: FakeEngine) -> None:
    assert await dialog.command(USER, "cancel", ["#12"]) == []
    assert await dialog.command(USER, "trust", ["NorthwindPaper.testnet"]) == []
    assert await dialog.command(USER, "untrust", ["northwindpaper.testnet"]) == []
    assert engine.called("cancel") == [(USER, 12)]
    assert engine.called("trust") == [(USER, "northwindpaper.testnet")]
    assert engine.called("untrust") == [(USER, "northwindpaper.testnet")]


@pytest.mark.parametrize(
    ("command", "args", "expected"),
    [
        ("cancel", [], "Which payment? Send /cancel <id>"),
        ("why", ["x"], "Which payment? Send /why <id>"),
        ("trust", [], "Which account? Send /trust <account>"),
        ("trust", ["@bob"], "Telegram username"),
        ("frobnicate", [], "I don't know /frobnicate."),
    ],
)
async def test_command_mistakes(dialog: Dialog, command: str, args: list[str], expected: str) -> None:
    [reply] = await dialog.command(USER, command, args)
    assert expected in reply.text


async def test_guardian_invite_links(dialog: Dialog, engine: FakeEngine) -> None:
    [reply] = await dialog.command(USER, "guardian")
    assert f"Telegram: https://t.me/HoldUpTestBot?start=g_{engine.invite_code}" in reply.text
    assert "expires in 24 hours" in reply.text


async def test_export_sends_a_csv_file(dialog: Dialog, engine: FakeEngine) -> None:
    [reply] = await dialog.command(USER, "export")
    account = engine.account_for(USER).account_id
    assert reply.document == Document(f"holdup-{account}.csv", engine.csv.encode())


async def test_export_can_be_a_signed_link(engine: FakeEngine, clock: Clock) -> None:
    dialog = Dialog(
        engine.settings,
        engine,
        export_link=lambda account: (f"http://testserver/export/{account.account_id}.csv", NOW_MS + 15 * 60_000),
        clock=clock,
        samples=[],
    )
    [reply] = await dialog.command(USER, "export")
    assert reply.document is None
    assert "(the link works until 09:15:00 UTC)" in reply.text
    assert "http://testserver/export/" in reply.text
    assert engine.called("export_csv") == []


async def test_prove_stats_and_budget(dialog: Dialog, engine: FakeEngine) -> None:
    [prove] = await dialog.command(USER, "prove")
    assert "vault.holdup.testnet, which has no access keys" in prove.text
    assert "http://testserver/prove" in prove.text
    [stats] = await dialog.command(USER, "stats")
    assert "12 payments created" in stats.text
    assert "3 cancelled after a warning" in stats.text
    assert "AI: 9 checks, 31 model steps, 2 fallbacks, $0.0213" in stats.text
    [budget] = await dialog.command(USER, "budget")
    assert "replaying recorded Otari responses" in budget.text
    assert "$0.0213 over 9 checks" in budget.text
    assert "fail-closed" in budget.text
    # Live: it claimed "a hard ceiling", which nothing on the server could show or check.
    assert "hard ceiling" not in budget.text and "spend limit on the AI key" in budget.text


async def test_help_lists_every_command(dialog: Dialog) -> None:
    [reply] = await dialog.command(USER, "help")
    for name in dialog.command_names:
        if name not in ("start", "help", "pay", "send"):
            assert f"/{name}" in reply.text


async def test_demo_sends_labelled_training_samples(dialog: Dialog, engine: FakeEngine) -> None:
    replies = await dialog.command(USER, "demo")
    texts = [r.text for r in replies]
    assert "training samples" in texts[0]
    assert texts[1].startswith("Training sample 1: Fake exchange support DM")
    assert "send 50 HUSD to safe-wallet-77.testnet" in texts[1]
    assert replies[2].kind == "sample" and replies[2].text == HERO.text
    assert replies[2].buttons == [[("Check it with my next payment", "sample:hero")]]
    assert "/trust northwindpaper.testnet" in texts[3]
    assert texts[-1] == "For comparison, an ordinary payment to a friend: send 12.5 HUSD to ravi.testnet"


async def test_sample_button_loads_the_sample_as_forwarded_context(dialog: Dialog, engine: FakeEngine) -> None:
    [reply] = await dialog.button(USER, "sample:hero")
    assert reply.text.endswith("Now type the payment it asks for:\nsend 50 HUSD to safe-wallet-77.testnet")
    ctx = engine.called("remember_context")[0][1]
    assert ctx.forwarded_text == f"Forwarded from CoinHaven Support:\n{HERO.text}"


# ----------------------------------------------------------------------
# accounts and limits
# ----------------------------------------------------------------------


async def test_the_first_action_opens_the_account_once(dialog: Dialog, engine: FakeEngine) -> None:
    await dialog.command(USER, "balance", display_name="Priya", chat_ref="111")
    await dialog.text(USER, "hi")
    assert engine.called("start") == [(USER, "Priya", "111")]


async def test_rate_limit_warns_once_then_stays_quiet(engine: FakeEngine) -> None:
    now = [0.0]
    dialog = Dialog(engine.settings, engine, limiter=RateLimiter(3, 60.0, clock=lambda: now[0]), samples=[])
    for _ in range(3):
        await dialog.command(USER, "balance")
    [warning] = await dialog.command(USER, "balance")
    assert warning.text == "Slow down a little: at most 3 actions a minute."
    assert await dialog.command(USER, "balance") == []
    assert len(engine.called("balance")) == 3
    assert await dialog.command(OTHER, "balance") != []  # limits are per person
    now[0] += 61
    [reply] = await dialog.command(USER, "balance")
    assert reply.text.startswith("Balance of")


async def test_on_a_network_each_person_pays_a_practice_friend_of_their_own() -> None:
    """People trying the dinner sample must not all pay one account: its senders and reports would add up."""
    engine = FakeEngine(make_settings(network="testnet", root_account="holdup-abc123.testnet"))
    dialog = Dialog(engine.settings, engine, samples=load_samples(engine.settings))
    friend = friend_account(engine.settings, USER)
    assert friend.startswith("ravi-") and friend.endswith(".holdup-abc123.testnet")
    [welcome] = await dialog.start(USER, display_name="Priya")
    assert f"send 10 HUSD to {friend}" in welcome.text
    demo = await dialog.command(USER, "demo")
    assert demo[-1].text == f"For comparison, an ordinary payment to a friend: send 12.5 HUSD to {friend}"
    [saved] = await dialog.button(USER, "sample:benign")
    assert saved.text.endswith(f"send 12.5 HUSD to {friend}")
    [offer] = await dialog.button(USER, "pay:benign")  # the console's chip for the same payment
    assert friend in offer.text and offer.buttons and offer.buttons[0][0][0] == "Confirm"
    assert (await dialog.button(USER, "pay:nothing"))[0].text == "That practice payment is not available."
