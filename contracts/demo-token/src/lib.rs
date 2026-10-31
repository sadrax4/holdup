//! HUSD, the HoldUp Demo Dollar: a plain NEP-141 token with NEP-145 storage management.
//!
//! It stands in for a stablecoin on sandbox and testnet when Circle USDC is not configured.
//! It has no value and is not USDC.

use near_contract_standards::fungible_token::events::FtMint;
use near_contract_standards::fungible_token::metadata::{
    FungibleTokenMetadata, FungibleTokenMetadataProvider, FT_METADATA_SPEC,
};
use near_contract_standards::fungible_token::{FungibleToken, FungibleTokenCore, FungibleTokenResolver};
use near_contract_standards::storage_management::{StorageBalance, StorageBalanceBounds, StorageManagement};
use near_sdk::json_types::U128;
use near_sdk::{
    env, log, near, require, AccountId, BorshStorageKey, NearToken, PanicOnDefault, PromiseOrValue,
};

pub const NAME: &str = "HoldUp Demo Dollar";
pub const SYMBOL: &str = "HUSD";
pub const DECIMALS: u8 = 6;

const ICON: &str = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E\
%3Ccircle cx='16' cy='16' r='16' fill='%230f766e'/%3E\
%3Ctext x='16' y='21' font-size='13' font-family='sans-serif' text-anchor='middle' fill='%23fff'%3EH%3C/text%3E\
%3C/svg%3E";

#[derive(BorshStorageKey)]
#[near]
enum StorageKey {
    Accounts,
}

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct DemoToken {
    owner_id: AccountId,
    token: FungibleToken,
}

#[near]
impl DemoToken {
    /// Creates the token with `total_supply` owned by `owner_id`. Only the token account itself
    /// can initialize it.
    #[init]
    #[private]
    pub fn new(owner_id: AccountId, total_supply: U128) -> Self {
        let mut token = FungibleToken::new(StorageKey::Accounts);
        token.internal_register_account(&owner_id);
        token.internal_deposit(&owner_id, total_supply.0);
        FtMint { owner_id: &owner_id, amount: total_supply, memo: Some("initial supply") }.emit();
        DemoToken { owner_id, token }
    }

    /// Mints `amount` to `account_id`, registering the account if needed. Owner only.
    pub fn mint(&mut self, account_id: AccountId, amount: U128) {
        require!(env::predecessor_account_id() == self.owner_id, "only the owner can mint");
        if !self.token.accounts.contains_key(&account_id) {
            self.token.internal_register_account(&account_id);
        }
        self.token.internal_deposit(&account_id, amount.0);
        FtMint { owner_id: &account_id, amount, memo: None }.emit();
    }

    pub fn owner(&self) -> AccountId {
        self.owner_id.clone()
    }
}

#[near]
impl FungibleTokenCore for DemoToken {
    #[payable]
    fn ft_transfer(&mut self, receiver_id: AccountId, amount: U128, memo: Option<String>) {
        self.token.ft_transfer(receiver_id, amount, memo)
    }

    #[payable]
    fn ft_transfer_call(
        &mut self,
        receiver_id: AccountId,
        amount: U128,
        memo: Option<String>,
        msg: String,
    ) -> PromiseOrValue<U128> {
        self.token.ft_transfer_call(receiver_id, amount, memo, msg)
    }

    fn ft_total_supply(&self) -> U128 {
        self.token.ft_total_supply()
    }

    fn ft_balance_of(&self, account_id: AccountId) -> U128 {
        self.token.ft_balance_of(account_id)
    }
}

#[near]
impl FungibleTokenResolver for DemoToken {
    #[private]
    fn ft_resolve_transfer(&mut self, sender_id: AccountId, receiver_id: AccountId, amount: U128) -> U128 {
        let (used_amount, burned_amount) =
            self.token.internal_ft_resolve_transfer(&sender_id, receiver_id, amount);
        if burned_amount > 0 {
            log!("Account @{} burned {}", sender_id, burned_amount);
        }
        used_amount.into()
    }
}

