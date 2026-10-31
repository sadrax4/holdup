//! Holds: create, extend, cancel, release and settle payouts.

use near_contract_standards::fungible_token::core::ext_ft_core;
use near_contract_standards::storage_management::ext_storage_management;
use near_sdk::json_types::{U128, U64};
use near_sdk::{env, near, AccountId, Gas, NearToken, Promise, PromiseError, PromiseOrValue};

use crate::asset::Asset;
use crate::errors::{ensure, fail, Error, SafeMath};
use crate::events;
use crate::hold::{Hold, HoldStatus, HoldView, SendView};
use crate::user::DAY_MS;
use crate::{
    hold_storage_reserve, payout_deposit, settlement_reserve_for, HoldVault, HoldVaultExt,
    FT_STORAGE_DEPOSIT, HOLD_STORAGE_FEE,
};

/// Maximum size of the evidence JSON passed to `extend_hold`.
pub const MAX_EVIDENCE_BYTES: usize = 4096;
/// Maximum length of `memo_hash` in `send`, in bytes.
pub const MAX_MEMO_HASH_LEN: usize = 64;

/// Gas for NEP-145 `storage_deposit` on the token before a token payout.
pub const GAS_FOR_STORAGE_DEPOSIT: Gas = Gas::from_tgas(10);
/// Gas for NEP-141 `ft_transfer` to the payee.
pub const GAS_FOR_FT_TRANSFER: Gas = Gas::from_tgas(15);
/// Gas reserved for `on_payout`. It also receives any gas left over from the call.
pub const GAS_FOR_ON_PAYOUT: Gas = Gas::from_tgas(20);

#[near]
impl HoldVault {
    /// Moves `amount` from the caller's vault balance into a new hold for `payee`, and
    /// `HOLD_STORAGE_FEE` from their NEAR balance to the vault for the hold's storage.
    ///
    /// A trusted payee (past its trust delay) is paid at once while the sender's instant usage
    /// for this asset stays within today's cap; any other payment waits `new_payee_hold_ms`.
    pub fn send(
        &mut self,
        payee: AccountId,
        asset: String,
        amount: U128,
        memo_hash: Option<String>,
    ) -> SendView {
        let sender = env::predecessor_account_id();
        let now_ms = env::block_timestamp_ms();
        let amount = amount.0;
        let mut user = self.registered_user(&sender);
        ensure(amount > 0, Error::ZeroAmount, "amount must be positive");
        let asset = self
            .config
            .asset(&asset)
            .unwrap_or_else(|| fail(Error::UnknownAsset, &format!("{asset} is not accepted by this vault")));
        ensure(
            payee != sender && payee != env::current_account_id(),
            Error::SelfPayment,
            "cannot pay yourself or the vault",
        );
        let fee = HOLD_STORAGE_FEE.as_yoctonear();
        let near_needed = if asset == Asset::Near { amount.checked_add(fee) } else { Some(fee) };
        ensure(
            user.balance(&asset) >= amount
                && near_needed.is_some_and(|needed| user.balance(&Asset::Near) >= needed),
            Error::InsufficientBalance,
            "balance is lower than the amount plus the hold storage fee in NEAR",
        );
        ensure(
            user.active_holds.len() < self.config.max_active_holds as usize,
            Error::TooManyHolds,
            "too many held payments; cancel one or wait for a release",
        );
        if let Some(memo_hash) = &memo_hash {
            ensure(
                memo_hash.len() <= MAX_MEMO_HASH_LEN,
                Error::MemoTooLong,
                "memo_hash is limited to 64 bytes",
            );
        }

        let day = now_ms / DAY_MS;
        let cap = self.config.instant_cap(&asset);
        let instant = user.trusts_at(&payee, now_ms)
            && user.instant_used(&asset, day).checked_add(amount).is_some_and(|total| total <= cap);

        let id = self.next_hold_id;
        self.next_hold_id = id.safe_add(1);
        let hold = Hold {
            id,
            sender: sender.clone(),
            payee: payee.clone(),
            asset: asset.clone(),
            amount,
            created_ms: now_ms,
            release_at_ms: if instant { now_ms } else { now_ms.safe_add(self.config.new_payee_hold_ms) },
            status: HoldStatus::Held,
            instant,
            flagged: false,
            extensions: 0,
            reason_bits: 0,
            evidence_hash: None,
            memo_hash,
            settled_ms: None,
            cancelled_by: None,
        };

        // NEAR moves from the sender's balance into the hold: both count as liabilities.
        user.debit(&asset, amount);
        self.take_storage_fee(&mut user, HOLD_STORAGE_FEE, "a hold");
        self.sent_holds.insert((sender.clone(), user.holds_sent), id);
        user.holds_sent = user.holds_sent.safe_add(1);
        if instant {
            user.record_instant(&asset, day, amount);
        } else {
            user.active_holds.push(id);
            self.active_holds.insert(id);
            let stats = self.payee_stats_mut(&payee);
            stats.holds = stats.holds.safe_add(1);
        }
        self.users.insert(sender, user);
        self.settlement_reserve = self.settlement_reserve.safe_add(settlement_reserve_for(&asset));
        self.holds.insert(id, hold.clone());
        self.assert_solvent();
        events::hold_created(&hold, fee);

        if instant {
            if let Some(payout) = self.pay_out(id) {
                payout.detach();
            }
        }
        SendView { hold: self.hold_view(id) }
    }

