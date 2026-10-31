"""NEP-297 event logs.

HoldVault logs every state change as ``EVENT_JSON:{"standard":"holdup",
"version":"1.0.0","event":"hold_created","data":[{...}]}``. These helpers
parse such logs out of transaction outcomes and build them for the
in-memory vault, so both adapters return identical ``TxResult.events``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

EVENT_PREFIX = "EVENT_JSON:"
STANDARD = "holdup"
VERSION = "1.0.0"


def parse_event_log(line: str) -> dict[str, Any] | None:
    """Parse one log line; None unless it is a well-formed NEP-297 event."""
    if not isinstance(line, str) or not line.startswith(EVENT_PREFIX):
        return None
    try:
        event = json.loads(line[len(EVENT_PREFIX) :])
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    if not all(isinstance(event.get(key), str) for key in ("standard", "version", "event")):
        return None
    data = event.get("data")
    if data is not None and not isinstance(data, list | dict):
        return None
    return event


def events_from_logs(logs: Iterable[str]) -> list[dict[str, Any]]:
    return [event for line in logs if (event := parse_event_log(line)) is not None]


def outcome_logs(result: dict[str, Any]) -> list[str]:
    """All logs of a raw ``send_tx``/``tx`` RPC result, in execution order."""
    outcomes = [result.get("transaction_outcome"), *(result.get("receipts_outcome") or [])]
    logs: list[str] = []
    for outcome in outcomes:
        if isinstance(outcome, dict):
            logs.extend(outcome.get("outcome", {}).get("logs", []))
    return logs


def events_from_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    return events_from_logs(outcome_logs(result))


def event_items(
    events: Iterable[dict[str, Any]], name: str, *, standard: str = STANDARD
) -> list[dict[str, Any]]:
    """The data entries of every ``name`` event of ``standard``, flattened."""
    items: list[dict[str, Any]] = []
    for event in events:
        if event.get("standard") != standard or event.get("event") != name:
            continue
        data = event.get("data")
        if isinstance(data, dict):
            items.append(data)
        elif isinstance(data, list):
            items.extend(item for item in data if isinstance(item, dict))
    return items


def first_item(events: Iterable[dict[str, Any]], name: str) -> dict[str, Any] | None:
    items = event_items(events, name)
    return items[0] if items else None


def make_event(name: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"standard": STANDARD, "version": VERSION, "event": name, "data": [data]}


def event_log(event: dict[str, Any]) -> str:
    return EVENT_PREFIX + json.dumps(event, separators=(",", ":"))
