//! NEP-297 events, logged as
//! `EVENT_JSON:{"standard":"holdup","version":"1.0.0","event":"<name>","data":[{...}]}`.
//!
//! Every hold event carries `hold_id`, `sender`, `payee`, `asset` and `amount`.

use near_sdk::json_types::{U128, U64};
use near_sdk::serde::Serialize;
use near_sdk::serde_json::{self, json, Map, Value};
use near_sdk::{env, AccountId};

use crate::asset::Asset;
use crate::hold::{to_hex, Hold};

pub const EVENT_STANDARD: &str = "holdup";
pub const EVENT_VERSION: &str = "1.0.0";

#[derive(Serialize)]
#[serde(crate = "near_sdk::serde")]
struct EventLog<'a> {
    standard: &'static str,
    version: &'static str,
    event: &'a str,
    data: [Value; 1],
}

fn emit(event: &str, data: Value) {
    let log = EventLog { standard: EVENT_STANDARD, version: EVENT_VERSION, event, data: [data] };
    // Serializing plain JSON values cannot fail.
    let json = serde_json::to_string(&log).unwrap_or_default();
    env::log_str(&format!("EVENT_JSON:{json}"));
}

/// The fields shared by all hold events, plus `extra`.
fn hold_event(event: &str, hold: &Hold, extra: Value) {
    let mut data = Map::new();
    data.insert("hold_id".into(), json!(U64(hold.id)));
    data.insert("sender".into(), json!(hold.sender));
    data.insert("payee".into(), json!(hold.payee));
    data.insert("asset".into(), json!(hold.asset.id()));
    data.insert("amount".into(), json!(U128(hold.amount)));
    if let Value::Object(fields) = extra {
        data.extend(fields);
    }
    emit(event, Value::Object(data));
}

pub fn user_registered(account_id: &AccountId, registered_ms: u64, fee: u128) {
    emit(
        "user_registered",
        json!({ "account_id": account_id, "registered_ms": U64(registered_ms), "fee": U128(fee) }),
    );
}

/// Money credited to a vault balance from outside (NEAR deposit or `ft_transfer_call`).
pub fn deposit(account_id: &AccountId, asset: &Asset, amount: u128, balance: u128) {
    emit(
        "deposit",
        json!({
            "account_id": account_id,
            "asset": asset.id(),
            "amount": U128(amount),
            "balance": U128(balance),
        }),
    );
}

/// `storage_fee` is the yoctoNEAR the sender paid the vault for the hold's storage.
pub fn hold_created(hold: &Hold, storage_fee: u128) {
    hold_event(
        "hold_created",
        hold,
        json!({
            "created_ms": U64(hold.created_ms),
            "release_at_ms": U64(hold.release_at_ms),
            "instant": hold.instant,
            "memo_hash": hold.memo_hash,
            "storage_fee": U128(storage_fee),
        }),
    );
}

/// `reason_bits` is the hold's accumulated set of reasons after this extension.
pub fn hold_extended(hold: &Hold, previous_release_at_ms: u64) {
    hold_event(
        "hold_extended",
        hold,
        json!({
            "previous_release_at_ms": U64(previous_release_at_ms),
            "release_at_ms": U64(hold.release_at_ms),
            "reason_bits": hold.reason_bits,
            "evidence_hash": hold.evidence_hash.as_ref().map(|hash| to_hex(hash)),
            "extensions": hold.extensions,
        }),
    );
}

/// `by` is `"sender"` or `"guardian"`; `report` is whether the cancel counted as a new report
/// against the payee (the hold was flagged and its sender had not reported the payee before).
pub fn hold_cancelled(hold: &Hold, by: &str, report: bool) {
    hold_event(
        "hold_cancelled",
        hold,
        json!({ "by": by, "cancelled_by": hold.cancelled_by, "flagged": hold.flagged, "report": report }),
    );
}

/// `internal` is true when the payee is a vault user and was credited inside the vault.
pub fn hold_released(hold: &Hold, internal: bool) {
    hold_event("hold_released", hold, json!({ "internal": internal, "instant": hold.instant }));
}

/// The external payout failed; the amount is back in the sender's vault balance.
pub fn hold_failed(hold: &Hold) {
    hold_event("hold_failed", hold, json!({ "refunded_to": hold.sender }));
}

/// `storage_fee` is 0 when the payee was already trusted.
pub fn trust_added(owner: &AccountId, payee: &AccountId, effective_ms: u64, storage_fee: u128) {
    emit(
        "trust_added",
        json!({
            "owner": owner,
            "payee": payee,
            "effective_ms": U64(effective_ms),
            "storage_fee": U128(storage_fee),
        }),
    );
}

pub fn trust_removed(owner: &AccountId, payee: &AccountId) {
    emit("trust_removed", json!({ "owner": owner, "payee": payee }));
}

pub fn guardian_changed(
    owner: &AccountId,
    guardian: Option<&AccountId>,
    effective_ms: u64,
    storage_fee: u128,
) {
    emit(
        "guardian_changed",
        json!({
            "owner": owner,
            "guardian": guardian,
            "effective_ms": U64(effective_ms),
            "storage_fee": U128(storage_fee),
        }),
    );
}

/// The guardian in charge dropped a pending change; `vetoed` is the guardian it would have set
/// (null for a removal).
pub fn guardian_change_vetoed(owner: &AccountId, guardian: &AccountId, vetoed: Option<&AccountId>) {
    emit("guardian_change_vetoed", json!({ "owner": owner, "guardian": guardian, "vetoed": vetoed }));
}
