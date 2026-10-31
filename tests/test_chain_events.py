"""NEP-297 event logs are parsed from transaction outcomes, and junk is ignored."""

from __future__ import annotations

import json

from holdup.chain.events import (
    event_items,
    event_log,
    events_from_logs,
    events_from_result,
    first_item,
    make_event,
    parse_event_log,
)

CREATED = {
    "standard": "holdup",
    "version": "1.0.0",
    "event": "hold_created",
    "data": [{"hold_id": "3", "sender": "a.testnet", "payee": "b.testnet", "asset": "near", "amount": "5"}],
}


def test_a_well_formed_event_is_parsed() -> None:
    assert parse_event_log("EVENT_JSON:" + json.dumps(CREATED)) == CREATED


def test_malformed_logs_are_ignored() -> None:
    junk = [
        "hold 3 created",
        "EVENT_JSON:not json",
        "EVENT_JSON:[1,2,3]",
        'EVENT_JSON:{"standard":"holdup","event":"x"}',
        'EVENT_JSON:{"standard":"holdup","version":"1.0.0","event":"x","data":5}',
        " EVENT_JSON:" + json.dumps(CREATED),
    ]
    assert events_from_logs(junk) == []


def test_events_come_from_every_receipt_in_order() -> None:
    nep141 = {"standard": "nep141", "version": "1.0.0", "event": "ft_transfer", "data": [{"amount": "1"}]}
    released = make_event("hold_released", {"hold_id": "3", "internal": False})
    result = {
        "transaction_outcome": {"id": "tx", "outcome": {"logs": []}},
        "receipts_outcome": [
            {"id": "r1", "outcome": {"logs": ["log line", event_log(make_event("hold_created", CREATED["data"][0]))]}},
            {"id": "r2", "outcome": {"logs": ["EVENT_JSON:" + json.dumps(nep141)]}},
            {"id": "r3", "outcome": {"logs": [event_log(released)]}},
        ],
    }
    events = events_from_result(result)
    assert [e["event"] for e in events] == ["hold_created", "ft_transfer", "hold_released"]
    assert first_item(events, "hold_released") == {"hold_id": "3", "internal": False}
    assert event_items(events, "ft_transfer") == []  # other standards are kept apart
    assert event_items(events, "ft_transfer", standard="nep141") == [{"amount": "1"}]


def test_event_log_round_trips_compactly() -> None:
    event = make_event("trust_added", {"owner": "a.testnet", "payee": "b.testnet", "effective_ms": "1"})
    line = event_log(event)
    assert line.startswith('EVENT_JSON:{"standard":"holdup","version":"1.0.0","event":"trust_added"')
    assert " " not in line
    assert parse_event_log(line) == event
