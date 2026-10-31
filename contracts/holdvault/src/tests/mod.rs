//! Unit tests. `Sim` runs every call the way the chain does: state is read from storage,
//! the method runs, state is written back, and a panicking call leaves storage untouched.
//! The vault's balance and storage usage carry over between calls, so solvency is real.

mod cancelling;
mod config;
mod deposits;
mod extending;
mod fees;
mod guardians;
mod properties;
mod releasing;
mod sending;
mod solvency;
mod trust;

use std::collections::HashMap;
use std::panic::{catch_unwind, AssertUnwindSafe};

use near_sdk::json_types::{U128, U64};
use near_sdk::mock::{with_mocked_blockchain, MockAction, MockedBlockchain, Receipt};
use near_sdk::serde_json::{self, Value};
use near_sdk::test_utils::{get_created_receipts, get_logs, VMContextBuilder};
use near_sdk::{env, test_vm_config, AccountId, NearToken, PromiseOrValue, PromiseResult, RuntimeFeesConfig};

use crate::{
    BalanceView, ConfigInput, HoldStatus, HoldVault, HoldView, PayeeStatsView, SolvencyView, TrustedView,
    UserView,
};
use near_contract_standards::fungible_token::receiver::FungibleTokenReceiver;

pub const NEAR: u128 = 10u128.pow(24);
pub const MILLINEAR: u128 = 10u128.pow(21);
/// One HUSD (6 decimals).
pub const HUSD: u128 = 1_000_000;
pub const MINUTE_MS: u64 = 60_000;
pub const HOUR_MS: u64 = 60 * MINUTE_MS;
pub const DAY_MS: u64 = 24 * HOUR_MS;
/// 2026-01-01T00:00:00Z
pub const START_MS: u64 = 1_767_225_600_000;
pub const HOLD_MS: u64 = 10 * MINUTE_MS;
pub const TRUST_DELAY_MS: u64 = 10 * MINUTE_MS;
pub const GUARDIAN_DELAY_MS: u64 = 10 * MINUTE_MS;
pub const MAX_HOLD_MS: u64 = 72 * HOUR_MS;
pub const REGISTRATION_FEE: u128 = 10 * MILLINEAR;
/// Storage taken by the account and its code before `new` runs.
pub const CODE_STORAGE_BYTES: u64 = 250_000;
/// NEAR the deployer leaves in the vault on top of the code's storage.
pub const DEFAULT_RESERVE: u128 = 5 * NEAR;
/// What every hold costs its sender, and every new trusted payee or guardian change its owner.
pub const HOLD_FEE: u128 = crate::HOLD_STORAGE_FEE.as_yoctonear();
pub const ENTRY_FEE: u128 = crate::ENTRY_STORAGE_FEE.as_yoctonear();

pub fn account(name: &str) -> AccountId {
    name.parse().unwrap()
}
pub fn vault() -> AccountId {
    account("vault.holdup.near")
}
pub fn oracle() -> AccountId {
    account("oracle.holdup.near")
}
pub fn husd() -> AccountId {
    account("husd.holdup.near")
}
pub fn treasury() -> AccountId {
    account("holdup.near")
}
/// Users are sub-accounts of the treasury, as the onboarding flow creates them.
pub fn alice() -> AccountId {
    account("alice.holdup.near")
}
pub fn bob() -> AccountId {
    account("bob.holdup.near")
}
pub fn carol() -> AccountId {
    account("carol.holdup.near")
}
pub fn mallory() -> AccountId {
    account("mallory.near")
}
/// A payee with no vault account: payments to it leave the vault.
pub fn shop() -> AccountId {
    account("shop.near")
}

pub fn config() -> ConfigInput {
    ConfigInput {
        oracles: vec![oracle()],
        tokens: vec![husd()],
        new_payee_hold_ms: U64(HOLD_MS),
        max_hold_ms: U64(MAX_HOLD_MS),
        trust_delay_ms: U64(TRUST_DELAY_MS),
        guardian_delay_ms: U64(GUARDIAN_DELAY_MS),
        instant_caps: HashMap::from([
            ("near".to_owned(), U128(10 * NEAR)),
            (husd().to_string(), U128(100 * HUSD)),
        ]),
        max_active_holds: 10,
        registration_fee: U128(REGISTRATION_FEE),
    }
}

pub fn ok() -> PromiseResult {
    PromiseResult::Successful(vec![])
}

pub fn failed() -> PromiseResult {
    PromiseResult::Failed
}

