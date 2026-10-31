"""Deterministic payment checks: no model involved, done in milliseconds.

They give the engine its instant provisional brake and keep the brake working
when the AI step is off or failing. The AI step reads messages far better;
these rules only catch the obvious. Finding details are templated: they name
what was found and never quote the forwarded message.

A rules brake costs the sender at least an hour that no later check can take
back, so only strong signals brake on their own: a request for a secret, text
addressed to the AI checker, promised returns, changed payment details, an
itemised invoice that does not add up, a near copy of a trusted payee's name,
reports from other senders. Weak signals (a deadline, a claim to be support or
an official, a payee account created in the last three days, a trusted payee's
name with a word added, amounts in running text that do not add up) brake only
when two different ones meet or a strong one is there too. A busy payee is no
reason on its own; next to a new account it is the money-mule profile and
counts as a weak signal. What does not brake is kept as a context finding: the
AI steps and the Why? view still see it, labelled as context.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from ..interfaces import VaultPort
from ..models import Finding, Hold, PaymentContext, Reason, StepRecord, Verdict
from .reasons import CONTEXT_REASONS, REPORT_WINDOW_MS, SHORT_LABELS, WEAK_REASONS, note_for, rules_hold_ms

if TYPE_CHECKING:
    from ..intel import PayeeIntel

log = logging.getLogger(__name__)

SOURCE = "rules"
NEW_ACCOUNT_HOURS = 72
MULE_MIN_SENDERS = 8
REPORTS_TO_BRAKE = 2
DETAIL_MAX = 160

# How much a signal weighs on its own.
STRONG = "strong"  # brakes alone
WEAK = "weak"  # brakes only together with another reason
CONTEXT = "context"  # never brakes; passed on as context
DETAIL = "detail"  # a pattern that describes a reason another pattern found; on its own, context


def weight_of(reason: Reason) -> str:
    """The weight of a reason when no pattern says otherwise."""
    if reason in CONTEXT_REASONS:
        return CONTEXT
    return WEAK if reason in WEAK_REASONS else STRONG


# ----------------------------------------------------------------------
# message heuristics
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class TextSignal:
    """One reason found in a message. The detail lists what matched in our own words, never quoting the text."""

    reason: Reason
    detail: str
    weight: str = STRONG  # STRONG, WEAK or CONTEXT (see ``weight_of``)


@dataclass(frozen=True)
class _Pattern:
    reason: Reason
    label: str
    regex: re.Pattern[str]
    negatable: bool = False  # skip matches that a "never", "don't" ... right before the verb turns down
    weight: str = ""  # STRONG, WEAK or DETAIL; empty for the reason's own weight
    check: Callable[[re.Match[str]], bool] | None = None  # a further test of each match
    by_line: bool = False  # also try each line on its own, so that a line start counts as a start

    @property
    def strength(self) -> str:
        return self.weight or weight_of(self.reason)


def _rule(
    reason: Reason,
    label: str,
    pattern: str,
    *,
    negatable: bool = False,
    cased: bool = False,
    weight: str = "",
    check: Callable[[re.Match[str]], bool] | None = None,
    by_line: bool = False,
) -> _Pattern:
    flags = 0 if cased else re.IGNORECASE
    return _Pattern(reason, label, re.compile(pattern, flags), negatable, weight, check, by_line)


_ASK = (
    r"(?:send(?:ing)?|shar(?:e|ing)|giv(?:e|ing)|enter(?:ing)?|typ(?:e|ing)|past(?:e|ing)|provid(?:e|ing)"
    r"|confirm(?:ing)?|verify(?:ing)?|validat(?:e|ing)|sync(?:ing)?|import(?:ing)?|submit(?:ting)?"
    r"|reply with|tell|read out|ask(?:s|ing)? for|need|requires?|input(?:ting)?|write down|dm)"
)
_SECRET_PHRASE = (
    r"(?:seed|recovery|secret|backup|mnemonic|wallet)[- ](?:phrase|words?)|mnemonic"
    r"|(?:12|24|twelve|twenty[- ]four)[- ]word(?:s| phrase)?"
)
_PRIVATE_KEY = r"private[- ]keys?|secret[- ]keys?|keystore(?: file)?"
_CODE = r"(?:one[- ]time|verification|login|security|2fa|sms|auth(?:entication)?)[- ](?:code|pin|passcode)|otp"
_FUNDS = r"(?:funds?|money|usdc|usdt|crypto|assets|balance|near|tokens|savings|coins)"
_OBJECT = (
    r"(?:it|this|that|(?:this|that|the|my|our) (?:payment|transfer|transaction|message|request|invoice|one))"
)
# What a changed-details message is about. A bare "address" or "bank" may be a postal address or
# the name of a bank, so those two need a payment in the same sentence (see ``_about_payment``).
_PAYMENT_TARGET = (
    r"(?:(?:usdc |usdt |crypto |payment |receiving |deposit |near )?(?:wallet|account)"
    r"|(?:wallet|usdc|usdt|crypto|payment|receiving|deposit|near) address|bank account"
    r"|(?:payment|bank|banking|wallet|account|receiving) details|address|bank)"
)
_DETAILS_NOUN = r"(?:wallets?|accounts?|address(?:es)?|details|instructions|bank(?: account)?s?|iban)"
_OLD = r"(?:old|previous|usual|former|earlier|original)"
_FREEZE = (
    r"(?:frozen|suspended|locked|closed|blocked|terminated|deleted|seized|confiscated|lost|forfeited|reset"
    r"|reversed|limited|restricted)"
)
_COUNT = (
    r"(?:\d{1,3}|two|three|four|five|six|ten|twelve|fifteen|twenty|thirty|forty[- ]five|forty|sixty|few"
    r"|couple of)"
)
# A short time window: "within 30 minutes", "in the next 24 hours", "within the next hour".
_WINDOW = (
    rf"(?:\bwithin (?:the next )?(?:{_COUNT}|an?|one) ?(?:minutes?|mins?|hours?|hrs?)\b"
    rf"|\bin (?:the next )?{_COUNT} (?:minutes?|mins?|hours?|hrs?)\b|\b(?:within|in) the next (?:hour|minute)\b)"
)
# Words that put a window on the reader: something to do, owe or lose within it. Not when they are
# part of an account id or a link ("tidewell-verify.testnet", "pay.example").
_DEMAND = (
    r"(?<![\w./@-])(?:send|sent|pay|paid|transfer|transferred|move|moved|settle|settled|deposit|deposited|post"
    r"|posted|confirm|verify|reply|respond|restore|complete|claim|act|re-?link|log in|sign in|must|needs?|have to"
    r"|has to|required|requires|due|unless)\b(?![.@-]\w)"
)
_CLAUSE = r"(?:[^.!?;]|\.(?=\w))"  # within one clause; a dot inside an account id or a number does not end it
_IN_SENTENCE = r"(?:[^.!?]|[.!?](?=\w))"  # within one sentence
# Lines written for an automated reader. Software brakes on its own when addressed; an assistant,
# a reviewer, a moderator or a model may be a person, so a line addressed to one counts only when
# the same sentence asks to clear the payment or says it is approved. Generic nouns (a system, a
# check) need a word that makes them software: "the safety checks" are a gas engineer's visit.
_QUALIFIER = r"(?:automated|automatic|ai|a\.i\.|fraud|risk|screening|scoring|aml|anti-fraud)"
_LOOSE_QUALIFIER = rf"(?:{_QUALIFIER}|payments?|compliance|safety|security|transaction)"
_SOFTWARE = r"(?:systems?|tools?|checks?|engines?|software)"
_ROBOT = r"(?:bots?|models?|classifiers?|scanners?|filters?|checkers?)"
_PERSONA = r"(?:assistants?|reviewers?|moderators?|checkers?|screeners?|approvers?|models?)"
_MACHINE = (
    rf"(?:(?-i:AI|A\.I\.)|bots?|chatbots?|llms?|language models?|(?:{_LOOSE_QUALIFIER}[ -]){{1,3}}{_ROBOT}"
    rf"|(?:{_QUALIFIER}[ -]){{1,3}}{_SOFTWARE}|(?:automated|automatic|ai|a\.i\.) (?:[\w-]+ ){{0,2}}?{_PERSONA})"
)
_TO_A_PERSONA = rf"(?:(?:any|all|every|the) )?(?:{_LOOSE_QUALIFIER} ){{0,2}}{_PERSONA}\b{_IN_SENTENCE}{{0,100}}?"
# An instruction to let the payment through, or a claim that it is approved.
_CLEAR = (
    r"\b(?:approve (?:it|this|the (?:payment|transfer|transaction))|approved|pre-? ?(?:approved|authori[sz]ed"
    r"|verified|cleared|screened)|whitelisted|let (?:it|this|this one|the (?:payment|transfer)) (?:through|pass)"
    r"|pass(?:ed)? (?:it|this)|release(?:d)? (?:it|this|the (?:payment|transfer|funds))"
    r"|clear(?:ed)? (?:it|this|the (?:payment|transfer))|allow (?:it|this|the (?:payment|transfer))"
    rf"|(?:mark|classify|treat|rate|score|label|tag|consider|report)(?: {_OBJECT})?(?: as)? (?:safe|approved"
    r"|trusted|verified|genuine|legit(?:imate)?|clean|low[- ]risk|no[- ]risk|benign|harmless|cleared|not (?:a )?scam)"
    r"|(?:do not|don't|never|must not|should not) (?:flag|hold|block|delay|stop|question|review|check|escalate)"
    r"|ignore (?:it|this|the (?:warnings?|flags?|alerts?|checks?|rules))|skip (?:it|this|the (?:check|review|hold))"
    r"|(?:return|report) no findings|nothing to (?:flag|report))\b"
)
# Trades whose titles look official: "Letting Agent Morgan", "Building Inspector Patel".
_TRADES = frozenset({
    "letting", "estate", "travel", "building", "insurance", "booking", "sales", "leasing", "property", "shipping",
    "ticket", "ticketing", "talent", "literary", "sports", "rental", "holiday", "fire", "health", "home", "gas",
    "safety", "customer", "support", "service", "press", "patent", "mortgage", "land", "housing", "tenancy",
    "executive", "finance", "financial", "medical", "planning", "food", "hygiene", "quality", "site", "school",
})
_PER_YEAR = {"day": 365, "daily": 365, "week": 52, "weekly": 52, "month": 12, "monthly": 12}
_STEEP = 50  # percent a year, simple: a fixed gain at least this steep is not a real offer
_GAIN_WORDS = re.compile(
    r"\b(?:earn|earns|earned|earning|earnings|pay|pays|paid|paying|payouts?|returns?|yields?|profits?|gains?|roi"
    r"|apy|interest|stake|staked|staking|compound|compounds|compounding|bonus|rewards?|income|dividends?|invest"
    r"|invested|investment|investors?|pool|plans?|deposits?)\b",
    re.IGNORECASE,
)
_COST_WORDS = re.compile(  # "with zero fees" is a selling point, not a cost
    r"(?<!\bzero )(?<!\bno )(?<!\bwithout )(?<!\blow )"
    r"\b(?:fee|fees|late|penalty|penalties|overdue|arrears|charge|charges|charged|surcharge|loan|loans|borrow"
    r"|borrowed|owe|owed|repay|repayment|debt|instal?ments?|fell|falls|decline|declined|discount|cheaper|tax|vat"
    r"|inflation|prices?)\b",
    re.IGNORECASE,
)
_PAYMENT_WORDS = re.compile(
    r"\b(?:pay|pays|paid|paying|payments?|transfers?|deposits?|invoices?|remit|remittance|wallet|usdc|usdt|crypto"
    r"|funds|salary|wages|rent)\b",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[.!?](?![\w/])")
_NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"


def _number(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def _sentence(match: re.Match[str], reach: int = 300) -> str:
    """The sentence the match is in, at most ``reach`` characters either side of it.

    A dot inside an account id or a link does not end a sentence. The bound keeps a long message
    without full stops from being read again for every match in it.
    """
    text = match.string
    begin = low = max(0, match.start() - reach)
    for end in _SENTENCE_END.finditer(text, low, match.start()):
        begin = end.end()
    high = min(len(text), match.end() + reach)
    stop = _SENTENCE_END.search(text, match.end(), high)
    return text[begin:stop.start() if stop else high]


_MODAL = re.compile(r"\b([a-z]+)(?:'ll| will| would| shall| usually| normally)\b((?: [\w'-]+){0,4}) ?$", re.IGNORECASE)
_OBLIGATION = re.compile(r"\b(?:need|needs|have to|has to|must|required|requires|due|owed|payable|expected)\b", re.I)


def _promised(before: str) -> bool:
    """Whether the words right before a time window promise it rather than put it on the reader.

    "Our team will reply", "we'll email the tickets", "the balance will unlock": someone else acts
    within the window. "You will need to pay", "you'll have to send" and "payment will be due" put
    it on the reader.
    """
    modal = _MODAL.search(before)
    if modal is None:
        return False
    subject, words = modal.group(1).lower(), modal.group(2)
    return subject not in ("you", "u") and not _OBLIGATION.search(words)


def _window_on_reader(match: re.Match[str]) -> bool:
    """A time window that is not promised by the words right before it."""
    start = match.start("window")
    return not _promised(match.string[max(0, start - 60):start])


def _more_back(match: re.Match[str]) -> bool:
    """A send-and-get-back claim where more comes back than went in."""
    sent, back = _number(match.group("sent")), _number(match.group("back"))
    return sent is not None and back is not None and back > sent


def _steep_gain(match: re.Match[str]) -> bool:
    """A fixed percentage gain per day, week or month that comes to at least ``_STEEP`` percent a year.

    The sentence must be about money coming in (earns, pays, returns, a pool, a plan), not about
    fees, loans, prices or losses: "late fees are 2 percent per day" is no investment offer.
    """
    rate = _number(match.group("rate"))
    if rate is None or rate * _PER_YEAR[match.group("period").lower()] < _STEEP:
        return False
    sentence = _sentence(match)
    return bool(_GAIN_WORDS.search(sentence)) and not _COST_WORDS.search(sentence)


def _about_payment(match: re.Match[str]) -> bool:
    """A changed address or bank that is about payments, not a house move or a new branch.

    A wallet, an account or payment details always are. A bare "address" or "bank" counts when the
    same sentence is about paying and no street address follows ("our new address is 12 Mill Lane").
    """
    if re.search(r"wallet|account|usdc|usdt|crypto|payment|receiving|deposit|banking|details", match.group(0), re.I):
        return True
    if re.match(r"\s*(?:is|:)\s*\d", match.string[match.end():]):
        return False
    return bool(_PAYMENT_WORDS.search(_sentence(match)))


def _for_a_third_party(match: re.Match[str]) -> bool:
    """A recovery phrase or key to be used on someone else's page, link or form, not in the user's own wallet."""
    sentence = _sentence(match)
    if re.search(r"\byour own\b", sentence, re.IGNORECASE):
        return False
    return bool(re.search(
        r"https?://|\bwww\.|\b(?:on|at|in|into|via|through|with) (?:the|this|that|our|a|an) (?:[\w-]+ )?"
        r"(?:page|site|website|link|form|portal|dapp|validator|agent|support|team|specialist|desk)\b",
        sentence,
        re.IGNORECASE,
    ))


def _claimed_office(match: re.Match[str]) -> bool:
    """A government office named like a sender's own ("the Tax Office", "this is ... from the tax office"),
    not mentioned in passing ("the tax office sent my refund", "I'm at the police station")."""
    if all(word[0].isupper() for word in match.group(0).split()):
        return True
    before = match.string[max(0, match.start() - 50):match.start()]
    return bool(re.search(
        r"\b(?:this is|this message is from|calling from|writing from|on behalf of|we are|we're|i am an?|i'm an?)\b"
        r"[^.!?]*$",
        before,
        re.IGNORECASE,
    ))


def _official_title(match: re.Match[str]) -> bool:
    """A title before a name that is not part of a trade's title ("Letting Agent Morgan")."""
    before = re.search(r"([A-Za-z]+)\s+$", match.string[max(0, match.start() - 30):match.start()])
    return before is None or before.group(1).lower() not in _TRADES


