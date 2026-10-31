//! Vault configuration. It is fixed forever by `new`: there is no owner and no setter.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use near_sdk::json_types::{U128, U64};
use near_sdk::{near, AccountId};

use crate::asset::{Asset, NEAR_ASSET_ID};
use crate::errors::{ensure, fail, Error};

pub const MAX_ORACLES: usize = 3;
pub const MAX_TOKENS: usize = 4;
/// Upper bound for `max_active_holds`; each sender keeps the ids of their active holds in their record.
pub const MAX_ACTIVE_HOLDS_LIMIT: u32 = 100;
/// Upper bound for every configured duration (one year).
pub const MAX_DURATION_MS: u64 = 365 * 24 * 60 * 60 * 1000;

/// Arguments of `new`.
#[near(serializers = [json])]
#[serde(deny_unknown_fields)]
pub struct ConfigInput {
    /// Accounts allowed to call `extend_hold` (1 to 3).
    pub oracles: Vec<AccountId>,
    /// Allowed NEP-141 token contracts (0 to 4).
    pub tokens: Vec<AccountId>,
    /// Default wait for payments to payees that are not (yet) trusted.
    pub new_payee_hold_ms: U64,
    /// Hard cap: no hold can ever be released later than `created_ms + max_hold_ms`.
    pub max_hold_ms: U64,
    /// Delay before a newly trusted payee can be paid instantly.
    pub trust_delay_ms: U64,
    /// Delay before a guardian change takes effect when a guardian is already in charge.
    pub guardian_delay_ms: U64,
    /// Instant-payment cap per asset id, per sender and UTC day. A missing asset means 0.
    pub instant_caps: HashMap<String, U128>,
    /// Maximum number of held payments per sender.
    pub max_active_holds: u32,
    /// yoctoNEAR kept by the vault as storage reserve when an account registers.
    pub registration_fee: U128,
}

#[near(serializers = [borsh])]
pub struct Config {
    pub oracles: Vec<AccountId>,
    pub tokens: Vec<AccountId>,
    pub new_payee_hold_ms: u64,
    pub max_hold_ms: u64,
    pub trust_delay_ms: u64,
    pub guardian_delay_ms: u64,
    pub instant_caps: BTreeMap<Asset, u128>,
    pub max_active_holds: u32,
    pub registration_fee: u128,
}

#[near(serializers = [json])]
pub struct ConfigView {
    pub oracles: Vec<AccountId>,
    pub tokens: Vec<AccountId>,
    pub new_payee_hold_ms: U64,
    pub max_hold_ms: U64,
    pub trust_delay_ms: U64,
    pub guardian_delay_ms: U64,
    pub instant_caps: BTreeMap<String, U128>,
    pub max_active_holds: u32,
    pub registration_fee: U128,
    /// `HOLD_STORAGE_FEE`: yoctoNEAR a sender pays from their NEAR balance for each hold.
    pub hold_storage_fee: U128,
    /// `ENTRY_STORAGE_FEE`: yoctoNEAR for each new trusted payee and each guardian change.
    pub entry_storage_fee: U128,
    pub version: String,
}

impl Config {
    /// Validates the input of `new`. `vault` is the contract's own account id.
    pub fn from_input(input: ConfigInput, vault: &AccountId) -> Self {
        let bad = |detail: &str| fail(Error::BadConfig, detail);

        ensure(
            (1..=MAX_ORACLES).contains(&input.oracles.len()),
            Error::BadConfig,
            "between 1 and 3 oracles are required",
        );
        ensure(input.tokens.len() <= MAX_TOKENS, Error::BadConfig, "at most 4 tokens are allowed");
        ensure(all_unique(&input.oracles), Error::BadConfig, "duplicate oracle");
        ensure(all_unique(&input.tokens), Error::BadConfig, "duplicate token");
        if input.oracles.contains(vault) {
            bad("the vault cannot be its own oracle");
        }
        for token in &input.tokens {
            if token.as_str() == NEAR_ASSET_ID {
                bad("a token account cannot be named like the native asset id \"near\"");
            }
            if token == vault {
                bad("the vault cannot be one of its tokens");
            }
        }

        let new_payee_hold_ms = input.new_payee_hold_ms.0;
        let max_hold_ms = input.max_hold_ms.0;
        ensure(new_payee_hold_ms > 0, Error::BadConfig, "new_payee_hold_ms must be positive");
        ensure(
            max_hold_ms >= new_payee_hold_ms,
            Error::BadConfig,
            "max_hold_ms must be at least new_payee_hold_ms",
        );
        for duration in [max_hold_ms, input.trust_delay_ms.0, input.guardian_delay_ms.0] {
            ensure(duration <= MAX_DURATION_MS, Error::BadConfig, "durations are limited to one year");
        }
        ensure(
            (1..=MAX_ACTIVE_HOLDS_LIMIT).contains(&input.max_active_holds),
            Error::BadConfig,
            "max_active_holds must be between 1 and 100",
        );

        let mut config = Config {
            oracles: input.oracles,
            tokens: input.tokens,
            new_payee_hold_ms,
            max_hold_ms,
            trust_delay_ms: input.trust_delay_ms.0,
            guardian_delay_ms: input.guardian_delay_ms.0,
            instant_caps: BTreeMap::new(),
            max_active_holds: input.max_active_holds,
            registration_fee: input.registration_fee.0,
        };
        for (asset_id, cap) in input.instant_caps {
            let asset = config
                .asset(&asset_id)
                .unwrap_or_else(|| bad(&format!("instant cap for unknown asset {asset_id}")));
            config.instant_caps.insert(asset, cap.0);
        }
        config
    }

    /// Resolves an asset id: `"near"` or one of the configured tokens.
    pub fn asset(&self, asset_id: &str) -> Option<Asset> {
        if asset_id == NEAR_ASSET_ID {
            return Some(Asset::Near);
        }
        self.tokens.iter().find(|token| token.as_str() == asset_id).cloned().map(Asset::Token)
    }

    /// Native NEAR followed by every configured token.
    pub fn assets(&self) -> impl Iterator<Item = Asset> + '_ {
        std::iter::once(Asset::Near).chain(self.tokens.iter().cloned().map(Asset::Token))
    }

    pub fn instant_cap(&self, asset: &Asset) -> u128 {
        self.instant_caps.get(asset).copied().unwrap_or(0)
    }

    pub fn is_oracle(&self, account_id: &AccountId) -> bool {
        self.oracles.contains(account_id)
    }

    pub fn is_token(&self, account_id: &AccountId) -> bool {
        self.tokens.contains(account_id)
    }

    pub fn view(&self) -> ConfigView {
        ConfigView {
            oracles: self.oracles.clone(),
            tokens: self.tokens.clone(),
            new_payee_hold_ms: U64(self.new_payee_hold_ms),
            max_hold_ms: U64(self.max_hold_ms),
            trust_delay_ms: U64(self.trust_delay_ms),
            guardian_delay_ms: U64(self.guardian_delay_ms),
            instant_caps: self
                .instant_caps
                .iter()
                .map(|(asset, cap)| (asset.id().to_owned(), U128(*cap)))
                .collect(),
            max_active_holds: self.max_active_holds,
            registration_fee: U128(self.registration_fee),
            hold_storage_fee: U128(crate::HOLD_STORAGE_FEE.as_yoctonear()),
            entry_storage_fee: U128(crate::ENTRY_STORAGE_FEE.as_yoctonear()),
            version: crate::VERSION.to_owned(),
        }
    }
}

fn all_unique(accounts: &[AccountId]) -> bool {
    accounts.iter().collect::<BTreeSet<_>>().len() == accounts.len()
}
