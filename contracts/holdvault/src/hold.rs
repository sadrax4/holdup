use near_sdk::json_types::{U128, U64};
use near_sdk::{near, AccountId};

use crate::asset::Asset;

#[near(serializers = [borsh, json])]
#[serde(rename_all = "lowercase")]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HoldStatus {
    /// Money is waiting in the vault. Only this status can be extended, cancelled or released.
    Held,
    /// An external payout is in flight; `on_payout` settles it.
    Releasing,
    Released,
    Cancelled,
    /// The external payout failed and the amount went back to the sender's vault balance.
    Failed,
}

/// A payment from `sender` to `payee`. The payee is fixed when the hold is created:
/// money only ever leaves a hold towards that payee or back to the sender.
#[near(serializers = [borsh])]
#[derive(Clone, Debug)]
pub struct Hold {
    pub id: u64,
    pub sender: AccountId,
    pub payee: AccountId,
    pub asset: Asset,
    pub amount: u128,
    pub created_ms: u64,
    /// Never decreases, and only `extend_hold` raises it, up to `created_ms + max_hold_ms`.
    pub release_at_ms: u64,
    pub status: HoldStatus,
    pub instant: bool,
    pub flagged: bool,
    pub extensions: u32,
    pub reason_bits: u32,
    /// sha256 of the evidence passed to the latest `extend_hold`.
    pub evidence_hash: Option<[u8; 32]>,
    pub memo_hash: Option<String>,
    pub settled_ms: Option<u64>,
    pub cancelled_by: Option<AccountId>,
}

#[near(serializers = [json])]
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HoldView {
    pub id: U64,
    pub sender: AccountId,
    pub payee: AccountId,
    pub asset: String,
    pub amount: U128,
    pub created_ms: U64,
    pub release_at_ms: U64,
    pub status: HoldStatus,
    pub instant: bool,
    pub flagged: bool,
    pub extensions: u32,
    pub reason_bits: u32,
    pub evidence_hash: Option<String>,
    pub memo_hash: Option<String>,
    pub settled_ms: Option<U64>,
    pub cancelled_by: Option<AccountId>,
}

#[near(serializers = [json])]
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SendView {
    pub hold: HoldView,
}

impl From<&Hold> for HoldView {
    fn from(hold: &Hold) -> Self {
        HoldView {
            id: U64(hold.id),
            sender: hold.sender.clone(),
            payee: hold.payee.clone(),
            asset: hold.asset.id().to_owned(),
            amount: U128(hold.amount),
            created_ms: U64(hold.created_ms),
            release_at_ms: U64(hold.release_at_ms),
            status: hold.status,
            instant: hold.instant,
            flagged: hold.flagged,
            extensions: hold.extensions,
            reason_bits: hold.reason_bits,
            evidence_hash: hold.evidence_hash.as_ref().map(|hash| to_hex(hash)),
            memo_hash: hold.memo_hash.clone(),
            settled_ms: hold.settled_ms.map(U64),
            cancelled_by: hold.cancelled_by.clone(),
        }
    }
}

/// Per-payee counters that let the next sender see how earlier payments to this payee ended.
#[near(serializers = [borsh])]
#[derive(Clone, Debug, Default)]
pub struct PayeeStats {
    pub holds: u32,
    pub flagged: u32,
    pub cancelled: u32,
    /// Reports: senders who cancelled a payment to this payee after an oracle flagged it,
    /// each sender counted once.
    pub cancelled_after_flag: u32,
    pub released: u32,
    /// When a hold to this payee was last flagged.
    pub last_flag_ms: Option<u64>,
    /// When the latest report was counted.
    pub last_report_ms: Option<u64>,
}

#[near(serializers = [json])]
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct PayeeStatsView {
    pub holds: u32,
    pub flagged: u32,
    pub cancelled: u32,
    pub cancelled_after_flag: u32,
    pub released: u32,
    pub last_flag_ms: Option<U64>,
    pub last_report_ms: Option<U64>,
}

impl From<&PayeeStats> for PayeeStatsView {
    fn from(stats: &PayeeStats) -> Self {
        PayeeStatsView {
            holds: stats.holds,
            flagged: stats.flagged,
            cancelled: stats.cancelled,
            cancelled_after_flag: stats.cancelled_after_flag,
            released: stats.released,
            last_flag_ms: stats.last_flag_ms.map(U64),
            last_report_ms: stats.last_report_ms.map(U64),
        }
    }
}

/// Lowercase hex encoding.
pub fn to_hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(DIGITS[usize::from(byte >> 4)] as char);
        out.push(DIGITS[usize::from(byte & 0x0f)] as char);
    }
    out
}
