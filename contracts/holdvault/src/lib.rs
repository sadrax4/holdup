//! HoldVault: a keyless payments vault for HoldUp.
//!
//! Payments to payees the sender has not trusted yet wait in a *hold*. Oracles can make a hold
//! longer (never shorter, never past `created_ms + max_hold_ms`), only the sender or their
//! guardian can cancel it, and anyone can release it once it is due. Money only ever leaves a
//! hold towards the payee fixed at `send`, or goes back to the sender's vault balance.
//! There is no owner, no upgrade method and no configuration setter.

mod account;
mod asset;
mod config;
mod errors;
mod events;
mod hold;
mod holds;
mod user;
mod views;

#[cfg(test)]
mod tests;

use near_sdk::json_types::{U128, U64};
use near_sdk::store::{IterableSet, LookupMap};
use near_sdk::{env, near, AccountId, BorshStorageKey, NearToken, PanicOnDefault};

pub use crate::asset::{Asset, NEAR_ASSET_ID};
pub use crate::config::{ConfigInput, ConfigView};
pub use crate::errors::Error;
pub use crate::hold::{HoldStatus, HoldView, PayeeStatsView, SendView};
pub use crate::holds::{GAS_FOR_FT_TRANSFER, GAS_FOR_ON_PAYOUT, GAS_FOR_STORAGE_DEPOSIT};
pub use crate::user::{BalanceView, PendingGuardianView, TrustedView, UserView};

use crate::config::Config;
use crate::errors::{ensure, fail, SafeMath};
use crate::hold::{Hold, PayeeStats};
use crate::user::User;

/// Contract version reported by `get_config`.
pub const VERSION: &str = "1.0.0";

/// NEAR attached to `storage_deposit` when paying a token to an account outside the vault
/// (the NEP-145 minimum of standard tokens; the token refunds it if the payee is registered).
pub const FT_STORAGE_DEPOSIT: NearToken = NearToken::from_yoctonear(1_250_000_000_000_000_000_000);

/// Upper bound on the storage a hold can still add after `send`: `evidence_hash` (32 bytes),
/// `settled_ms` (8), `cancelled_by` (4 + 64) and the payee's `last_flag_ms` (8).
pub const HOLD_SETTLEMENT_BYTES: u64 = 32 + 8 + 4 + 64 + 8;

#[derive(BorshStorageKey)]
#[near]
enum StorageKey {
    Users,
    Holds,
    SentHolds,
    ActiveHolds,
    PayeeStats,
}

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct HoldVault {
    config: Config,
    users: LookupMap<AccountId, User>,
    holds: LookupMap<u64, Hold>,
    /// `(sender, n)` -> id of the n-th hold the sender created.
    sent_holds: LookupMap<(AccountId, u32), u64>,
    /// Ids of every hold with status `held`.
    active_holds: IterableSet<u64>,
    payee_stats: LookupMap<AccountId, PayeeStats>,
    next_hold_id: u64,
    /// yoctoNEAR owed to users: their NEAR balances plus NEAR in held holds.
    near_liabilities: u128,
    /// yoctoNEAR set aside so every open hold can settle without new funds: its remaining storage
    /// growth and, for token holds, the deposits a payout attaches.
    settlement_reserve: u128,
}

/// The vault's NEAR accounting. Solvent means `account_balance + locked` covers
/// `near_liabilities + settlement_reserve + storage_cost`.
#[near(serializers = [json])]
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SolvencyView {
    pub account_balance: U128,
    pub locked: U128,
    pub near_liabilities: U128,
    pub settlement_reserve: U128,
    pub storage_usage: U64,
    pub storage_cost: U128,
    /// What is left after all of the above (0 when insolvent). Top it up with a plain transfer.
    pub surplus: U128,
}

