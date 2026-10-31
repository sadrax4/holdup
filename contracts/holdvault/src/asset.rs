use near_sdk::{near, AccountId};

/// Asset id used in method arguments and views for native NEAR.
pub const NEAR_ASSET_ID: &str = "near";

/// An asset the vault can hold: native NEAR or an allowed NEP-141 token.
#[near(serializers = [borsh])]
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Asset {
    Near,
    Token(AccountId),
}

impl Asset {
    /// The asset id string: `"near"` or the token contract account id.
    pub fn id(&self) -> &str {
        match self {
            Asset::Near => NEAR_ASSET_ID,
            Asset::Token(token) => token.as_str(),
        }
    }
}