    /// Cancels a held payment and returns the money to the sender's vault balance.
    pub fn cancel(&mut self, hold_id: U64) -> HoldView {
        let caller = env::predecessor_account_id();
        let hold = self.hold(hold_id.0);
        ensure(caller == hold.sender, Error::NotSender, "only the sender can cancel this hold");
        self.cancel_hold(hold, caller, "sender")
    }

    /// Same as `cancel`, for the guardian in charge of the sender's account.
    pub fn guardian_cancel(&mut self, hold_id: U64) -> HoldView {
        let caller = env::predecessor_account_id();
        let hold = self.hold(hold_id.0);
        let now_ms = env::block_timestamp_ms();
        let guardian = self.users.get(&hold.sender).and_then(|sender| sender.effective_guardian(now_ms));
        ensure(
            guardian == Some(&caller),
            Error::NotGuardian,
            "only the sender's guardian can cancel this hold",
        );
        self.cancel_hold(hold, caller, "guardian")
    }

    /// Makes a hold longer. Oracles cannot shorten, release, cancel or redirect anything.
    ///
    /// `evidence` is the canonical evidence JSON; only its sha256 is stored, the text itself
    /// stays in the transaction arguments for anyone to re-hash.
    pub fn extend_hold(
        &mut self,
        hold_id: U64,
        release_at_ms: U64,
        reason_bits: u32,
        evidence: String,
    ) -> HoldView {
        ensure(
            self.config.is_oracle(&env::predecessor_account_id()),
            Error::NotOracle,
            "only an oracle can extend a hold",
        );
        let mut hold = self.hold(hold_id.0);
        ensure(hold.status == HoldStatus::Held, Error::HoldNotActive, "hold is not held");
        let release_at_ms = release_at_ms.0;
        ensure(release_at_ms > hold.release_at_ms, Error::OnlyLonger, "a hold can only be made longer");
        let latest_release_ms = hold.created_ms.safe_add(self.config.max_hold_ms);
        ensure(
            release_at_ms <= latest_release_ms,
            Error::AboveMaxHold,
            "release_at_ms is past created_ms + max_hold_ms",
        );
        ensure(
            evidence.len() <= MAX_EVIDENCE_BYTES,
            Error::EvidenceTooLong,
            "evidence is limited to 4096 bytes",
        );
        ensure(reason_bits != 0, Error::NoReason, "at least one reason bit is required");

        let previous_release_at_ms = hold.release_at_ms;
        hold.release_at_ms = release_at_ms;
        hold.reason_bits |= reason_bits;
        hold.extensions = hold.extensions.safe_add(1);
        hold.evidence_hash = Some(env::sha256_array(evidence.as_bytes()));
        if !hold.flagged {
            hold.flagged = true;
            let stats = self.payee_stats_mut(&hold.payee);
            stats.flagged = stats.flagged.safe_add(1);
            stats.last_flag_ms = Some(env::block_timestamp_ms());
        }
        events::hold_extended(&hold, previous_release_at_ms);
        let view = HoldView::from(&hold);
        self.holds.insert(hold.id, hold);
        view
    }

