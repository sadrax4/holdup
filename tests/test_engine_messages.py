"""Message texts: brake format, reason tags, buttons and friendly errors."""

from __future__ import annotations

from holdup import messages
from holdup.chain import errors as E
from holdup.config import offline_settings
from holdup.models import NEAR, Finding, Hold, HoldStatus, Reason

NOW = 1_793_610_000_000
HOUR = 3_600_000
SETTINGS = offline_settings()
HOLD = Hold(4, "u1.holdup.testnet", "safe-wallet-77.testnet", SETTINGS.husd_token or "", 50_000_000,
            NOW, NOW + 20_000, HoldStatus.HELD)


def test_reason_tags_name_what_found_each_reason() -> None:
    tag = messages.finding_tag
    assert tag(Finding(Reason.NEW_ACCOUNT, "", "rules")) == "[rules]"
    assert tag(Finding(Reason.URGENCY, "", "triage", "openai/gpt-oss-20b")) == "[triage gpt-oss-20b]"
    assert tag(Finding(Reason.MULE_PATTERN, "", "mcp")) == "[MCP near-intel]"
    assert tag(Finding(Reason.PHISHING_LINK, "", "web_fetch")) == "[web fetch]"
    assert tag(Finding(Reason.PUBLIC_WARNING, "", "web_search", citations=["a", "b"])) == "[web search, 2 sources]"
    assert tag(Finding(Reason.PUBLIC_WARNING, "", "web_search", citations=["a"])) == "[web search, 1 source]"
    assert tag(Finding(Reason.CHECKER_INJECTION, "", "guardrail")) == "[guardrail]"
    assert tag(Finding(Reason.AMOUNT_MISMATCH, "", "code_execution")) == "[code execution]"
    assert tag(Finding(Reason.SECOND_OPINION, "", "reviewer", "deepseek/deepseek-v4.1-flash-1017")) == (
        "[reviewer deepseek-v4.1-flash-1017]"
    )
    assert tag(Finding(Reason.AI_UNAVAILABLE, "", "fail_closed")) == "[fail-closed]"


def test_brake_message_format() -> None:
    findings = [
        Finding(Reason.NEW_ACCOUNT, "created 2 h ago", "rules"),
        Finding(Reason.NEW_ACCOUNT, "", "mcp"),
        Finding(Reason.IMPERSONATION, "claims to be CoinHaven Support " + "x" * 200, "triage", "openai/gpt-oss-20b"),
    ]
    text = messages.brake(SETTINGS, HOLD, findings, NOW + 24 * HOUR, now_ms=NOW,
                          tx_text="tx abc (simulated)", page="https://h.example/h/4")
    lines = text.splitlines()
    assert lines[0] == "Brakes on - held until 3 Nov 09:00 UTC"
    assert lines[1] == "50 HUSD to safe-wallet-77.testnet:"
    assert lines[2] == "- Payee account is brand new: created 2 h ago [rules] [MCP near-intel]"
    assert lines[3].startswith("- Message impersonates support") and lines[3].endswith("... [triage gpt-oss-20b]")
    assert lines[-2:] == ["Details: https://h.example/h/4", "tx abc (simulated)"]
    provisional = messages.provisional_brake(SETTINGS, HOLD, findings[:1], NOW + HOUR, now_ms=NOW)
    assert provisional.startswith("Brakes on - held until 10:00 UTC\n") and "Quick checks found:" in provisional


def test_times_and_amounts_read_naturally() -> None:
    assert messages.fmt_utc(NOW + 20_000, NOW) == "09:00:20 UTC"
    assert messages.fmt_utc(NOW + 2 * HOUR, NOW) == "11:00 UTC"
    assert messages.fmt_utc(NOW + 30 * HOUR, NOW) == "3 Nov 15:00 UTC"
    assert [messages.fmt_duration(ms) for ms in (20_000, 600_000, 90 * 60_000, 72 * HOUR)] == [
        "20 s", "10 min", "1 h 30 min", "3 days"]
    assert messages.fmt_amount(SETTINGS, NEAR, 12_500_000_000_000_000_000_000_000) == "12.5 NEAR"
    assert messages.fmt_amount(SETTINGS, "unknown.testnet", 5) == "5 unknown.testnet"


def test_buttons_use_the_shared_callback_format() -> None:
    assert messages.sender_buttons(4) == [[("Cancel & refund", "cancel:4"), ("Why?", "why:4")]]
    assert messages.guardian_buttons(4, "Priya") == [[("Cancel for Priya", "gcancel:4"), ("Why?", "why:4")]]


def test_failures_are_described_honestly() -> None:
    text = messages.fail_closed(SETTINGS, HOLD, "gateway timeout", NOW + HOUR, now_ms=NOW)
    assert "could not run (gateway timeout)" in text and "brake stays on" in text
    final = messages.fail_closed(SETTINGS, HOLD, "gateway timeout", NOW + 6 * HOUR, now_ms=NOW, final=True)
    assert "will release then unless you cancel" in final
    assert "budget is used up" in messages.fail_closed(SETTINGS, HOLD, "", NOW, now_ms=NOW, budget=True)


def test_no_message_labels_an_account_as_a_scam() -> None:
    findings = [Finding(Reason.PAYEE_REPORTED, "2 reports in 30 days", "rules")]
    texts = [
        messages.held(SETTINGS, HOLD, now_ms=NOW),
        messages.brake(SETTINGS, HOLD, findings, NOW + HOUR, now_ms=NOW),
        messages.guardian_alert(SETTINGS, HOLD, findings, NOW + HOUR, "Priya", now_ms=NOW),
        messages.cancelled(SETTINGS, HOLD),
        messages.released(SETTINGS, HOLD),
        messages.checks_clear(SETTINGS, HOLD, now_ms=NOW),
    ]
    assert all("scam" not in text.lower() for text in texts)
    assert "Other senders cancelled payments to this payee" in texts[1]


def test_error_texts() -> None:
    assert messages.error_text(E.E_INSUFFICIENT_BALANCE).startswith("Not enough balance")
    assert messages.refusal(E.UNKNOWN_PAYEE, payee="x.testnet") == "x.testnet does not exist on NEAR. Check the name; nothing was sent."
    assert messages.error_text("E_SOMETHING_NEW", "detail") == "That did not work (E_SOMETHING_NEW: detail)."