#[near]
impl StorageManagement for DemoToken {
    #[payable]
    fn storage_deposit(
        &mut self,
        account_id: Option<AccountId>,
        registration_only: Option<bool>,
    ) -> StorageBalance {
        self.token.storage_deposit(account_id, registration_only)
    }

    #[payable]
    fn storage_withdraw(&mut self, amount: Option<NearToken>) -> StorageBalance {
        self.token.storage_withdraw(amount)
    }

    #[payable]
    fn storage_unregister(&mut self, force: Option<bool>) -> bool {
        match self.token.internal_storage_unregister(force) {
            Some((account_id, balance)) => {
                if balance > 0 {
                    log!("Closed @{} with {}", account_id, balance);
                }
                true
            }
            None => false,
        }
    }

    fn storage_balance_bounds(&self) -> StorageBalanceBounds {
        self.token.storage_balance_bounds()
    }

    fn storage_balance_of(&self, account_id: AccountId) -> Option<StorageBalance> {
        self.token.storage_balance_of(account_id)
    }
}

#[near]
impl FungibleTokenMetadataProvider for DemoToken {
    fn ft_metadata(&self) -> FungibleTokenMetadata {
        FungibleTokenMetadata {
            spec: FT_METADATA_SPEC.to_string(),
            name: NAME.to_string(),
            symbol: SYMBOL.to_string(),
            icon: Some(ICON.to_string()),
            reference: None,
            reference_hash: None,
            decimals: DECIMALS,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use near_sdk::test_utils::{accounts, get_logs, VMContextBuilder};
    use near_sdk::testing_env;

    const SUPPLY: u128 = 1_000_000 * 10u128.pow(DECIMALS as u32);

    fn context(predecessor: AccountId, deposit: NearToken) -> VMContextBuilder {
        let mut builder = VMContextBuilder::new();
        builder
            .current_account_id("husd.test.near".parse().unwrap())
            .predecessor_account_id(predecessor.clone())
            .signer_account_id(predecessor)
            .attached_deposit(deposit);
        builder
    }

    fn setup() -> DemoToken {
        testing_env!(context(accounts(0), NearToken::from_near(0)).build());
        DemoToken::new(accounts(0), U128(SUPPLY))
    }

    #[test]
    fn new_mints_the_supply_to_the_owner() {
        let token = setup();
        assert_eq!(token.ft_total_supply().0, SUPPLY);
        assert_eq!(token.ft_balance_of(accounts(0)).0, SUPPLY);
        assert_eq!(token.owner(), accounts(0));
        assert!(get_logs().iter().any(|log| log.contains("\"ft_mint\"")));
    }

    #[test]
    fn metadata_names_the_demo_token() {
        let token = setup();
        let metadata = token.ft_metadata();
        metadata.assert_valid();
        assert_eq!(metadata.name, "HoldUp Demo Dollar");
        assert_eq!(metadata.symbol, "HUSD");
        assert_eq!(metadata.decimals, 6);
    }

    #[test]
    fn owner_mints_and_registers_new_accounts() {
        let mut token = setup();
        token.mint(accounts(1), U128(5_000_000));
        assert_eq!(token.ft_balance_of(accounts(1)).0, 5_000_000);
        assert_eq!(token.ft_total_supply().0, SUPPLY + 5_000_000);
        assert!(token.storage_balance_of(accounts(1)).is_some());
    }

    #[test]
    #[should_panic(expected = "only the owner can mint")]
    fn others_cannot_mint() {
        let mut token = setup();
        testing_env!(context(accounts(1), NearToken::from_near(0)).build());
        token.mint(accounts(1), U128(1));
    }

    #[test]
    fn transfer_between_registered_accounts() {
        let mut token = setup();
        testing_env!(context(accounts(2), token.storage_balance_bounds().min).build());
        token.storage_deposit(None, Some(true));
        testing_env!(context(accounts(0), NearToken::from_yoctonear(1)).build());
        token.ft_transfer(accounts(2), U128(1_000), None);
        assert_eq!(token.ft_balance_of(accounts(2)).0, 1_000);
        assert_eq!(token.ft_balance_of(accounts(0)).0, SUPPLY - 1_000);
    }
}
