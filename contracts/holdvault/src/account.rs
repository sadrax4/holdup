//! Registration, deposits, trusted payees and guardians.

use near_contract_standards::fungible_token::receiver::FungibleTokenReceiver;
use near_sdk::json_types::{U128, U64};
use near_sdk::{env, near, serde_json, AccountId, PromiseOrValue};

use crate::asset::Asset;
use crate::errors::{ensure, fail, Error, SafeMath};
use crate::events;
use crate::user::{PendingGuardian, TrustedView, User, UserView, MAX_TRUSTED};
use crate::{is_self_or_parent, HoldVault, HoldVaultExt};

/// `msg` accepted by `ft_on_transfer` to credit an account other than the token sender.
#[near(serializers = [json])]
#[serde(deny_unknown_fields)]
struct TransferMessage {
    credit_to: AccountId,
}

#[near]
impl HoldVault {
    /// Credits the attached NEAR to `account_id`, registering it first if needed.
    ///
    /// Anyone may deposit for a registered account. Registration needs at least
    /// `registration_fee` (kept by the vault as storage reserve) and must come from the account
    /// itself or its parent account. Returns the new NEAR balance.
    #[payable]
    pub fn deposit_near_for(&mut self, account_id: AccountId) -> U128 {
        let attached = env::attached_deposit().as_yoctonear();
        let now_ms = env::block_timestamp_ms();
        let fee = self.config.registration_fee;
        let newly_registered = !self.users.contains_key(&account_id);
        let (mut user, credit) = if newly_registered {
            // Registration turns later payouts to this account into internal credits, so only the
            // account itself or its parent (the onboarding treasury) may opt it in.
            if !is_self_or_parent(&env::predecessor_account_id(), &account_id) {
                fail(
                    Error::NotRegistered,
                    &format!("{account_id} is not registered; only it or its parent account can register it"),
                );
            }
            if attached < fee {
                fail(Error::FeeTooLow, &format!("registration needs at least {fee} yoctoNEAR"));
            }
            (User::new(now_ms, self.config.assets()), attached.safe_sub(fee))
        } else {
            (self.registered_user(&account_id), attached)
        };
        user.credit(&Asset::Near, credit);
        self.near_liabilities = self.near_liabilities.safe_add(credit);
        let balance = user.balance(&Asset::Near);
        self.users.insert(account_id.clone(), user);
        if newly_registered {
            self.assert_solvent();
            events::user_registered(&account_id, now_ms, fee);
        }
        if credit > 0 {
            events::deposit(&account_id, &Asset::Near, credit, balance);
        }
        U128(balance)
    }

    /// Lets payments to `payee` be instant (within the daily cap) once `trust_delay_ms` has passed.
    /// Re-adding a payee keeps its existing entry and does not restart or shorten the delay.
    pub fn add_trusted(&mut self, payee: AccountId) -> TrustedView {
        let owner = env::predecessor_account_id();
        let mut user = self.registered_user(&owner);
        ensure(
            payee != owner && payee != env::current_account_id(),
            Error::SelfPayment,
            "cannot trust yourself or the vault",
        );
        let effective_ms = match user.trusted.get(&payee) {
            Some(effective_ms) => *effective_ms,
            None => {
                ensure(user.trusted.len() < MAX_TRUSTED, Error::TooManyTrusted, "at most 50 trusted payees");
                let effective_ms = env::block_timestamp_ms().safe_add(self.config.trust_delay_ms);
                user.trusted.insert(payee.clone(), effective_ms);
                effective_ms
            }
        };
        self.users.insert(owner.clone(), user);
        self.assert_solvent();
        events::trust_added(&owner, &payee, effective_ms);
        TrustedView { payee, effective_ms: U64(effective_ms) }
    }

    /// Removes a trusted payee immediately. Returns false if it was not trusted.
    pub fn remove_trusted(&mut self, payee: AccountId) -> bool {
        let owner = env::predecessor_account_id();
        let mut user = self.registered_user(&owner);
        if user.trusted.remove(&payee).is_none() {
            return false;
        }
        events::trust_removed(&owner, &payee);
        self.users.insert(owner, user);
        true
    }

    /// Sets (or with `None`, removes) the guardian who may cancel the caller's held payments.
    ///
    /// The first guardian takes effect at once. Any later change waits `guardian_delay_ms`,
    /// the current guardian stays in charge meanwhile, and a newer call replaces the pending one.
    pub fn set_guardian(&mut self, guardian: Option<AccountId>) -> UserView {
        let owner = env::predecessor_account_id();
        let now_ms = env::block_timestamp_ms();
        let mut user = self.registered_user(&owner);
        if let Some(guardian) = &guardian {
            ensure(
                *guardian != owner && *guardian != env::current_account_id(),
                Error::BadGuardian,
                "you cannot be your own guardian",
            );
        }
        user.apply_due_guardian(now_ms);
        let effective_ms = if user.guardian.is_none() && user.pending_guardian.is_none() {
            user.guardian = guardian.clone();
            now_ms
        } else {
            let effective_ms = now_ms.safe_add(self.config.guardian_delay_ms);
            user.pending_guardian = Some(PendingGuardian { guardian: guardian.clone(), effective_ms });
            effective_ms
        };
        let view = UserView::new(owner.clone(), &user, now_ms);
        self.users.insert(owner.clone(), user);
        self.assert_solvent();
        events::guardian_changed(&owner, guardian.as_ref(), effective_ms);
        view
    }
}

#[near]
impl FungibleTokenReceiver for HoldVault {
    /// Credits tokens sent with `ft_transfer_call`. `msg` is empty (credit `sender_id`) or
    /// `{"credit_to":"<account>"}`. Tokens for an account that is not registered are returned.
    fn ft_on_transfer(&mut self, sender_id: AccountId, amount: U128, msg: String) -> PromiseOrValue<U128> {
        let token = env::predecessor_account_id();
        ensure(self.config.is_token(&token), Error::NotToken, "this token is not accepted by the vault");
        let account_id = if msg.is_empty() {
            sender_id
        } else {
            serde_json::from_str::<TransferMessage>(&msg)
                .unwrap_or_else(|_| {
                    fail(Error::BadMessage, "msg must be empty or {\"credit_to\":\"<account>\"}")
                })
                .credit_to
        };
        let Some(mut user) = self.users.get(&account_id).cloned() else {
            // The token's ft_resolve_transfer returns the unused amount to the sender.
            return PromiseOrValue::Value(amount);
        };
        let asset = Asset::Token(token);
        user.credit(&asset, amount.0);
        events::deposit(&account_id, &asset, amount.0, user.balance(&asset));
        self.users.insert(account_id, user);
        PromiseOrValue::Value(U128(0))
    }
}