_PATTERNS: tuple[_Pattern, ...] = (
    # Requests for secrets. A request verb must come first, and "never share your
    # recovery phrase" style warnings are skipped. "Use your recovery phrase" asks for it only
    # when it is to go into someone else's page, link or form.
    _rule(Reason.SECRET_REQUEST, "asks for a recovery phrase",
          rf"\b{_ASK}\b[^.!?\n]{{0,48}}?\b(?:{_SECRET_PHRASE})\b", negatable=True),
    _rule(Reason.SECRET_REQUEST, "asks for a recovery phrase",
          rf"\bus(?:e|ing)\b[^.!?\n]{{0,48}}?\b(?:{_SECRET_PHRASE})\b", negatable=True, check=_for_a_third_party),
    _rule(Reason.SECRET_REQUEST, "asks for a private key",
          rf"\b{_ASK}\b[^.!?\n]{{0,48}}?\b(?:{_PRIVATE_KEY})\b", negatable=True),
    _rule(Reason.SECRET_REQUEST, "asks for a private key",
          rf"\bus(?:e|ing)\b[^.!?\n]{{0,48}}?\b(?:{_PRIVATE_KEY})\b", negatable=True, check=_for_a_third_party),
    _rule(Reason.SECRET_REQUEST, "asks for a one-time code",
          rf"\b{_ASK}(?: (?:me|us))?(?: (?:the|your|that))? (?:\d-digit )?(?:{_CODE})\b", negatable=True),
    _rule(Reason.SECRET_REQUEST, "asks for a one-time code",
          r"\b(?:send|give|tell|forward|read)(?: (?:me|us)) (?:the|your|that) (?:\d-digit )?code\b",
          negatable=True),
    # Pressure. Each of these is weak: honest payments have deadlines too. A time window counts
    # only when it is put on the reader (pay within, or else), not when the sender promises one.
    _rule(Reason.URGENCY, "a short deadline", rf"{_DEMAND}{_CLAUSE}{{0,60}}?(?P<window>{_WINDOW})",
          check=_window_on_reader),
    _rule(Reason.URGENCY, "a short deadline",
          rf"(?P<window>{_WINDOW}){_CLAUSE}{{0,30}}?\b(?:or|otherwise|unless)\b (?:else|it|they|the|your|you|we|this"
          r"|that)\b",
          check=_window_on_reader),
    _rule(Reason.URGENCY, "a short deadline",
          rf"\b(?:expires?|expiring|closes?|closing|ends?) (?:after|in|within) {_COUNT} (?:minutes?|mins?|hours?)\b"),
    _rule(Reason.URGENCY, "a short deadline",
          r"\b(?:before|by) (?:midnight|tonight|(?:the )?end of (?:the )?(?:business )?day|(?:the )?close of business"
          r"|it(?:'s| is) too late)\b"),
    _rule(Reason.URGENCY, "a short deadline", r"\b(?:expires?|expiring) (?:today|tonight|soon|in \d)"),
    _rule(Reason.URGENCY, "a threat to freeze or lose funds",
          rf"\bor (?:it|they|your (?:account|wallet|{_FUNDS}|earnings|rewards|profits)) (?:will|would|could|may) "
          rf"(?:be )?(?:permanently )?{_FREEZE}\b"),
    _rule(Reason.URGENCY, "a threat to freeze or lose funds",
          rf"\b(?:account|wallet|{_FUNDS}|earnings|rewards|profits)\b[^.!?]{{0,40}}\b(?:will|would|is going to"
          rf"|are going to|could|may) (?:be |get )?(?:permanently )?{_FREEZE}\b"),
    _rule(Reason.URGENCY, "a threat to freeze or lose funds",
          r"\b(?:account|wallet|funds?|assets?|balance)\b (?:has|have|is|are|was|were) (?:been )?(?:temporarily )?"
          r"(?:frozen|suspended|locked|compromised|restricted|blocked|limited)\b"),
    _rule(Reason.URGENCY, "a threat to freeze or lose funds",
          r"\b(?:otherwise|or else)\b[^.!?]{0,30}?\b(?:account|wallet|funds?|balance|earnings|rewards|profits"
          r"|commission|bonus)\b[^.!?]{0,15}?\b(?:is|are|gets?|will be) (?:reset|lost|forfeited|frozen|locked"
          r"|cancelled|canceled|wiped|seized)\b"),
    _rule(Reason.URGENCY, "a threat of arrest, a fine or legal action",
          r"\b(?:to avoid|or (?:face|risk)|unless|otherwise)\b[^.!?]{0,50}?(?:\b(?:warrants?|arrests?|seizure"
          r"|prosecution|legal action|court action|criminal charges?|penalt(?:y|ies)|fines|bailiffs?)\b"
          r"|\b(?:a|the|any|heavy|large|further) (?:[\w,-]+ ){0,2}?fine\b)"),
    _rule(Reason.URGENCY, "a threat of arrest, a fine or legal action",
          r"\b(?:warrant|arrest|seizure order|prosecution|legal action|criminal charges?)\b[^.!?]{0,40}?"
          r"\b(?:will|shall|is going to|are going to) be (?:issued|filed|executed|enforced|taken|brought|started)\b"),
    # Keeping a payment from the bank or the police; secrecy towards family is left out, since
    # surprise gifts ask for it too.
    _rule(Reason.URGENCY, "asks to keep it secret",
          r"\b(?:do not|don't|never) (?:tell|involve|call|contact|inform|alert) (?:the|your) (?:police|bank"
          r"|authorities|branch|branch staff|bank staff)\b"
          r"|\b(?:do not|don't) (?:discuss|share|mention) (?:this|it|the case|this case|the matter|this call)"
          r"(?: \w+)? with (?:anyone|anybody|your bank|the bank|branch staff|bank staff)\b"),
    _rule(Reason.URGENCY, "urgent wording",
          r"\b(?:urgent(?:ly)?|immediately|asap|act now|without delay|time[- ]sensitive"
          r"|(?:last|final) (?:chance|notice|warning))\b"),
    # Text written for the AI checker rather than for the person. A line addressed to an automated
    # reader brakes on its own; claims of approval alone only describe such a line (businesses also
    # write "pre-approved" to people) and are passed on as context.
    _rule(Reason.CHECKER_INJECTION, "addresses AI assistants",
          r"\b(?:note|message|instructions?|attention|memo|notice|reminder|request|warning|comment)s? (?:to|for) "
          rf"(?:(?:any|all|every|the) )?{_MACHINE}\b"),
    _rule(Reason.CHECKER_INJECTION, "addresses AI assistants",
          r"\b(?:note|message|instructions?|attention|memo|notice|reminder|request|comment)s? (?:to|for) "
          rf"{_TO_A_PERSONA}{_CLEAR}"),
    _rule(Reason.CHECKER_INJECTION, "addresses AI assistants",
          rf"\b{_MACHINE} (?:note|notice|instructions?|memo|message)s?\s?:"),
    _rule(Reason.CHECKER_INJECTION, "addresses AI assistants",
          rf"\b(?:{_LOOSE_QUALIFIER} )?{_PERSONA} (?:note|notice|instructions?|memo|message)s?\s?:"
          rf"{_IN_SENTENCE}{{0,100}}?{_CLEAR}"),
    _rule(Reason.CHECKER_INJECTION, "addresses AI assistants",
          rf"\b(?:dear|hey|hi|hello|greetings)\b,? (?:(?:the|any|all|every) )?{_MACHINE}\b"),
    _rule(Reason.CHECKER_INJECTION, "addresses AI assistants",
          rf"\b(?:dear|hey|hi|hello|greetings)\b,? {_TO_A_PERSONA}{_CLEAR}"),
    _rule(Reason.CHECKER_INJECTION, "addresses AI assistants",
          rf"(?:^|[(\[{{.!?;:]\s*)(?:to|attn|attention)[:,]? (?:(?:the|any|all|every) )?{_MACHINE}"
          r"(?:\s*(?:[:,)]|$)|\s+(?:reading|checking|reviewing|scanning|processing|that|who|which)\b)",
          by_line=True),
    _rule(Reason.CHECKER_INJECTION, "addresses AI assistants",
          rf"(?:^|[(\[{{.!?;:]\s*)(?:to|attn|attention)[:,]? {_TO_A_PERSONA}{_CLEAR}", by_line=True),
    _rule(Reason.CHECKER_INJECTION, "addresses AI assistants",  # not "Assistant:" or "Model:", which people write
          r"(?:^|[(\[.!?;]\s*)(?-i:AI|A\.I\.|SYSTEM|BOT)\s?:|\bsystem (?:message|override|instructions?|prompt)\s?:",
          by_line=True),
    _rule(Reason.CHECKER_INJECTION, "addresses AI assistants",
          r"\bif you are an? (?:ai|a\.i\.|language model|llm|bot|automated (?:system|checker|reviewer|filter|tool))\b"
          r"|\bas an? (?:ai|a\.i\.|language model|llm)(?:\s?[,:]|\s+(?:you|language|model|assistant|system|checker"
          r"|reviewer)\b)"),
    _rule(Reason.CHECKER_INJECTION, "addresses AI assistants",
          r"\b(?:automated|automatic|ai|a\.i\.) (?:[\w-]+ ){0,2}?(?:checkers?|reviewers?|filters?|scanners?|systems?"
          r"|tools?|screening|checks?|moderators?|classifiers?|models?) (?:must|should|shall|may|are to|need to"
          r"|have to|will) (?:not )?(?:flag|block|hold|delay|stop|review|question|check|screen|approve|pass|clear"
          r"|allow|ignore|skip|treat|classify|mark|accept|release)\b"),
    _rule(Reason.CHECKER_INJECTION, "addresses AI assistants",
          r"\b(?:automated|automatic|ai|a\.i\.) (?:[\w-]+ ){0,2}?(?:checkers?|reviewers?|filters?|scanners?|systems?"
          rf"|tools?|screening|checks?|moderators?|classifiers?|models?)\s?[:,]{_IN_SENTENCE}{{0,100}}?{_CLEAR}"),
    _rule(Reason.CHECKER_INJECTION, "asks to ignore instructions",
          r"\b(?:ignore|disregard|forget|override|bypass) (?:(?:all|any|the|your|these|those|of) )*"
          r"(?:(?:previous|prior|above|earlier|preceding|system|safety|security) )?"
          r"(?:instructions|rules|prompts?|guidelines|warnings?|checks?|polic(?:y|ies))\b"),
    _rule(Reason.CHECKER_INJECTION, "says the payment is pre-approved",
          r"\bpre-? ?(?:approved|authori[sz]ed|verified|cleared|screened)\b", weight=DETAIL),
    _rule(Reason.CHECKER_INJECTION, "says the payment is pre-approved",
          r"\b(?:this|the) (?:payment|transfer|transaction|request) (?:is|has been|was) (?:already )?"
          r"(?:whitelisted|marked safe|verified safe)\b", weight=DETAIL),
    _rule(Reason.CHECKER_INJECTION, "asks to mark it safe",
          rf"\b(?:mark|classify|flag|label|treat|rate|score|tag) (?:{_OBJECT} (?:as )?|as )"
          r"(?:safe|legit(?:imate)?|clean|low[- ]risk|no[- ]risk|benign|harmless|not (?:a )?scam)\b"),
    _rule(Reason.CHECKER_INJECTION, "asks to mark it safe",
          rf"\b(?:mark|classify|label|treat|rate|consider|tag) {_OBJECT} (?:as )?"
          r"(?:approved|trusted|verified|genuine)\b",
          weight=DETAIL),
    _rule(Reason.CHECKER_INJECTION, "asks not to hold the payment",
          r"\b(?:do not|don't|never|must not|mustn't|should not|shouldn't) (?:flag|block|hold) (?:this|the|my) "
          r"(?:payment|transfer|transaction)\b"),
    _rule(Reason.CHECKER_INJECTION, "prompt manipulation words", r"\b(?:system prompt|jailbreak|developer mode)\b"),
    # Changed payment details.
    _rule(Reason.DETAILS_CHANGED, "announces a new wallet or account",
          rf"\b(?:we|i|they|our company)(?:'ve| have| has)? (?:moved|changed|switched|updated|migrated) "
          rf"(?:to )?(?:a |our |my |the )?(?:new )?{_PAYMENT_TARGET}\b",
          check=_about_payment),
    _rule(Reason.DETAILS_CHANGED, "announces a new wallet or account",
          r"\b(?:our|my|the|this) (?:new|updated|changed|temporary|alternative|different|replacement) "
          rf"{_PAYMENT_TARGET}\b",
          check=_about_payment),
    _rule(Reason.DETAILS_CHANGED, "announces a new wallet or account",
          r"\b(?:our|my) (?:new|updated|changed|temporary|alternative|different|replacement) (?:[\w-]+ ){1,2}?"
          r"(?:wallet|account)\b"),
    _rule(Reason.DETAILS_CHANGED, "announces a new wallet or account",
          r"\b(?:our|my|the) (?:(?:wallet|payment|usdc|usdt|crypto|receiving|deposit|bank) )?(?:wallet|account|bank"
          r"|address|bank details|payment details|details) (?:has|have) (?:changed|moved)\b",
          check=_about_payment),
    _rule(Reason.DETAILS_CHANGED, "says not to use the old details",
          rf"\b(?:do not|don't|no longer|stop|never) (?:use|using|pay|paying|send|sending|transfer|transferring)"
          rf"(?: (?:to|into|via))? (?:the|our|my|any|that) (?:{_OLD} (?:[\w-]+ )?(?:{_DETAILS_NOUN}|one)"
          rf"|(?:[\w-]+ )?{_DETAILS_NOUN} (?:in|from|on|at|of) (?:the|our|my|that) {_OLD})\b"),
    _rule(Reason.DETAILS_CHANGED, "says not to use the old details",
          rf"\binstead of (?:the|our|my|your) {_OLD} (?:[\w-]+ )?{_DETAILS_NOUN}\b"),
    _rule(Reason.DETAILS_CHANGED, "says not to use the old details",
          rf"\b{_OLD} (?:[\w-]+ )?{_DETAILS_NOUN}\b[^.!?]{{0,30}}?\b(?:is|are|was|were|has been|have been) "
          r"(?:no longer|closed|deactivated|not monitored|invalid|not valid|out of use)"),
    _rule(Reason.DETAILS_CHANGED, "asks to update records",
          r"\bupdate (?:your|our|the|my) (?:records|beneficiary|wallet address|bank account|(?:[\w-]+ )?"
          r"(?:payment|bank|banking|wallet|payee|payroll|salary|deposit|beneficiary) (?:details|information|info))\b"),
    # Investment bait.
    _rule(Reason.IMPOSSIBLE_RETURNS, "guaranteed profit",
          r"\b(?:guaranteed|risk[- ]free|no[- ]risk|zero[- ]risk) (?:returns?|profits?|income|gains?|yields?"
          r"|interest)\b"),
    _rule(Reason.IMPOSSIBLE_RETURNS, "multiplied money",
          r"\b(?:double|triple|quadruple|2x|3x|5x|10x|100x|x2|x3|x5|x10) (?:your )?"
          r"(?:money|funds|crypto|investment|deposit|usdc|usdt|near|coins?|tokens?)\b"),
    _rule(Reason.IMPOSSIBLE_RETURNS, "multiplied money",
          r"\b(?:doubled|tripled) (?:my|your|his|her|their|our) (?:money|stack|savings|investment|deposit|funds"
          r"|crypto|holdings|portfolio|coins|tokens)\b"),
    _rule(Reason.IMPOSSIBLE_RETURNS, "multiplied money",
          r"\b(?:receive|get|gets|got|have it|has it|returned|returning|return|paid|pays|pay you|comes?|sent|send)"
          r"\b(?: [\w,]+){0,4}? (?:back (?:double|triple|2x|3x)|(?:double|triple|2x|3x) back)\b(?!-)"),
    _rule(Reason.IMPOSSIBLE_RETURNS, "a fixed percentage every day, week or month",
          r"(?<![\w.,])(?P<rate>\d{1,4}(?:\.\d+)?) ?(?:%|percent|per cent) (?:(?:a|per|every|each) )?"
          r"(?P<period>day|daily|week|weekly|month|monthly)\b",
          check=_steep_gain),
    _rule(Reason.IMPOSSIBLE_RETURNS, "a fixed percentage every day, week or month",
          r"\b(?P<period>daily|weekly|monthly) (?:roi|returns?|yields?|profits?|interest|gains?|payouts?|income) "
          r"(?:of |at |is |are )?(?:up to |about |around )?(?P<rate>\d{1,4}(?:\.\d+)?)(?![\d.]) ?"
          r"(?:%|percent|per cent)",
          check=_steep_gain),
    _rule(Reason.IMPOSSIBLE_RETURNS, "a send-and-get-back offer",
          rf"\b(?:send|sent|deposit|deposited|transfer|transferred|put in|invest|invested) \$?(?P<sent>{_NUMBER})"
          rf"[^.!?]{{0,40}}?\b(?:and|to|,) (?:get|got|receive|received|gets) \$?(?P<back>{_NUMBER}) (?:\w+ )?back\b",
          check=_more_back),
    # Fake support, officials and account-security lures. Weak, except a request to move
    # funds to a "protected" wallet: that is the safe-account scam itself.
    _rule(Reason.IMPERSONATION, "claims to be a support or security team",
          r"\b(?:support|security|fraud|compliance|verification|risk|trust (?:and|&) safety|account protection) "
          r"(?:team|department|desk|cent(?:er|re)|agent|officer|specialist|unit|division)\b"),
    _rule(Reason.IMPERSONATION, "signed as a support desk",
          r"\b[A-Z][\w&'.-]*(?: [A-Z][\w&'.-]*){0,2} (?:Support|Security|Help ?[Dd]esk|Customer (?:Care|Service))\b",
          cased=True),
    _rule(Reason.IMPERSONATION, "claims to be an official or a government office",
          r"\b(?:Officer|Agent|Deputy|Clerk|Inspector|Investigator|Detective|Sergeant|Constable|Marshal|Sheriff)"
          r" [A-Z][a-z]+\b",
          cased=True, check=_official_title),
    _rule(Reason.IMPERSONATION, "claims to be an official or a government office",
          r"\b(?:tax (?:office|authority)|revenue (?:office|service|agency|department|bureau)|customs (?:office|officer"
          r"|department)|police (?:department|station|officer|unit)|court (?:office|clerk|officer)|enforcement "
          r"(?:office|officer|unit|agency|department|team)|licensing (?:office|agency|authority))\b",
          check=_claimed_office),
    _rule(Reason.IMPERSONATION, "claims to be an official or a government office",
          r"\b(?:Federal|National|State|County|District|Interstate|Government)(?: [A-Z][a-z]+){0,3} "
          r"(?:Office|Bureau|Agency|Department|Commission|Authority)\b",
          cased=True),
    _rule(Reason.IMPERSONATION, "asks to move funds to a protected wallet",
          r"\b(?:move|moving|transfer|transferring|send|sending|withdraw|withdrawing|put|putting|deposit|depositing)"
          r"\b(?: [\w-]+){0,4}? (?:to|into|in) (?:a|an|the|this|our|your) (?:new |temporary )?(?:safe|safety|secure"
          r"|secured|protected|protection|recovery|quarantine|evidence|reversal) (?:[\w-]+ )?"
          r"(?:wallet|account|address|vault)\b",
          weight=STRONG),
    # Backup, holding and escrow accounts are also honest things to pay into; moving all of one's
    # own money there is the scam.
    _rule(Reason.IMPERSONATION, "asks to move funds to a protected wallet",
          rf"\b(?:move|moving|transfer|transferring|send|sending|withdraw|withdrawing|put|putting) (?:all )?(?:of )?"
          rf"your (?:{_FUNDS}|holdings|portfolio) (?:to|into|in) (?:a|an|the|this|our|your) (?:new |temporary )?"
          r"(?:backup|holding|escrow) (?:wallet|account|address|vault)\b",
          weight=STRONG),
    _rule(Reason.IMPERSONATION, "an account-security alert",
          r"\b(?:unusual|suspicious|unauthori[sz]ed|unrecognized) "
          r"(?:login|log-in|sign[- ]in|activity|access|device)\b"),
    _rule(Reason.IMPERSONATION, "an account-security alert",
          r"\b(?:detected|noticed|blocked|spotted|flagged) (?:an? |some )?(?:(?:unusual |suspicious |new |unknown )?"
          r"(?:login|log-in|sign[- ]in|access attempt)|(?:unusual|suspicious|unknown) activity)\b"),
    _rule(Reason.IMPERSONATION, "an account-security alert",
          r"\b(?:your|the) (?:wallet|account|vault|keys?|device)\b[^.!?]{0,30}?\b(?:compromised|hacked|breached"
          r"|exposed)\b"),
    _rule(Reason.IMPERSONATION, "an account-security alert",
          r"\b(?:hackers?|criminals?|scammers?|someone|a third party)\b[^.!?]{0,20}?\b(?:accessed|hacked"
          r"|compromised|broken into)\b[^.!?]{0,20}?\byour\b"),
)