    /// Pays a due hold to its payee. Anyone can call it; nobody can release a hold early.
    ///
    /// Attach about 100 Tgas: token payouts chain `storage_deposit`, `ft_transfer` and `on_payout`.
    pub fn release(&mut self, hold_id: U64) -> PromiseOrValue<HoldView> {
        let hold = self.hold(hold_id.0);
        ensure(hold.status == HoldStatus::Held, Error::HoldNotActive, "hold is not held");
        if env::block_timestamp_ms() < hold.release_at_ms {
            fail(Error::NotDue, &format!("hold {} is held until {}", hold.id, hold.release_at_ms));
        }
        self.close_active(&hold);
        match self.pay_out(hold.id) {
            Some(payout) => PromiseOrValue::Promise(payout),
            None => PromiseOrValue::Value(self.hold_view(hold.id)),
        }
    }

    /// Settles an external payout once its transfer has finished.
    ///
    /// Private: only the vault's own payout promise can call it. It never asks for new funds,
    /// so a payout can always settle.
    #[private]
    pub fn on_payout(&mut self, hold_id: U64) -> HoldView {
        let mut hold = self.hold(hold_id.0);
        ensure(hold.status == HoldStatus::Releasing, Error::HoldNotActive, "hold is not being paid out");
        hold.settled_ms = Some(env::block_timestamp_ms());
        self.unreserve(hold_storage_reserve());
        if payout_succeeded() {
            hold.status = HoldStatus::Released;
            if !hold.instant {
                let stats = self.payee_stats_mut(&hold.payee);
                stats.released = stats.released.safe_add(1);
            }
            events::hold_released(&hold, false);
        } else {
            let mut sender = self.registered_user(&hold.sender);
            sender.credit(&hold.asset, hold.amount);
            self.users.insert(hold.sender.clone(), sender);
            if hold.asset == Asset::Near {
                // The failed transfer's deposit comes back to the vault as a refund receipt.
                self.near_liabilities = self.near_liabilities.safe_add(hold.amount);
            }
            hold.status = HoldStatus::Failed;
            events::hold_failed(&hold);
        }
        let view = HoldView::from(&hold);
        self.holds.insert(hold.id, hold);
        view
    }
}

impl HoldVault {
    pub(crate) fn hold_view(&self, hold_id: u64) -> HoldView {
        HoldView::from(&self.hold(hold_id))
    }

    /// Takes a held payment out of the active sets of the vault and of its sender.
    fn close_active(&mut self, hold: &Hold) {
        self.active_holds.remove(&hold.id);
        let mut sender = self.registered_user(&hold.sender);
        sender.active_holds.retain(|id| *id != hold.id);
        self.users.insert(hold.sender.clone(), sender);
    }

