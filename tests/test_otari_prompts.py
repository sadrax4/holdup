"""Prompt files and output contracts: hygiene rules that keep requests through the gateway firewall."""

from __future__ import annotations

import hashlib
from importlib import resources
from typing import Any

import pytest
from pydantic import BaseModel

from holdup.guard import rules_verdict
from holdup.risk import prompts
from holdup.risk.pipeline import DEFAULT_DETAIL, MCP_PURPOSE
from holdup.risk.prompts import contracts as C

STEP_CONTRACTS: dict[str, type[BaseModel]] = {
    "triage": C.TriageResult,
    "vision": C.VisionResult,
    "investigator": C.InvestigatorResult,
    "reviewer": C.ReviewerResult,
    "injection_policy": C.PolicyVerdict,
}
STEP_PROMPTS = ("triage", "vision", "investigator", "reviewer", "gate")
# Phrases a guardrail or a firewall could read as an attack if they sat in our own instructions.
TRIGGERS = (
    "ignore previous",
    "ignore all",
    "pre-approved",
    "preapproved",
    "mark it safe",
    "note to any ai",
    "system prompt",
    "jailbreak",
    " or 1=1",
)


@pytest.mark.parametrize("name", prompts.PROMPT_NAMES)
def test_prompts_are_plain_ascii_without_quotes_or_backticks(name: str) -> None:
    text = prompts.load(name).text
    assert text.strip(), name
    assert all(ord(ch) < 128 for ch in text), f"{name}: non-ASCII characters"
    for ch in (chr(0x60), "'", '"'):
        assert ch not in text, f"{name} contains {ch!r}; the gateway firewall rejects some quoted text"
    lowered = text.lower()
    assert not [t for t in TRIGGERS if t in lowered], name


@pytest.mark.parametrize("name", STEP_PROMPTS)
def test_our_own_instructions_pass_the_injection_guardrail(name: str) -> None:
    # Otari may hand the guardrail the whole input, system prompt included.
    assert rules_verdict("checker-injection", prompts.load(name).text).valid is True


def test_tool_descriptions_sent_to_the_gateway_are_clean() -> None:
    for text in (MCP_PURPOSE, *DEFAULT_DETAIL.values()):
        assert not set(text) & {chr(0x60), "'", '"'} and all(ord(ch) < 128 for ch in text)
    assert rules_verdict("checker-injection", MCP_PURPOSE).valid is True


@pytest.mark.parametrize(("name", "contract"), STEP_CONTRACTS.items())
def test_every_contract_field_is_explained_in_its_prompt(name: str, contract: type[BaseModel]) -> None:
    text = prompts.load(name).text
    missing = [field for field in contract.model_fields if field not in text]
    assert not missing, f"{name} prompt does not mention {missing}"


def test_finding_reasons_listed_in_prompts_match_the_contract() -> None:
    for name in ("investigator", "reviewer"):
        text = prompts.load(name).text
        assert all(reason in text for reason in C.FINDING_REASONS), name
    for tool in C.TOOLS:
        assert tool in prompts.load("investigator").text


def test_versions_are_the_sha256_of_the_file_bytes() -> None:
    for name in prompts.PROMPT_NAMES:
        raw = resources.files("holdup.risk.prompts").joinpath(f"{name}.txt").read_bytes()
        prompt = prompts.load(name)
        assert prompt.sha256 == hashlib.sha256(raw).hexdigest()
        assert prompts.versions()[name] == prompt.version == prompt.sha256[:16]
    with pytest.raises(KeyError):
        prompts.load("unknown")


def _objects(schema: Any) -> list[dict[str, Any]]:
    found = []
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            found.append(schema)
        for value in schema.values():
            found += _objects(value)
    elif isinstance(schema, list):
        for value in schema:
            found += _objects(value)
    return found


@pytest.mark.parametrize(
    "contract", [C.TriageResult, C.VisionResult, C.ReviewerResult, C.InvestigatorResult, C.PolicyVerdict]
)
def test_strict_schemas_close_every_object(contract: type[BaseModel]) -> None:
    schema = C.strict_schema(contract)
    assert "$defs" not in schema and "$ref" not in str(schema)
    for obj in _objects(schema):
        assert obj["additionalProperties"] is False
        assert obj["required"] == list(obj["properties"])
    assert C.response_format(contract, "x")["json_schema"]["strict"] is True


def test_contracts_normalise_untidy_answers() -> None:
    triage = C.TriageResult.model_validate(
        {
            "categories": ["Invoice", "made-up", "invoice"],
            "urgency": "true",
            "impersonation": False,
            "secret_request": False,
            "details_changed": False,
            "injection_suspected": False,
            "impossible_returns": False,
            "money_claims": [{"label": "total  due", "amount": "1,250.00", "currency": "USDC", "extra": 1}],
            "risk": "HIGH",
            "summary": "x" * 500,
            "unexpected": True,
        }
    )
    assert triage.categories == ["invoice"] and triage.urgency is True and triage.risk == "high"
    assert triage.money_claims[0].label == "total due" and len(triage.summary) == 200

    result = C.InvestigatorResult.model_validate(
        {
            "findings": [
                {"reason": "new_account", "tool": "near-intel.payee_profile"},
                {"reason": "NOT_A_REASON", "tool": "web_search"},
                {"reason": "PUBLIC_WARNING", "tool": "Otari Web Search", "citations": ["https://a.example", 7]},
                {"reason": "PHISHING_LINK", "tool": "fetch_page"},
                {"reason": "AMOUNT_MISMATCH", "tool": "python"},
                {"reason": "URGENCY", "tool": "a hunch"},
            ]
        }
    )
    assert [(f.reason, f.tool) for f in result.findings] == [
        ("NEW_ACCOUNT", "near-intel"),
        ("PUBLIC_WARNING", "web_search"),
        ("PHISHING_LINK", "near-intel"),
        ("AMOUNT_MISMATCH", "code_execution"),
        ("URGENCY", "message"),
    ]
    assert result.findings[1].citations == ["https://a.example", "7"]
    assert result.brake is False and result.amount_check is None


def test_a_missing_required_field_fails_the_contract() -> None:
    with pytest.raises(ValueError):
        C.TriageResult.model_validate({"categories": [], "urgency": False})
    with pytest.raises(ValueError):
        C.ReviewerResult.model_validate({"reasons": []})