# A negation spares a request only when it governs the request verb: right before it, or with
# words in between that keep it governing ("never share", "do not ever send", "we will never ask
# you to type", "no one at the bank will ask for"). "It won't take long so share ..." is a request.
_NEGATION_HEAD = (
    r"(?:\b(?:never|(?<!why )not|cannot)|n't|\b(?:under|in) no (?:circumstances?|case)|\bat no (?:point|time)"
    r"|\b(?:no one|nobody|no (?:legitimate|real|genuine|honest) [\w-]+)(?: [\w'-]+){0,3}? (?:will|would|should"
    r"|shall|can|could|may|must|is|are|does|do))"
)
_BRIDGE = (
    r"(?:ever|again|really|actually|please|once|we|i|they|you|us|anyone|anybody|from|our|the|team|staff|support"
    r"|will|would|should|shall|can|could|may|might|must|need|needs|have|has|had|to|be|been|being|do|does|did"
    r"|ask|asks|asked|asking|request|requests|requested|require|requires|required|tell|tells|want|wants|expect"
    r"|expects|dm|message|email|call|text|contact|for|that|trust|who|whoever|which|sign|log|in|on)"
)
_GOVERNING_NEGATION = re.compile(rf"{_NEGATION_HEAD}(?:,? {_BRIDGE}){{0,8}},? ?$", re.IGNORECASE)
_INVISIBLE = re.compile("[\u200b\u2060-\u2064\ufeff]")  # zero-width characters with no use in plain text
_SPOOFING = re.compile("[\u202a-\u202e\U000e0000-\U000e007f]")  # bidi overrides, Unicode tag characters
_STRIP = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f]")
_TYPOGRAPHY = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                             "\u2013": "-", "\u2014": "-", "\u2212": "-"})


