"""Validate ScamBench (bench/scambench.jsonl) and describe its composition.

Checks: one JSON object per line, required fields and types, unique and well-formed ids,
label and category vocabularies, reason names, NEAR testnet account ids, amounts, payee
profiles, message hygiene (1 to 6 sentences, where each line of an itemised invoice counts
as one; ASCII only, line breaks allowed; no backticks or angle brackets; links only on
reserved .example domains; no real brand names), label balance, a source for every scam
item (listed in SOURCES.md), consistency between the payee profile and the profile-based
reasons, and the dev/test split. Optionally checks that no message shares an 8-word
sequence with the saved text of the cited source pages.

    python -m bench.validate                      # the same checks as python -m bench.run --check
    python -m bench.validate --source-texts DIR   # also the copy check against saved source pages
    python -m bench.validate --assign-splits      # give items without a split one, and rewrite the file

Standard library only.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import html
import json
import re
import statistics
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BENCH = Path(__file__).resolve().parent
DATASET = BENCH / "scambench.jsonl"
SOURCES = BENCH / "SOURCES.md"

REQUIRED = ["id", "label", "category", "message", "payee", "asset", "amount", "payee_profile",
            "trusted_lookalike", "expected_reasons", "source_url", "source_licence", "notes", "split"]
PROFILE_REQUIRED = ["age_hours", "distinct_inbound_senders_24h", "prior_releases_from_sender"]
PROFILE_OPTIONAL = ["reports_30d"]
CATEGORIES = {
    "scam": {"fake_support_safe_wallet", "unlock_or_withdrawal_fee", "investment_impossible_returns",
             "romance_emergency", "supplier_wallet_change", "impersonation_exchange", "impersonation_government",
             "job_task_scam", "giveaway_doubling", "recovery_scam", "marketplace_overpayment",
             "sextortion_payment", "other"},
    "benign": {"friend_split", "rent_known_landlord", "supplier_invoice_consistent", "marketplace_purchase",
               "donation", "family_support", "business_payroll", "urgent_but_legit", "new_payee_but_legit",
               "other"},
}
REASONS = ["NEW_ACCOUNT", "MULE_PATTERN", "IMPERSONATION", "URGENCY", "IMPOSSIBLE_RETURNS", "CHECKER_INJECTION",
           "DETAILS_CHANGED", "PAYEE_REPORTED", "PHISHING_LINK", "PUBLIC_WARNING", "LOOKALIKE_PAYEE",
           "AMOUNT_MISMATCH", "SECRET_REQUEST"]  # the payment reasons of Reason in holdup/models.py, in bit order
ASSETS = {"USDC", "NEAR"}
SPLITS = ("dev", "test")
# Thresholds the profile-based reasons were assigned with (the HoldUp build spec).
NEW_ACCOUNT_HOURS = 72
MULE_SENDERS = 8
REPORTS_MIN = 2
MIN_PER_LABEL = 60
# The split: about this share of every (label, category) stratum is in dev, picked in this hash order.
DEV_SHARE = 0.6
SPLIT_SALT = "scambench-split-v1"

ID_RE = re.compile(r"^sb-(scam|benign)-(\d{3})$")
TWIN_RE = re.compile(r"\bTwin: (sb-(?:scam|benign)-\d{3})")
ACCOUNT_RE = re.compile(r"^(([a-z\d]+[-_])*[a-z\d]+\.)*([a-z\d]+[-_])*[a-z\d]+$")
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,6})?$")
URL_RE = re.compile(r"https?://([^/\s]+)[^\s]*?(?=[.,;:!?)]?(?:\s|$))")
EMAIL_RE = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
BARE_DOMAIN_RE = re.compile(r"\b[\w-]+\.(?:com|net|org|io|co|uk|gov|app|xyz|info|me|ai)\b", re.I)
BRANDS = ["coinbase", "binance", "kraken", "paypal", "venmo", "zelle", "cash app", "western union", "moneygram",
          "amazon", "apple", "google", "microsoft", "facebook", "instagram", "whatsapp", "telegram", "twitter",
          "tiktok", "linkedin", "metamask", "ledger", "trezor", "tether", "bybit", "near foundation", "fbi", "irs",
          "hmrc", "ftc", "ic3", "dvla", "hmcts", "ncsc", "interpol", "europol"]
BRAND_RE = re.compile(r"\b(" + "|".join(re.escape(b) for b in BRANDS) + r")\b", re.I)
ABBREVIATIONS = ["Mr.", "Mrs.", "Ms.", "Dr.", "Co.", "Ltd.", "Inc.", "St.", "No.", "e.g.", "i.e.", "a.m.", "p.m.",
                 "vs."]


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def read_items(path: Path = DATASET) -> tuple[list[dict[str, Any]], list[str]]:
    """The items of a JSON-lines file and the lines that could not be read."""
    items, errors = [], []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            errors.append(f"line {n}: blank line")
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {n}: invalid JSON ({exc})")
            continue
        if not isinstance(item, dict):
            errors.append(f"line {n}: not a JSON object")
            continue
        items.append(item)
    return items, errors


def count_sentences(text: str) -> int:
    t = re.sub(r"https?://\S+?(?=[.,;:!?)]?(?:\s|$))", "URL", text)
    t = EMAIL_RE.sub("EMAIL", t)
    for a in ABBREVIATIONS:
        t = t.replace(a, a.replace(".", ""))
    t = re.sub(r"(?<=\w)\.(?=\w)", "", t)  # decimals, account ids, domains
    parts = [p for line in t.split("\n") for p in re.split(r"[.!?]+\)?(?:\s+|$)", line)]
    return len([p for p in parts if re.search(r"[A-Za-z]{2}", p)])


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def ngrams(tokens: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


def page_text(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix in (".html", ".htm"):
        raw = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", " ", raw)
        raw = re.sub(r"(?s)<[^>]+>", " ", raw)
        raw = html.unescape(raw)
    return raw


def parse_sources(path: Path = SOURCES) -> dict[str, str]:
    """Each source URL in SOURCES.md with the licence recorded next to it."""
    out: dict[str, str] = {}
    url = None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*-\s*URL:\s*(\S+)", line)
        if m:
            url = m.group(1).strip("<>")
            out[url] = ""
            continue
        m = re.match(r"\s*-\s*Licence:\s*(.+)", line)
        if m and url:
            out[url] = m.group(1).strip()
            url = None
    return out


def twins(items: Iterable[dict[str, Any]]) -> dict[str, str]:
    """Minimal pairs: items whose notes say ``Twin: <id>``, mapped both ways."""
    items = list(items)
    ids = {it.get("id") for it in items}
    pairs: dict[str, str] = {}
    for it in items:
        for other in TWIN_RE.findall(str(it.get("notes", ""))):
            if other in ids and other != it["id"]:
                pairs[it["id"]] = other
                pairs.setdefault(other, it["id"])
    return pairs


def _split_order(item_id: str) -> str:
    return hashlib.sha256(f"{SPLIT_SALT}:{item_id}".encode()).hexdigest()


def assign_splits(items: Sequence[dict[str, Any]]) -> dict[str, str]:
    """The dev/test split: stratified by label and category, deterministic, about 60/40.

    Items that already have a split keep it, so adding items never moves old ones. In each
    (label, category) stratum the first ``round(0.6 * n)`` items in sha256 order of
    ``scambench-split-v1:<id>`` go to dev and the rest to test. The benign item of a twin pair
    goes wherever its scam twin went, so a minimal pair is never divided between the splits.
    """
    split = {it["id"]: it["split"] for it in items if it.get("split") in SPLITS}
    pair = twins(items)
    strata: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for it in items:
        strata[(it["label"], it["category"])].append(it["id"])
    # Scam strata first, so that benign twins can follow their scam twin.
    for (label, _category), ids in sorted(strata.items(), key=lambda kv: (kv[0][0] != "scam", kv[0])):
        if label == "benign":
            for item_id in ids:
                if item_id not in split and pair.get(item_id) in split:
                    split[item_id] = split[pair[item_id]]
        quota = round(DEV_SHARE * len(ids))
        dev = sum(split.get(item_id) == "dev" for item_id in ids)
        for item_id in sorted((i for i in ids if i not in split), key=_split_order):
            split[item_id] = "dev" if dev < quota else "test"
            dev += split[item_id] == "dev"
    return split


def _check_item(it: dict[str, Any], where: str, sources: dict[str, str] | None, report: Report) -> None:
    errors = report.errors
    label = it["label"]
    if it["category"] not in CATEGORIES[label]:
        errors.append(f"{where}: category {it['category']!r} not allowed for {label}")
    for name in ("message", "notes", "payee", "amount"):
        if not isinstance(it[name], str) or not it[name].strip():
            errors.append(f"{where}: {name} must be a non-empty string")
    msg = it["message"] if isinstance(it["message"], str) else ""
    for name in ("message", "notes", "category", "payee"):
        value = it[name] if isinstance(it[name], str) else ""
        newline_ok = name == "message"
        if any(not (32 <= ord(c) < 127 or (newline_ok and c == "\n")) for c in value):
            errors.append(f"{where}: {name} contains non-ASCII characters")
    if "`" in msg or "<" in msg or ">" in msg:
        errors.append(f"{where}: message contains a backtick or angle bracket")
    sentences = count_sentences(msg)
    if not 1 <= sentences <= 6:
        errors.append(f"{where}: message has {sentences} sentences (allowed 1 to 6)")
    for host in [h.lower() for h in URL_RE.findall(msg)] + [h.lower() for h in EMAIL_RE.findall(msg)]:
        if not host.endswith(".example"):
            errors.append(f"{where}: link or email host {host!r} is not on a reserved .example domain")
    for domain in BARE_DOMAIN_RE.findall(URL_RE.sub(" ", EMAIL_RE.sub(" ", msg))):
        errors.append(f"{where}: message mentions a real-looking domain {domain!r}")
    for name in ("message", "payee", "notes"):
        value = it[name] if isinstance(it[name], str) else ""
        for brand in BRAND_RE.findall(value):
            errors.append(f"{where}: {name} mentions a real brand or agency name {brand!r}")
    payee = it["payee"]
    if not (isinstance(payee, str) and 2 <= len(payee) <= 64 and ACCOUNT_RE.match(payee)
            and payee.endswith(".testnet")):
        errors.append(f"{where}: payee {payee!r} is not a valid NEAR testnet account id")
    if it["asset"] not in ASSETS:
        errors.append(f"{where}: asset {it['asset']!r} not in {sorted(ASSETS)}")
    amount = it["amount"]
    if not (isinstance(amount, str) and AMOUNT_RE.match(amount) and float(amount) > 0):
        errors.append(f"{where}: amount {amount!r} must be a positive decimal string")
    if it["split"] not in SPLITS:
        errors.append(f"{where}: split must be dev or test")
    profile = it["payee_profile"]
    if not isinstance(profile, dict):
        errors.append(f"{where}: payee_profile must be an object")
        profile = {}
    for key in PROFILE_REQUIRED:
        if key not in profile:
            errors.append(f"{where}: payee_profile missing {key}")
    for key, value in profile.items():
        if key not in PROFILE_REQUIRED + PROFILE_OPTIONAL:
            errors.append(f"{where}: payee_profile has unknown key {key}")
        elif isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            errors.append(f"{where}: payee_profile.{key} must be a non-negative number")
        elif key != "age_hours" and not isinstance(value, int):
            errors.append(f"{where}: payee_profile.{key} must be an integer")
    lookalike = it["trusted_lookalike"]
    if lookalike is not None and not (isinstance(lookalike, str) and ACCOUNT_RE.match(lookalike)
                                      and lookalike != payee):
        errors.append(f"{where}: trusted_lookalike must be null or a different valid account id")
    reasons = it["expected_reasons"]
    if not isinstance(reasons, list) or any(r not in REASONS for r in reasons) or len(set(reasons)) != len(reasons):
        errors.append(f"{where}: expected_reasons must be a list of unique names from {REASONS}")
        reasons = [r for r in reasons if r in REASONS] if isinstance(reasons, list) else []
    elif reasons != sorted(reasons, key=REASONS.index):
        report.warnings.append(f"{where}: expected_reasons not in canonical bit order")
    if label == "benign":
        if reasons:
            errors.append(f"{where}: benign items must have an empty expected_reasons list")
        if it["source_url"] is not None or it["source_licence"] is not None:
            errors.append(f"{where}: benign items must have null source_url and source_licence")
        return
    if not reasons:
        errors.append(f"{where}: scam item without expected_reasons")
    src = it["source_url"]
    if not (isinstance(src, str) and re.match(r"^https?://\S+$", src)):
        errors.append(f"{where}: scam item needs an http(s) source_url")
    elif sources is not None and src not in sources:
        errors.append(f"{where}: source_url not listed in SOURCES.md: {src}")
    elif sources is not None and sources.get(src) and sources[src] != it["source_licence"]:
        errors.append(f"{where}: source_licence differs from SOURCES.md for {src}")
    if not (isinstance(it["source_licence"], str) and it["source_licence"].strip()):
        errors.append(f"{where}: scam item needs a source_licence")
    if "PUBLIC_WARNING" not in reasons:
        errors.append(f"{where}: every scam item follows a cited public warning, so it must list PUBLIC_WARNING")
    checks = [
        ("NEW_ACCOUNT", profile.get("age_hours", 0) < NEW_ACCOUNT_HOURS),
        ("MULE_PATTERN", profile.get("distinct_inbound_senders_24h", 0) >= MULE_SENDERS),
        ("PAYEE_REPORTED", profile.get("reports_30d", 0) >= REPORTS_MIN),
        ("LOOKALIKE_PAYEE", lookalike is not None),
    ]
    for reason, applies in checks:
        if (reason in reasons) != applies:
            errors.append(f"{where}: {reason} {'missing' if applies else 'present'} but the payee data says "
                          f"{'it applies' if applies else 'it does not'}")


def validate(
    items: Sequence[dict[str, Any]], *, sources: dict[str, str] | None = None, source_texts: Path | None = None
) -> Report:
    """Every check above; ``sources`` (from SOURCES.md) enables the source checks."""
    report = Report()
    seen: set[str] = set()
    numbers: dict[str, list[int]] = collections.defaultdict(list)
    valid: list[dict[str, Any]] = []
    for n, it in enumerate(items, 1):
        where = f"item {n} ({it.get('id', '?')})"
        missing = [f for f in REQUIRED if f not in it]
        extra = [f for f in it if f not in REQUIRED]
        if missing:
            report.errors.append(f"{where}: missing fields {missing}")
            continue
        if extra:
            report.errors.append(f"{where}: unexpected fields {extra}")
        label, item_id = it["label"], it["id"]
        if label not in CATEGORIES:
            report.errors.append(f"{where}: label must be scam or benign, not {label!r}")
            continue
        m = ID_RE.match(item_id) if isinstance(item_id, str) else None
        if not m or m.group(1) != label:
            report.errors.append(f"{where}: id does not match sb-{label}-NNN")
        else:
            numbers[label].append(int(m.group(2)))
        if item_id in seen:
            report.errors.append(f"{where}: duplicate id")
        seen.add(item_id)
        _check_item(it, where, sources, report)
        valid.append(it)

    for label, nums in numbers.items():
        if sorted(nums) != list(range(1, len(nums) + 1)):
            report.warnings.append(f"{label} ids are not numbered 001..{len(nums):03d} without gaps")
    _check_duplicates(valid, report)
    _check_balance(valid, report)
    _check_split(valid, report)
    if source_texts is not None:
        _check_copies(valid, source_texts, report)
    return report


def _check_duplicates(items: Sequence[dict[str, Any]], report: Report) -> None:
    tokens = [(it["id"], set(words(it.get("message", "")))) for it in items]
    for i in range(len(tokens)):
        for j in range(i + 1, len(tokens)):
            a, b = tokens[i][1], tokens[j][1]
            if a and b:
                jaccard = len(a & b) / len(a | b)
                if jaccard >= 0.85:
                    report.errors.append(f"{tokens[i][0]} and {tokens[j][0]}: near-duplicate messages "
                                         f"(Jaccard {jaccard:.2f})")
                elif jaccard >= 0.6:
                    report.warnings.append(f"{tokens[i][0]} and {tokens[j][0]}: similar messages "
                                           f"(Jaccard {jaccard:.2f})")


def _check_balance(items: Sequence[dict[str, Any]], report: Report) -> None:
    scams = [it for it in items if it["label"] == "scam"]
    benign = [it for it in items if it["label"] == "benign"]
    for name, group in (("scam", scams), ("benign", benign)):
        if len(group) < MIN_PER_LABEL:
            report.errors.append(f"only {len(group)} {name} items (need at least {MIN_PER_LABEL})")
    injection = [it for it in scams if "CHECKER_INJECTION" in it["expected_reasons"]]
    share = len(injection) / len(scams) if scams else 0.0
    if not 0.05 <= share <= 0.15:
        report.errors.append(f"checker-injection share is {share:.1%}; expected about 10 percent")


def _check_split(items: Sequence[dict[str, Any]], report: Report) -> None:
    by_id = {it["id"]: it for it in items}
    for item_id, other in twins(items).items():
        if item_id < other and by_id[item_id]["split"] != by_id[other]["split"]:
            report.errors.append(f"{item_id} and {other} are twins but sit in different splits")
    strata: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for it in items:
        strata[(it["label"], it["category"])].append(it["split"])
    for (label, category), splits in sorted(strata.items()):
        quota = round(DEV_SHARE * len(splits))
        if abs(splits.count("dev") - quota) > 1:
            report.warnings.append(f"{label}/{category}: {splits.count('dev')} of {len(splits)} items in dev, "
                                   f"expected about {quota}")


def _check_copies(items: Sequence[dict[str, Any]], folder: Path, report: Report) -> None:
    pages = [p for p in folder.iterdir() if p.suffix in (".html", ".htm", ".txt")]
    grams8: set[tuple[str, ...]] = set()
    grams6: set[tuple[str, ...]] = set()
    for page in pages:
        tokens = words(page_text(page))
        grams8 |= ngrams(tokens, 8)
        grams6 |= ngrams(tokens, 6)
    shared6 = 0
    for it in items:
        tokens = words(it["message"])
        if hit := ngrams(tokens, 8) & grams8:
            report.errors.append(f"{it['id']}: shares an 8-word sequence with a source page: {' '.join(min(hit))}")
        shared6 += bool(ngrams(tokens, 6) & grams6)
    report.warnings.append(f"copy check: {len(pages)} saved pages; {shared6} messages share a 6-word sequence")


def composition(items: Sequence[dict[str, Any]], sources: dict[str, str] | None = None) -> list[str]:
    """What the set contains, as short lines."""

    def counter(values: Iterable[Any]) -> str:
        counts = collections.Counter(values)
        return ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0]))))

    scams = [it for it in items if it.get("label") == "scam"]
    benign = [it for it in items if it.get("label") == "benign"]
    injection = [it for it in scams if "CHECKER_INJECTION" in it.get("expected_reasons", [])]
    urls = {it.get("source_url") for it in scams}
    lines = [
        f"items: {len(items)} (scam {len(scams)}, benign {len(benign)})",
        "split: " + "; ".join(
            f"{name} {sum(it.get('split') == name for it in items)} "
            f"(scam {sum(it.get('split') == name for it in scams)}, "
            f"benign {sum(it.get('split') == name for it in benign)})"
            for name in SPLITS
        ),
        "scam categories: " + counter(it["category"] for it in scams),
        "benign categories: " + counter(it["category"] for it in benign),
        "expected reasons on scam items: " + counter(r for it in scams for r in it.get("expected_reasons", [])),
        f"checker-injection items: {len(injection)} of {len(scams)} scams",
        f"scam items with a source: {sum(bool(it.get('source_url')) for it in scams)} of {len(scams)}; "
        f"distinct source URLs: {len(urls)}"
        + (f"; all listed in SOURCES.md: {all(u in sources for u in urls)}" if sources is not None else ""),
        "source licences: " + counter(it.get("source_licence") for it in scams),
        f"hard cases: scam {sum(it.get('notes', '').startswith('Hard case') for it in scams)}, "
        f"benign {sum(it.get('notes', '').startswith('Hard case') for it in benign)}",
        f"twin pairs: {len(twins(items)) // 2}",
        "assets: " + counter(it.get("asset") for it in items),
    ]
    sentences = [count_sentences(it["message"]) for it in items if isinstance(it.get("message"), str)]
    if sentences:
        lines.append(f"sentences per message: min {min(sentences)}, median {statistics.median(sentences):g}, "
                     f"max {max(sentences)}")
    return lines


def write_items(items: Sequence[dict[str, Any]], path: Path = DATASET) -> None:
    """One JSON object per line, fields in the documented order."""
    lines = [json.dumps({k: it[k] for k in REQUIRED if k in it}, ensure_ascii=True) for it in items]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("jsonl", nargs="?", default=str(DATASET))
    parser.add_argument("--sources", default=str(SOURCES))
    parser.add_argument("--source-texts", help="folder of saved source pages (.html, .txt) for the copy check")
    parser.add_argument("--assign-splits", action="store_true",
                        help="give every item without a split one, then rewrite the file")
    args = parser.parse_args(argv)

    path = Path(args.jsonl)
    items, unreadable = read_items(path)
    if args.assign_splits:
        if unreadable:
            print("\n".join(f"ERROR: {e}" for e in unreadable), file=sys.stderr)
            return 1
        split = assign_splits(items)
        changed = sum(it.get("split") != split[it["id"]] for it in items)
        for it in items:
            it["split"] = split[it["id"]]
        write_items(items, path)
        print(f"{changed} item(s) given a split; {path} rewritten")
    sources = parse_sources(Path(args.sources)) if Path(args.sources).exists() else None
    report = validate(items, sources=sources, source_texts=Path(args.source_texts) if args.source_texts else None)
    report.errors[:0] = unreadable
    if sources is None:
        report.errors.append(f"{args.sources} not found; cannot check that scam sources are listed")
    print("ScamBench validation:", "OK" if report.ok else "FAILED")
    print(f"file: {path}")
    for line in composition(items, sources):
        print(line)
    for warning in report.warnings:
        print("WARNING:", warning)
    for error in report.errors:
        print("ERROR:", error)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