#[near]
impl HoldVault {
    /// Initializes the vault. Callable once, by the vault account itself or by its parent account
    /// (the deployer, usually batching `DeployContract` and `new` in one transaction), so a
    /// stranger cannot initialize a freshly deployed vault with their own oracles.
    #[init]
    pub fn new(config: ConfigInput) -> Self {
        let vault = env::current_account_id();
        ensure(
            is_self_or_parent(&env::predecessor_account_id(), &vault),
            Error::BadConfig,
            "only the vault account or its parent account can initialize it",
        );
        HoldVault {
            config: Config::from_input(config, &vault),
            users: LookupMap::new(StorageKey::Users),
            holds: LookupMap::new(StorageKey::Holds),
            sent_holds: LookupMap::new(StorageKey::SentHolds),
            active_holds: IterableSet::new(StorageKey::ActiveHolds),
            payee_stats: LookupMap::new(StorageKey::PayeeStats),
            next_hold_id: 0,
            near_liabilities: 0,
            settlement_reserve: 0,
        }
    }
}

impl HoldVault {
    fn registered_user(&self, account_id: &AccountId) -> User {
        self.users
            .get(account_id)
            .cloned()
            .unwrap_or_else(|| fail(Error::NotRegistered, &format!("{account_id} is not registered")))
    }

    fn hold(&self, hold_id: u64) -> Hold {
        self.holds
            .get(&hold_id)
            .cloned()
            .unwrap_or_else(|| fail(Error::HoldNotFound, &format!("hold {hold_id} does not exist")))
    }

    fn payee_stats_mut(&mut self, payee: &AccountId) -> &mut PayeeStats {
        self.payee_stats.entry(payee.clone()).or_default()
    }

    fn unreserve(&mut self, amount: u128) {
        self.settlement_reserve = self.settlement_reserve.safe_sub(amount);
    }

    /// Writes cached collection entries so `env::storage_usage()` reflects this call's changes.
    fn flush(&mut self) {
        self.users.flush();
        self.holds.flush();
        self.sent_holds.flush();
        self.active_holds.flush();
        self.payee_stats.flush();
    }

    fn solvency(&self) -> SolvencyView {
        let account_balance = env::account_balance().as_yoctonear();
        let locked = env::account_locked_balance().as_yoctonear();
        let storage_usage = env::storage_usage();
        let storage_cost = u128::from(storage_usage).safe_mul(env::storage_byte_cost().as_yoctonear());
        let required = self.near_liabilities.safe_add(self.settlement_reserve).safe_add(storage_cost);
        let available = account_balance.safe_add(locked);
        SolvencyView {
            account_balance: U128(account_balance),
            locked: U128(locked),
            near_liabilities: U128(self.near_liabilities),
            settlement_reserve: U128(self.settlement_reserve),
            storage_usage: U64(storage_usage),
            storage_cost: U128(storage_cost),
            surplus: U128(available.saturating_sub(required)),
        }
    }

    /// Called at the end of every method that can add storage or new obligations
    /// (registration, `send`, trust and guardian changes). Methods that only move existing
    /// money (cancel, extend, release, payout callbacks) never need new funds: their storage
    /// is covered by `settlement_reserve`, so they cannot be blocked by a low reserve.
    fn assert_solvent(&mut self) {
        self.flush();
        let solvency = self.solvency();
        let available = solvency.account_balance.0.safe_add(solvency.locked.0);
        let required = solvency
            .near_liabilities
            .0
            .safe_add(solvency.settlement_reserve.0)
            .safe_add(solvency.storage_cost.0);
        if available < required {
            fail(
                Error::Insolvent,
                &format!(
                    "vault needs {required} yoctoNEAR to cover balances and storage but has {available}"
                ),
            );
        }
    }
}

/// Whether `caller` is `account` itself or the account that created it (its parent).
fn is_self_or_parent(caller: &AccountId, account: &AccountId) -> bool {
    caller == account || account.get_parent_account_id() == Some(caller.as_ref())
}

/// NEAR reserved for a new hold until it settles.
fn settlement_reserve_for(asset: &Asset) -> u128 {
    hold_storage_reserve().safe_add(payout_deposit(asset))
}

fn hold_storage_reserve() -> u128 {
    u128::from(HOLD_SETTLEMENT_BYTES).safe_mul(env::storage_byte_cost().as_yoctonear())
}

/// NEAR an external payout of `asset` attaches besides the amount itself.
fn payout_deposit(asset: &Asset) -> u128 {
    match asset {
        Asset::Near => 0,
        Asset::Token(_) => FT_STORAGE_DEPOSIT.as_yoctonear().safe_add(1),
    }
}