def _clean(text: str) -> str:
    """NFKC, invisible characters removed, curly quotes straightened; line breaks kept."""
    text = unicodedata.normalize("NFKC", text)
    return _STRIP.sub("", text).translate(_TYPOGRAPHY)


def _hidden_characters(text: str) -> bool:
    return bool(_SPOOFING.search(text)) or len(_INVISIBLE.findall(text)) >= 3


def _negated(text: str, start: int) -> bool:
    return bool(_GOVERNING_NEGATION.search(text[max(0, start - 80):start]))


def _matches(pattern: _Pattern, text: str) -> bool:
    """Whether ``pattern`` matches anywhere it is not negated and its check passes.

    After a match is turned down the search resumes one character later, so a candidate that
    starts inside the rejected one is still tried.
    """
    position = 0
    while (match := pattern.regex.search(text, position)) is not None:
        negated = pattern.negatable and _negated(text, match.start())
        if not negated and (pattern.check is None or pattern.check(match)):
            return True
        position = match.start() + 1
    return False


def scan_message(text: str | None) -> list[TextSignal]:
    """Cheap pattern checks on a forwarded message: at most one signal per reason, in bit order.

    A reason found only by patterns that describe (``DETAIL``) is passed on as context.
    """
    if not text or not text.strip():
        return []
    cleaned = _clean(text)
    flat = re.sub(r"\s+", " ", cleaned).strip()
    lines = [re.sub(r"\s+", " ", line).strip() for line in cleaned.splitlines()]
    lines = [line for line in lines if line] if len(lines) > 1 else []
    labels: dict[Reason, list[str]] = {}
    weights: dict[Reason, set[str]] = {}
    for pattern in _PATTERNS:
        found = labels.setdefault(pattern.reason, [])
        hit = _matches(pattern, flat) or (pattern.by_line and any(_matches(pattern, line) for line in lines))
        if hit:
            weights.setdefault(pattern.reason, set()).add(pattern.strength)
            if pattern.label not in found:
                found.append(pattern.label)
    if _hidden_characters(text):
        labels.setdefault(Reason.CHECKER_INJECTION, []).append("hidden characters")
        weights.setdefault(Reason.CHECKER_INJECTION, set()).add(STRONG)
    signals = []
    for reason, found in labels.items():
        if not found:
            continue
        weight = next((w for w in (STRONG, WEAK) if w in weights[reason]), CONTEXT)
        signals.append(TextSignal(reason, ", ".join(found)[:DETAIL_MAX], weight))
    mismatch = invoice_check(text)
    if mismatch is not None:
        items, total, itemised = mismatch
        detail = f"line items add up to {items:,.2f} but the stated total is {total:,.2f}"
        signals.append(TextSignal(Reason.AMOUNT_MISMATCH, detail, STRONG if itemised else WEAK))
    return sorted(signals, key=lambda s: int(s.reason))


