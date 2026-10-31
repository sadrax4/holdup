//! Read-only methods.

use near_sdk::json_types::{U128, U64};
use near_sdk::{env, near, AccountId};

use crate::asset::Asset;
use crate::config::ConfigView;
use crate::hold::{HoldView, PayeeStatsView};
use crate::user::{BalanceView, UserView};
use crate::{HoldVault, HoldVaultExt, SolvencyView};

const DEFAULT_LIMIT: u32 = 50;
const MAX_LIMIT: u32 = 100;

#[near]
impl HoldVault {
    pub fn get_config(&self) -> ConfigView {
        self.config.view()
    }

    pub fn get_hold(&self, hold_id: U64) -> Option<HoldView> {
        self.holds.get(&hold_id.0).map(HoldView::from)
    }

    /// Holds sent by `account_id`, newest first (default 50, at most 100).
    pub fn get_holds_for(&self, account_id: AccountId, limit: Option<u32>) -> Vec<HoldView> {
        let Some(user) = self.users.get(&account_id) else {
            return Vec::new();
        };
        let limit = limit.unwrap_or(DEFAULT_LIMIT).min(MAX_LIMIT);
        (0..user.holds_sent)
            .rev()
            .take(limit as usize)
            .filter_map(|n| self.sent_holds.get(&(account_id.clone(), n)))
            .filter_map(|hold_id| self.holds.get(hold_id))
            .map(HoldView::from)
            .collect()
    }

    /// The sender's holds that are still `held`, oldest first.
    pub fn get_active_holds(&self, account_id: AccountId) -> Vec<HoldView> {
        let Some(user) = self.users.get(&account_id) else {
            return Vec::new();
        };
        user.active_holds.iter().filter_map(|hold_id| self.holds.get(hold_id)).map(HoldView::from).collect()
    }

    /// Held payments whose release time has passed, earliest first (default 50, at most 100).
    pub fn get_due_holds(&self, limit: Option<u32>) -> Vec<HoldView> {
        let now_ms = env::block_timestamp_ms();
        let mut due: Vec<HoldView> = self
            .active_holds
            .iter()
            .filter_map(|hold_id| self.holds.get(hold_id))
            .filter(|hold| hold.release_at_ms <= now_ms)
            .map(HoldView::from)
            .collect();
        due.sort_by_key(|hold| (hold.release_at_ms.0, hold.id.0));
        due.truncate(limit.unwrap_or(DEFAULT_LIMIT).min(MAX_LIMIT) as usize);
        due
    }

    /// Vault balances of `account_id`; zeros for unknown accounts.
    pub fn get_balance(&self, account_id: AccountId) -> BalanceView {
        let user = self.users.get(&account_id);
        let balance = |asset: &Asset| U128(user.map_or(0, |user| user.balance(asset)));
        BalanceView {
            near: balance(&Asset::Near),
            tokens: self
                .config
                .tokens
                .iter()
                .map(|token| (token.clone(), balance(&Asset::Token(token.clone()))))
                .collect(),
        }
    }

    pub fn get_user(&self, account_id: AccountId) -> Option<UserView> {
        let now_ms = env::block_timestamp_ms();
        self.users.get(&account_id).map(|user| UserView::new(account_id.clone(), user, now_ms))
    }

    pub fn get_payee_stats(&self, account_id: AccountId) -> PayeeStatsView {
        self.payee_stats.get(&account_id).map(PayeeStatsView::from).unwrap_or_default()
    }

    pub fn is_registered(&self, account_id: AccountId) -> bool {
        self.users.contains_key(&account_id)
    }

    pub fn get_solvency(&self) -> SolvencyView {
        self.solvency()
    }
}