/// Asserts that a call failed with the given error code.
#[track_caller]
pub fn assert_error<T: std::fmt::Debug>(result: Result<T, String>, code: &str) {
    match result {
        Ok(value) => panic!("expected {code}, got Ok({value:?})"),
        Err(message) => assert!(message.contains(&format!("{code}:")), "expected {code}, got: {message}"),
    }
}

thread_local! {
    /// Building the VM config parses nearcore's runtime parameters, so do it once per thread.
    static VM_CONFIG: near_parameters::vm::Config = test_vm_config();
}

pub struct Sim {
    pub now_ms: u64,
    /// The vault account's balance between calls.
    pub balance: u128,
    pub storage_usage: u64,
    logs: Vec<String>,
    receipts: Vec<Receipt>,
}

impl Sim {
    pub fn new() -> Self {
        Self::try_init(config(), DEFAULT_RESERVE).expect("default config is valid")
    }

    pub fn try_init(config: ConfigInput, reserve: u128) -> Result<Self, String> {
        Self::try_init_as(&vault(), config, reserve)
    }

    pub fn try_init_as(caller: &AccountId, config: ConfigInput, reserve: u128) -> Result<Self, String> {
        let code_cost = u128::from(CODE_STORAGE_BYTES) * env::storage_byte_cost().as_yoctonear();
        let mut sim = Sim {
            now_ms: START_MS,
            balance: code_cost + reserve,
            storage_usage: CODE_STORAGE_BYTES,
            logs: Vec::new(),
            receipts: Vec::new(),
        };
        sim.install(caller, 0, Vec::new(), HashMap::new());
        let outcome = catch_unwind(AssertUnwindSafe(|| {
            let contract = HoldVault::new(config);
            env::state_write(&contract);
        }));
        outcome.map_err(panic_message)?;
        sim.finish();
        Ok(sim)
    }

    fn install(
        &self,
        predecessor: &AccountId,
        deposit: u128,
        promise_results: Vec<PromiseResult>,
        storage: HashMap<Vec<u8>, Vec<u8>>,
    ) {
        let context = VMContextBuilder::new()
            .current_account_id(vault())
            .predecessor_account_id(predecessor.clone())
            .signer_account_id(predecessor.clone())
            .block_timestamp(self.now_ms * 1_000_000)
            // Like nearcore, the VM adds the attached deposit to this balance itself.
            .account_balance(NearToken::from_yoctonear(self.balance))
            .attached_deposit(NearToken::from_yoctonear(deposit))
            .storage_usage(self.storage_usage)
            .build();
        env::set_blockchain_interface(MockedBlockchain::new(
            context,
            VM_CONFIG.with(Clone::clone),
            RuntimeFeesConfig::test(),
            promise_results,
            storage,
            HashMap::new(),
            None,
        ));
    }

    fn finish(&mut self) {
        self.balance = env::account_balance().as_yoctonear();
        self.storage_usage = env::storage_usage();
        self.logs = get_logs();
        self.receipts = get_created_receipts();
    }

    fn run<R>(
        &mut self,
        predecessor: &AccountId,
        deposit: u128,
        promise_results: Vec<PromiseResult>,
        method: impl FnOnce(&mut HoldVault) -> R,
    ) -> Result<R, String> {
        let snapshot = with_mocked_blockchain(|chain| chain.take_storage());
        self.install(predecessor, deposit, promise_results, snapshot.clone());
        let outcome = catch_unwind(AssertUnwindSafe(|| {
            let mut contract: HoldVault = env::state_read().expect("vault is initialized");
            let result = method(&mut contract);
            env::state_write(&contract);
            // Dropping the contract flushes its collections, as at the end of a real call.
            drop(contract);
            result
        }));
        match outcome {
            Ok(result) => {
                self.finish();
                Ok(result)
            }
            Err(panic) => {
                // A failed call changes nothing: restore storage, keep balance and usage.
                self.install(predecessor, 0, Vec::new(), snapshot);
                Err(panic_message(panic))
            }
        }
    }

    /// Calls a contract method as `predecessor` with an attached deposit. Methods must not
    /// return promises; use the wrappers below for those.
    pub fn call<R>(
        &mut self,
        predecessor: &AccountId,
        deposit: u128,
        method: impl FnOnce(&mut HoldVault) -> R,
    ) -> Result<R, String> {
        self.run(predecessor, deposit, Vec::new(), method)
    }

    pub fn view<R>(&self, method: impl FnOnce(&HoldVault) -> R) -> R {
        let storage = with_mocked_blockchain(|chain| chain.take_storage());
        self.install(&vault(), 0, Vec::new(), storage);
        let contract: HoldVault = env::state_read().expect("vault is initialized");
        method(&contract)
    }