# ----------------------------------------------------------------------
# invoice arithmetic
# ----------------------------------------------------------------------

_CURRENCY = "$\u20ac\u00a3"  # dollar, euro and pound signs
_MONEY = re.compile(r"(?<![\w.,])(-\s?)?(?:[" + _CURRENCY + r"]\s?)?(\d{1,3}(?:,\d{3})+|\d+)\.(\d{2})(?![\d])")
# Where line items end: on an itemised invoice at a line break, a semicolon, a bar, a dash or the
# end of a sentence; in running text also at a comma, "and" or "plus" ("paper 600.00, envelopes
# 350.00 and delivery 200.00"). A comma only breaks when a space follows, so "1,250.00" stays whole.
_ITEM_BREAK = re.compile(r"\n|;|\|| - |(?<=[.!?])\s+(?=[A-Z])")
_RUNNING_BREAK = re.compile(r"\n|;|\|| - |,(?=\s)|\s+(?:and|plus)\s+|(?<=[.!?])\s+(?=[A-Z])")
_SUBTOTAL_LABEL = re.compile(r"\bsub-? ?total\s*[:=]?\s*(?:usdc|usdt|usd|husd|near|eur|gbp)?\s*$", re.IGNORECASE)
_TOTAL_LABEL = re.compile(
    r"\b(?:grand total|total (?:due|amount|payable)|amount due|balance due|total)\s*(?:\(\w+\))?\s*[:=]?\s*"
    r"(?:usdc|usdt|usd|husd|near|eur|gbp)?\s*$",
    re.IGNORECASE,
)
_DEDUCTION = re.compile(
    r"\b(?:discount|credit|rebate|deduction|deducted|less|minus|refund|refunded|bursary|scholarship|voucher|coupon"
    r"|waiver|subsidy|deposit paid|already paid)\b",
    re.IGNORECASE,
)
# An amount already paid or received: taken off the bill, or not part of it at all.
_SETTLED = re.compile(r"\b(?:paid|received|prepaid|prepayment)\b", re.IGNORECASE)
# A unit price, not a line total: "3 nights at 80.00", "25 x 14.00", "45.00 each".
_PRICE_BEFORE = re.compile(r"(?:\bat|\bx|\u00d7|@|\bper)\s*$", re.IGNORECASE)
_PRICE_AFTER = re.compile(r"^\s*(?:each|per|/)", re.IGNORECASE)
_PER_ONE = re.compile(r"^\s*per (?:order|invoice|delivery|booking|visit|job|trip|call-?out)\b", re.IGNORECASE)
_QUANTITY = re.compile(r"(?<![\w.,])(\d+(?:\.\d+)?)\s*(?:[a-z][\w-]*\s+){0,3}?(?:at|x|\u00d7|@)\s*$", re.IGNORECASE)


