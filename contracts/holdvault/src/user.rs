use std::collections::BTreeMap;

use near_sdk::json_types::{U128, U64};
use near_sdk::{near, AccountId};

use crate::asset::Asset;
use crate::errors::{ensure, Error, SafeMath};

/// Maximum number of trusted payees per user.
pub const MAX_TRUSTED: usize = 50;

/// Length of a UTC day in milliseconds; instant caps reset at `now_ms / DAY_MS` boundaries.
pub const DAY_MS: u64 = 86_400_000;

/// A registered vault user. Balance and instant-usage entries for every configured asset are
/// created at registration, so later credits and debits never grow storage.
#[near(serializers = [borsh])]
#[derive(Clone, Debug)]
pub struct User {
    pub registered_ms: u64,
    pub balances: BTreeMap<Asset, u128>,
    pub instant_usage: BTreeMap<Asset, DailyUsage>,
    pub guardian: Option<AccountId>,
    pub pending_guardian: Option<PendingGuardian>,
    /// Trusted payee -> time from which payments to it may be instant.
    pub trusted: BTreeMap<AccountId, u64>,
    /// Ids of this user's holds with status `held`, oldest first.
    pub active_holds: Vec<u64>,
    /// Number of holds this user has ever sent (length of their history).
    pub holds_sent: u32,
}

/// How much a user paid instantly on a given UTC day.
#[near(serializers = [borsh])]
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct DailyUsage {
    pub day: u64,
    pub used: u128,
}

/// A guardian change that waits for `guardian_delay_ms`. `guardian: None` removes the guardian.
#[near(serializers = [borsh, json])]
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PendingGuardian {
    pub guardian: Option<AccountId>,
    pub effective_ms: u64,
}

impl User {
    pub fn new(registered_ms: u64, assets: impl Iterator<Item = Asset>) -> Self {
        let mut balances = BTreeMap::new();
        let mut instant_usage = BTreeMap::new();
        for asset in assets {
            balances.insert(asset.clone(), 0);
            instant_usage.insert(asset, DailyUsage::default());
        }
        User {
            registered_ms,
            balances,
            instant_usage,
            guardian: None,
            pending_guardian: None,
            trusted: BTreeMap::new(),
            active_holds: Vec::new(),
            holds_sent: 0,
        }
    }

    pub fn balance(&self, asset: &Asset) -> u128 {
        self.balances.get(asset).copied().unwrap_or(0)
    }

    pub fn credit(&mut self, asset: &Asset, amount: u128) {
        let balance = self.balances.entry(asset.clone()).or_insert(0);
        *balance = balance.safe_add(amount);
    }

    pub fn debit(&mut self, asset: &Asset, amount: u128) {
        let balance = self.balances.entry(asset.clone()).or_insert(0);
        ensure(*balance >= amount, Error::InsufficientBalance, "balance is lower than the amount");
        *balance = balance.safe_sub(amount);
    }

    /// The guardian in charge at `now`: a pending change counts once its delay has passed.
    pub fn effective_guardian(&self, now_ms: u64) -> Option<&AccountId> {
        match &self.pending_guardian {
            Some(pending) if pending.effective_ms <= now_ms => pending.guardian.as_ref(),
            _ => self.guardian.as_ref(),
        }
    }

    /// The guardian change that is still waiting at `now`, if any.
    pub fn pending_guardian_at(&self, now_ms: u64) -> Option<&PendingGuardian> {
        self.pending_guardian.as_ref().filter(|pending| pending.effective_ms > now_ms)
    }

    /// Makes a due pending change permanent.
    pub fn apply_due_guardian(&mut self, now_ms: u64) {
        if let Some(pending) = self.pending_guardian.take() {
            if pending.effective_ms <= now_ms {
                self.guardian = pending.guardian;
            } else {
                self.pending_guardian = Some(pending);
            }
        }
    }

    pub fn trusts_at(&self, payee: &AccountId, now_ms: u64) -> bool {
        self.trusted.get(payee).is_some_and(|effective_ms| *effective_ms <= now_ms)
    }

    pub fn instant_used(&self, asset: &Asset, day: u64) -> u128 {
        self.instant_usage.get(asset).filter(|usage| usage.day == day).map_or(0, |usage| usage.used)
    }

    pub fn record_instant(&mut self, asset: &Asset, day: u64, amount: u128) {
        let used = self.instant_used(asset, day).safe_add(amount);
        self.instant_usage.insert(asset.clone(), DailyUsage { day, used });
    }
}

#[near(serializers = [json])]
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TrustedView {
    pub payee: AccountId,
    pub effective_ms: U64,
}

#[near(serializers = [json])]
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PendingGuardianView {
    pub guardian: Option<AccountId>,
    pub effective_ms: U64,
}

#[near(serializers = [json])]
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UserView {
    pub account_id: AccountId,
    pub registered_ms: U64,
    /// The guardian in charge now (a due pending change is already applied).
    pub guardian: Option<AccountId>,
    pub pending_guardian: Option<PendingGuardianView>,
    pub trusted: Vec<TrustedView>,
    pub active_holds: u32,
}

impl UserView {
    pub fn new(account_id: AccountId, user: &User, now_ms: u64) -> Self {
        UserView {
            account_id,
            registered_ms: U64(user.registered_ms),
            guardian: user.effective_guardian(now_ms).cloned(),
            pending_guardian: user.pending_guardian_at(now_ms).map(|pending| PendingGuardianView {
                guardian: pending.guardian.clone(),
                effective_ms: U64(pending.effective_ms),
            }),
            trusted: user
                .trusted
                .iter()
                .map(|(payee, effective_ms)| TrustedView {
                    payee: payee.clone(),
                    effective_ms: U64(*effective_ms),
                })
                .collect(),
            active_holds: user.active_holds.len() as u32,
        }
    }
}

#[near(serializers = [json])]
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BalanceView {
    pub near: U128,
    pub tokens: BTreeMap<AccountId, U128>,
}