    pub fn advance(&mut self, ms: u64) {
        self.now_ms += ms;
    }

    /// Receipts created by the last successful call.
    pub fn receipts(&self) -> &[Receipt] {
        &self.receipts
    }

    /// NEP-297 events logged by the last successful call, as `(name, data)`.
    pub fn events(&self) -> Vec<(String, Value)> {
        self.logs
            .iter()
            .filter_map(|log| log.strip_prefix("EVENT_JSON:"))
            .map(|json| {
                let event: Value = serde_json::from_str(json).expect("event is JSON");
                assert_eq!(event["standard"], "holdup");
                assert_eq!(event["version"], "1.0.0");
                let data = event["data"].as_array().expect("data is an array");
                assert_eq!(data.len(), 1);
                (event["event"].as_str().unwrap().to_owned(), data[0].clone())
            })
            .collect()
    }

    pub fn event_names(&self) -> Vec<String> {
        self.events().into_iter().map(|(name, _)| name).collect()
    }

    pub fn event(&self, name: &str) -> Value {
        self.events()
            .into_iter()
            .find(|(event, _)| event == name)
            .map(|(_, data)| data)
            .unwrap_or_else(|| panic!("no {name} event in {:?}", self.logs))
    }

    // ---------------------------------------------------------------- contract methods

    /// Registers `who` with `near` yoctoNEAR of balance. The treasury onboards its own
    /// sub-accounts; any other account registers itself.
    pub fn register(&mut self, who: &AccountId, near: u128) {
        let payer =
            if who.get_parent_account_id() == Some(treasury().as_ref()) { treasury() } else { who.clone() };
        self.deposit_near(&payer, who, REGISTRATION_FEE + near).expect("registration succeeds");
    }

    pub fn deposit_near(
        &mut self,
        caller: &AccountId,
        who: &AccountId,
        deposit: u128,
    ) -> Result<u128, String> {
        let who = who.clone();
        self.call(caller, deposit, |v| v.deposit_near_for(who).0)
    }

    /// `ft_on_transfer` from `token`; returns the amount handed back to the token.
    pub fn ft_deposit(
        &mut self,
        token: &AccountId,
        sender: &AccountId,
        amount: u128,
        msg: &str,
    ) -> Result<u128, String> {
        let (sender, msg) = (sender.clone(), msg.to_owned());
        self.call(token, 0, |v| match v.ft_on_transfer(sender, U128(amount), msg) {
            PromiseOrValue::Value(unused) => unused.0,
            PromiseOrValue::Promise(_) => unreachable!("ft_on_transfer answers synchronously"),
        })
    }

    pub fn fund_husd(&mut self, who: &AccountId, amount: u128) {
        assert_eq!(self.ft_deposit(&husd(), who, amount, ""), Ok(0));
    }

    pub fn send(
        &mut self,
        sender: &AccountId,
        payee: &AccountId,
        asset: &str,
        amount: u128,
    ) -> Result<HoldView, String> {
        self.send_with_memo(sender, payee, asset, amount, None)
    }

    pub fn send_with_memo(
        &mut self,
        sender: &AccountId,
        payee: &AccountId,
        asset: &str,
        amount: u128,
        memo_hash: Option<String>,
    ) -> Result<HoldView, String> {
        let (payee, asset) = (payee.clone(), asset.to_owned());
        self.call(sender, 0, |v| v.send(payee, asset, U128(amount), memo_hash).hold)
    }

    pub fn cancel(&mut self, caller: &AccountId, hold_id: u64) -> Result<HoldView, String> {
        self.call(caller, 0, |v| v.cancel(U64(hold_id)))
    }

    pub fn guardian_cancel(&mut self, caller: &AccountId, hold_id: u64) -> Result<HoldView, String> {
        self.call(caller, 0, |v| v.guardian_cancel(U64(hold_id)))
    }

    pub fn extend(
        &mut self,
        caller: &AccountId,
        hold_id: u64,
        release_at_ms: u64,
        reason_bits: u32,
        evidence: &str,
    ) -> Result<HoldView, String> {
        let evidence = evidence.to_owned();
        self.call(caller, 0, |v| v.extend_hold(U64(hold_id), U64(release_at_ms), reason_bits, evidence))
    }