def _amount(match: re.Match[str]) -> Decimal:
    value = Decimal(f"{match.group(2).replace(',', '')}.{match.group(3)}")
    return -value if match.group(1) else value


@dataclass(frozen=True)
class _Item:
    amount: Decimal  # the line total, or the unit price when ``unit`` is set
    unit: bool = False  # a unit price whose quantity the line does not give
    settled: bool = False  # an amount already paid or received: taken off the bill, or not part of it


def _line_item(segment: str, match: re.Match[str]) -> _Item:
    """What an item's last amount stands for.

    A unit price counts times its quantity ("3 nights at 80.00" is 240.00) and once for a one-off
    charge ("200.00 per order"); without a number to multiply by ("45.00 each") only the unit price
    is known. Deductions count negative.
    """
    amount = _amount(match)
    after = segment[match.end():]
    unit = False
    if _PRICE_BEFORE.search(segment[:match.start()]) or (_PRICE_AFTER.match(after) and not _PER_ONE.match(after)):
        quantity = _QUANTITY.search(segment[:match.start()])
        if quantity:
            amount = Decimal(quantity.group(1)) * amount
        else:
            unit = True
    if amount > 0 and _DEDUCTION.search(segment):
        amount = -amount
    return _Item(amount, unit, bool(_SETTLED.search(segment)))