    fn cancel_hold(&mut self, mut hold: Hold, caller: AccountId, by: &str) -> HoldView {
        ensure(hold.status == HoldStatus::Held, Error::HoldNotActive, "hold is not held");
        self.close_active(&hold);
        // NEAR moves from the hold back to the sender's balance: liabilities are unchanged.
        let mut sender = self.registered_user(&hold.sender);
        sender.credit(&hold.asset, hold.amount);
        self.users.insert(hold.sender.clone(), sender);

        let now_ms = env::block_timestamp_ms();
        hold.status = HoldStatus::Cancelled;
        hold.cancelled_by = Some(caller);
        hold.settled_ms = Some(now_ms);
        self.unreserve(settlement_reserve_for(&hold.asset));
        // A cancel after a warning is a report against the payee, but each sender counts once
        // per payee: cancelling more flagged payments to it adds nothing.
        let report = hold.flagged && self.reporters.insert((hold.payee.clone(), hold.sender.clone()));
        let stats = self.payee_stats_mut(&hold.payee);
        stats.cancelled = stats.cancelled.safe_add(1);
        if report {
            stats.cancelled_after_flag = stats.cancelled_after_flag.safe_add(1);
            stats.last_report_ms = Some(now_ms);
        }
        events::hold_cancelled(&hold, by, report);
        let view = HoldView::from(&hold);
        self.holds.insert(hold.id, hold);
        view
    }

    /// Pays a hold that has just left the `held` state. Returns the payout promise when the
    /// money has to leave the vault.
    ///
    /// A payee that is a registered vault user is credited internally: no promise can fail and
    /// nothing gets stuck in an account that only has function-call keys. Otherwise the hold is
    /// marked `releasing` before any promise exists, so it cannot be paid or cancelled twice.
    fn pay_out(&mut self, hold_id: u64) -> Option<Promise> {
        let mut hold = self.hold(hold_id);
        if let Some(mut payee) = self.users.get(&hold.payee).cloned() {
            payee.credit(&hold.asset, hold.amount);
            self.users.insert(hold.payee.clone(), payee);
            hold.status = HoldStatus::Released;
            hold.settled_ms = Some(env::block_timestamp_ms());
            self.unreserve(settlement_reserve_for(&hold.asset));
            if !hold.instant {
                let stats = self.payee_stats_mut(&hold.payee);
                stats.released = stats.released.safe_add(1);
            }
            events::hold_released(&hold, true);
            self.holds.insert(hold_id, hold);
            return None;
        }

        hold.status = HoldStatus::Releasing;
        let transfer = match &hold.asset {
            Asset::Near => {
                // The amount leaves the account balance together with the liability.
                self.near_liabilities = self.near_liabilities.safe_sub(hold.amount);
                Promise::new(hold.payee.clone()).transfer(NearToken::from_yoctonear(hold.amount))
            }
            Asset::Token(token) => {
                // The attached deposits were set aside when the hold was created.
                self.unreserve(payout_deposit(&hold.asset));
                ext_storage_management::ext(token.clone())
                    .with_attached_deposit(FT_STORAGE_DEPOSIT)
                    .with_static_gas(GAS_FOR_STORAGE_DEPOSIT)
                    .with_unused_gas_weight(0)
                    .storage_deposit(Some(hold.payee.clone()), Some(true))
                    .then(
                        ext_ft_core::ext(token.clone())
                            .with_attached_deposit(NearToken::from_yoctonear(1))
                            .with_static_gas(GAS_FOR_FT_TRANSFER)
                            .with_unused_gas_weight(0)
                            .ft_transfer(
                                hold.payee.clone(),
                                U128(hold.amount),
                                Some(format!("HoldUp hold {hold_id}")),
                            ),
                    )
            }
        };
        self.holds.insert(hold_id, hold);
        // `on_payout` is chained on the transfer alone (for tokens: on `ft_transfer`, which itself
        // waits for `storage_deposit`), so promise result 0 is the transfer's outcome.
        Some(transfer.then(
            Self::ext(env::current_account_id()).with_static_gas(GAS_FOR_ON_PAYOUT).on_payout(U64(hold_id)),
        ))
    }
}

/// Outcome of the transfer `on_payout` is chained on. Only success or failure matters, so no
/// returned bytes are read (`TooLong` still means the transfer succeeded).
fn payout_succeeded() -> bool {
    !matches!(env::promise_result_checked(0, 0), Err(PromiseError::Failed))
}