    /// Calls `release`. Returns the hold when it settled at once, `None` when a payout promise
    /// was created (see `receipts`).
    pub fn release(&mut self, caller: &AccountId, hold_id: u64) -> Result<Option<HoldView>, String> {
        self.call(caller, 0, |v| match v.release(U64(hold_id)) {
            PromiseOrValue::Value(hold) => Some(hold),
            PromiseOrValue::Promise(payout) => {
                payout.detach();
                None
            }
        })
    }

    /// Runs the payout callback with the given transfer result.
    pub fn on_payout(&mut self, hold_id: u64, result: PromiseResult) -> Result<HoldView, String> {
        self.run(&vault(), 0, vec![result], |v| v.on_payout(U64(hold_id)))
    }

    /// A failed NEAR transfer comes back to the vault as a refund receipt.
    pub fn refund(&mut self, amount: u128) {
        self.balance += amount;
    }

    pub fn add_trusted(&mut self, owner: &AccountId, payee: &AccountId) -> Result<TrustedView, String> {
        let payee = payee.clone();
        self.call(owner, 0, |v| v.add_trusted(payee))
    }

    pub fn remove_trusted(&mut self, owner: &AccountId, payee: &AccountId) -> Result<bool, String> {
        let payee = payee.clone();
        self.call(owner, 0, |v| v.remove_trusted(payee))
    }

    pub fn set_guardian(
        &mut self,
        owner: &AccountId,
        guardian: Option<&AccountId>,
    ) -> Result<UserView, String> {
        let guardian = guardian.cloned();
        self.call(owner, 0, |v| v.set_guardian(guardian))
    }

    pub fn veto(&mut self, guardian: &AccountId, owner: &AccountId) -> Result<UserView, String> {
        let owner = owner.clone();
        self.call(guardian, 0, |v| v.veto_guardian_change(owner))
    }

    // ---------------------------------------------------------------- views

    pub fn hold(&self, hold_id: u64) -> HoldView {
        self.view(|v| v.get_hold(U64(hold_id))).expect("hold exists")
    }

    pub fn status(&self, hold_id: u64) -> HoldStatus {
        self.hold(hold_id).status
    }

    pub fn balances(&self, who: &AccountId) -> BalanceView {
        let who = who.clone();
        self.view(|v| v.get_balance(who))
    }

    pub fn near_of(&self, who: &AccountId) -> u128 {
        self.balances(who).near.0
    }

    pub fn husd_of(&self, who: &AccountId) -> u128 {
        self.balances(who).tokens[&husd()].0
    }

    pub fn user(&self, who: &AccountId) -> Option<UserView> {
        let who = who.clone();
        self.view(|v| v.get_user(who))
    }

    pub fn stats(&self, payee: &AccountId) -> PayeeStatsView {
        let payee = payee.clone();
        self.view(|v| v.get_payee_stats(payee))
    }

    pub fn solvency(&self) -> SolvencyView {
        self.view(|v| v.get_solvency())
    }

    pub fn near_liabilities(&self) -> u128 {
        self.view(|v| v.near_liabilities)
    }

    /// The vault holds enough NEAR for every balance, every open hold and its own storage.
    #[track_caller]
    pub fn assert_solvent(&self) {
        let s = self.solvency();
        let required = s.near_liabilities.0 + s.settlement_reserve.0 + s.storage_cost.0;
        assert!(
            s.account_balance.0 + s.locked.0 >= required,
            "insolvent: balance {} < required {required} ({s:?})",
            s.account_balance.0
        );
    }
}

/// The index the mock assigned to a receipt (used in other receipts' `receipt_indices`).
pub fn receipt_index(receipt: &Receipt) -> u64 {
    match receipt.actions.first() {
        Some(
            MockAction::Transfer { receipt_index, .. } | MockAction::FunctionCallWeight { receipt_index, .. },
        ) => *receipt_index,
        other => panic!("unexpected first action {other:?}"),
    }
}

/// The receiver and method name of every function call in `receipts`.
pub fn function_calls(receipts: &[Receipt]) -> Vec<(AccountId, String)> {
    receipts
        .iter()
        .flat_map(|receipt| {
            receipt.actions.iter().filter_map(move |action| match action {
                MockAction::FunctionCallWeight { method_name, .. } => {
                    Some((receipt.receiver_id.clone(), String::from_utf8(method_name.clone()).unwrap()))
                }
                _ => None,
            })
        })
        .collect()
}

fn panic_message(panic: Box<dyn std::any::Any + Send>) -> String {
    if let Some(message) = panic.downcast_ref::<String>() {
        message.clone()
    } else if let Some(message) = panic.downcast_ref::<&str>() {
        (*message).to_owned()
    } else {
        "non-string panic".to_owned()
    }
}