def _read_invoice(text: str, breaks: re.Pattern[str]) -> tuple[Decimal, Decimal] | bool | None:
    """One reading of an invoice: ``(items, total)`` when its numbers disagree, ``True`` when they
    add up, ``None`` when this reading cannot tell."""
    total: Decimal | None = None
    subtotal: Decimal | None = None
    before_subtotal = 0  # how many items come before the subtotal
    items: list[_Item] = []
    for segment in breaks.split(_clean(text)):
        last: re.Match[str] | None = None
        for match in _MONEY.finditer(segment):
            prefix = segment[max(0, match.start() - 32):match.start()]
            if _SUBTOTAL_LABEL.search(prefix):
                subtotal, before_subtotal = _amount(match), len(items)
            elif _TOTAL_LABEL.search(prefix):
                total = _amount(match)
            else:
                last = match
        if last is not None:
            items.append(_line_item(segment, last))
    units = [item for item in items if item.unit]
    if total is None or len(units) > 1 or (units and (subtotal is not None or units[0].amount <= 0)):
        return None
    sums: list[Decimal] = []
    # An amount already paid is either taken off the bill or left out of it; try both.
    for leave_out in (False, True) if any(item.settled for item in items) else (False,):
        kept = {n: item for n, item in enumerate(items) if not (leave_out and item.settled)}
        if len(kept) < 2:
            return None
        signed = {n: -abs(item.amount) if item.settled else item.amount for n, item in kept.items() if not item.unit}
        known = sum(signed.values(), Decimal(0))
        if units:
            price, rest = units[0].amount, total - known
            if rest >= 0 and rest % price == 0:
                return True  # some whole number of units makes the total (none: the line only gives a rate)
            sums.append(known + max(1, round(rest / price)) * price)
            continue
        if known == total:
            return True
        if subtotal is not None:
            before = sum((value for n, value in signed.items() if n < before_subtotal), Decimal(0))
            first = any(n < before_subtotal for n in signed)
            if (not first or before == subtotal) and subtotal + known - before == total:
                return True
        sums.append(known)
    return sums[0], total


def invoice_check(text: str | None) -> tuple[Decimal, Decimal, bool] | None:
    """``(sum of line items, stated total, itemised)`` when an invoice's numbers disagree, else ``None``.

    Conservative on purpose: it needs a labelled total and at least two line items with two-decimal
    amounts, and no way of reading the items may add up. They are read twice: as an itemised invoice
    (split at line breaks, semicolons and sentences) and as running text (also at commas, "and" and
    "plus"). ``itemised`` says whether the itemised reading found the mismatch; one found only in
    running text is less sure, since prose mentions amounts that are not items. In each item the
    last unlabelled amount is the line total (so "40 x 15.00 = 600.00" counts once), a unit price
    counts times its quantity, deductions count negative, an amount already paid is taken off or
    left out, and a subtotal is accepted when the lines before it add up to it. One unit price
    without a quantity is fine when a whole number of units makes the total; with two there is no
    answer.
    """
    if not text:
        return None
    itemised = _read_invoice(text, _ITEM_BREAK)
    running = _read_invoice(text, _RUNNING_BREAK)
    if itemised is True or running is True:
        return None
    if isinstance(itemised, tuple):
        return itemised[0], itemised[1], True
    if isinstance(running, tuple):
        return running[0], running[1], False
    return None


def invoice_mismatch(text: str | None) -> tuple[Decimal, Decimal] | None:
    """``(sum of line items, stated total)`` when an invoice's numbers disagree (see ``invoice_check``)."""
    found = invoice_check(text)
    return None if found is None else (found[0], found[1])


# ----------------------------------------------------------------------
# look-alike payees
# ----------------------------------------------------------------------

_NETWORK_SUFFIXES = (".testnet", ".near")
MIN_ADDED_WORD_NAME = 5  # a trusted name this long or longer is not a coincidence inside a longer one


def osa_distance(a: str, b: str) -> int:
    """Optimal string alignment distance: Damerau-Levenshtein where an adjacent swap costs one."""
    previous2: list[int] = []
    previous = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        current = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                current[j] = min(current[j], previous2[j - 2] + 1)
        previous2, previous = previous, current
    return previous[-1]


def _strip_network(account_id: str) -> str:
    name = account_id.lower()
    for suffix in _NETWORK_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def skeleton(account_id: str) -> str:
    """The account name with the network suffix, separators and digits removed."""
    return re.sub(r"[^a-z]", "", _strip_network(account_id))


def _words(account_id: str) -> list[str]:
    """The letter runs of the account name: ``acme-supplies-billing.testnet`` gives acme, supplies, billing."""
    return re.findall(r"[a-z]+", _strip_network(account_id))


def adds_a_word(longer: str, shorter: str) -> bool:
    """Whether ``longer`` is the name ``shorter`` with whole words added in front or behind.

    ``acme-supplies-billing`` and ``pay-acme-supplies`` add a word to ``acme-supplies``;
    ``mumford`` adds nothing to ``mum`` (no word boundary), and a name shorter than
    ``MIN_ADDED_WORD_NAME`` letters is too common to count.
    """
    big, small = _words(longer), _words(shorter)
    if not small or len(big) <= len(small) or len("".join(small)) < MIN_ADDED_WORD_NAME:
        return False
    return big[: len(small)] == small or big[-len(small):] == small


# How a payee name resembles a trusted one.
CLOSE = "close"  # the same letters, or an edit or two apart: brakes alone
ADDED_WORD = "added word"  # the trusted name with a word added: brakes only with another signal


def resemblance(a: str, b: str) -> str | None:
    """How account id ``a`` resembles ``b``: ``CLOSE``, ``ADDED_WORD`` or ``None``.

    A name with a word added is weaker evidence than a near copy: one owner often runs
    ``lucas-martin`` and ``lucas-martin-photo``.
    """
    if a == b or a.endswith(f".{b}") or b.endswith(f".{a}"):
        return None  # the same account, or a sub-account, which only its parent can create
    left, right = _strip_network(a), _strip_network(b)
    if len(skeleton(a)) >= 4 and skeleton(a) == skeleton(b):
        return CLOSE
    limit = 1 if min(len(left), len(right)) < 6 else 2
    if osa_distance(left, right) <= limit:
        return CLOSE
    if adds_a_word(a, b) or adds_a_word(b, a):
        return ADDED_WORD
    return None


def looks_alike(a: str, b: str) -> bool:
    """Whether two different account ids are close enough to be confused."""
    return resemblance(a, b) is not None


def lookalike_match(payee: str, trusted: Iterable[str]) -> tuple[str, str] | None:
    """``(trusted payee, resemblance)`` for the trusted payee ``payee`` could be mistaken for, a near copy first."""
    known = sorted({t for t in trusted if t})
    if payee in known:
        return None
    found = [(candidate, kind) for candidate in known if (kind := resemblance(payee, candidate)) is not None]
    return min(found, key=lambda pair: pair[1] != CLOSE, default=None)


def lookalike_of(payee: str, trusted: Iterable[str]) -> str | None:
    """The trusted payee that ``payee`` could be mistaken for, if any (never itself)."""
    found = lookalike_match(payee, trusted)
    return None if found is None else found[0]


# ----------------------------------------------------------------------
# payee facts
# ----------------------------------------------------------------------


def _ago(ms: int) -> str:
    """``about 2 h ago``, ``about 3 days ago`` or ``less than an hour ago``."""
    hours = ms / 3_600_000
    if hours < 1:
        return "less than an hour ago"
    if hours < 48:
        return f"about {round(hours)} h ago"
    return f"about {round(hours / 24)} days ago"


def payee_findings(profile: dict[str, Any], now_ms: int) -> list[Finding]:
    """Findings from what the chain says about the payee (see ``PayeeIntel.profile``).

    They are not weighed yet: ``weigh`` decides which of them brake.
    """
    findings: list[Finding] = []
    if profile.get("exists") is False:
        findings.append(Finding(Reason.UNKNOWN_PAYEE, "no NEAR account with this name exists", SOURCE))
        return findings
    reputation = profile.get("reputation") or {}
    # Reports count senders, each once per payee, and age from the latest report (not the
    # latest flag, which a later honest payment can set).
    reports = int(reputation.get("cancelled_after_flag") or 0)
    last_report = reputation.get("last_report_ms")
    if reports >= REPORTS_TO_BRAKE and last_report is not None and now_ms - int(last_report) <= REPORT_WINDOW_MS:
        detail = f"{reports} earlier senders cancelled after a warning; latest report {_ago(now_ms - int(last_report))}"
        findings.append(Finding(Reason.PAYEE_REPORTED, detail, SOURCE))
    age = profile.get("age_hours")
    if age is not None and not profile.get("age_is_lower_bound") and age < NEW_ACCOUNT_HOURS:
        findings.append(Finding(Reason.NEW_ACCOUNT, f"created {_ago(int(age * 3_600_000))}", SOURCE))
    senders = profile.get("distinct_inbound_senders_24h")
    if senders is not None and senders >= MULE_MIN_SENDERS:
        findings.append(Finding(Reason.MULE_PATTERN, f"{senders} different senders paid it in the last 24 h", SOURCE))
    return findings


# ----------------------------------------------------------------------
# weighing
# ----------------------------------------------------------------------


def weigh(signals: Iterable[tuple[Finding, str]]) -> list[Finding]:
    """The findings in bit order, those that are no reason to hold marked as context.

    The payment brakes when a strong signal is there, or when weak signals of two different
    reasons meet; weak signals then count as reasons too. Otherwise every weak signal is
    context, and so is every ``CONTEXT`` signal, with one exception: a busy payee whose account
    is also new is the money-mule profile, so there the busy payee weighs as a weak signal and
    the two brake together.
    """
    signals = list(signals)
    if any(finding.reason is Reason.NEW_ACCOUNT for finding, _ in signals):
        signals = [(f, WEAK if f.reason is Reason.MULE_PATTERN else weight) for f, weight in signals]
    weak = {finding.reason for finding, weight in signals if weight == WEAK}
    brakes = any(weight == STRONG for _, weight in signals) or len(weak) >= 2
    marked = [
        replace(finding, context=weight == CONTEXT or (weight == WEAK and not brakes)) for finding, weight in signals
    ]
    return sorted(marked, key=lambda f: int(f.reason))


def summary_for(findings: Iterable[Finding]) -> str:
    """The verdict's one-line note, naming context findings after the reasons."""
    findings = list(findings)
    bits = 0
    for finding in findings:
        bits |= 0 if finding.context else int(finding.reason)
    context = [SHORT_LABELS[f.reason] for f in findings if f.context]
    note = note_for(bits)
    if not context:
        return note
    return f"{note} Context only: {', '.join(context)}."


# ----------------------------------------------------------------------
# the RiskPort
# ----------------------------------------------------------------------


class RulesRisk:
    """The deterministic checks as a ``RiskPort``: payee facts, the sender's trust list, the message."""

    def __init__(self, intel: PayeeIntel, vault: VaultPort) -> None:
        self.intel = intel
        self.vault = vault

    async def assess(self, hold: Hold, ctx: PaymentContext) -> Verdict:
        started = time.monotonic()
        profile = await self._profile(hold.payee)
        now = await self._now_ms()
        signals = [(finding, weight_of(finding.reason)) for finding in payee_findings(profile, now)]
        lookalike = lookalike_match(hold.payee, await self._trusted(hold.sender, ctx))
        if lookalike is not None:
            trusted, kind = lookalike
            if kind == CLOSE:
                finding = Finding(Reason.LOOKALIKE_PAYEE, f"looks like your trusted payee {trusted}", SOURCE)
            else:
                finding = Finding(Reason.LOOKALIKE_PAYEE, f"your trusted payee {trusted} with a word added", SOURCE)
            signals.append((finding, STRONG if kind == CLOSE else WEAK))
        for signal in scan_message(ctx.forwarded_text):
            signals.append((Finding(signal.reason, signal.detail, SOURCE), signal.weight))
        findings = weigh(signals)
        step = StepRecord(
            step="precheck",
            latency_ms=round((time.monotonic() - started) * 1000),
            tool_calls=[{"name": "payee_profile", "sources": profile.get("source", "")}],
        )
        verdict = Verdict(
            hold_id=hold.id,
            brake=any(not f.context for f in findings),
            hold_for_ms=rules_hold_ms(findings),
            findings=findings,
            steps=[step],
        )
        verdict.summary = summary_for(findings)
        return verdict

    async def _profile(self, account_id: str) -> dict[str, Any]:
        try:
            return await self.intel.profile(account_id)
        except Exception:
            log.exception("payee profile failed for %s; checking the message only", account_id)
            return {}

    async def _now_ms(self) -> int:
        try:
            return await self.vault.now_ms()
        except Exception:
            log.warning("vault clock unavailable; using local time")
            return int(time.time() * 1000)

    async def _trusted(self, sender: str, ctx: PaymentContext) -> set[str]:
        """Trusted payees from the context and the sender's on-chain list (pending ones too)."""
        trusted = set(ctx.trusted_payees)
        try:
            user = await self.vault.get_user(sender)
        except Exception:
            log.warning("could not read the trust list of %s", sender)
            return trusted
        if user is not None:
            trusted.update(entry.payee for entry in user.trusted)
        return trusted
