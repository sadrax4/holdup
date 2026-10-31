use super::*;

#[test]
fn first_deposit_registers_and_keeps_the_fee() {
    let mut sim = Sim::new();
    let balance = sim.deposit_near(&treasury(), &alice(), REGISTRATION_FEE + 2 * NEAR).unwrap();
    assert_eq!(balance, 2 * NEAR);
    assert_eq!(sim.event_names(), ["user_registered", "deposit"]);
    let registered = sim.event("user_registered");
    assert_eq!(registered["account_id"], "alice.holdup.near");
    assert_eq!(registered["registered_ms"], START_MS.to_string());
    assert_eq!(registered["fee"], REGISTRATION_FEE.to_string());
    let deposit = sim.event("deposit");
    assert_eq!(deposit["asset"], "near");
    assert_eq!(deposit["amount"], (2 * NEAR).to_string());
    assert_eq!(deposit["balance"], (2 * NEAR).to_string());

    assert!(sim.view(|v| v.is_registered(alice())));
    let user = sim.user(&alice()).unwrap();
    assert_eq!(user.registered_ms, U64(START_MS));
    assert_eq!(user.guardian, None);
    assert!(user.trusted.is_empty());
    // The fee stays with the vault as storage reserve; only the rest is owed to alice.
    assert_eq!(sim.near_liabilities(), 2 * NEAR);
    sim.assert_solvent();
}

#[test]
fn registration_needs_the_fee() {
    let mut sim = Sim::new();
    assert_error(sim.deposit_near(&treasury(), &alice(), REGISTRATION_FEE - 1), "E_FEE_TOO_LOW");
    assert!(!sim.view(|v| v.is_registered(alice())));
    assert_eq!(sim.deposit_near(&treasury(), &alice(), REGISTRATION_FEE), Ok(0));
    assert_eq!(sim.event_names(), ["user_registered"]);
}

#[test]
fn only_the_account_or_its_parent_can_register_it() {
    let mut sim = Sim::new();
    // Registering someone else's account would divert payouts to it into the vault.
    assert_error(sim.deposit_near(&mallory(), &shop(), NEAR), "E_NOT_REGISTERED");
    assert_error(sim.deposit_near(&mallory(), &alice(), NEAR), "E_NOT_REGISTERED");
    assert_error(sim.deposit_near(&treasury(), &account("sub.alice.holdup.near"), NEAR), "E_NOT_REGISTERED");
    assert!(!sim.view(|v| v.is_registered(shop())));

    assert_eq!(sim.deposit_near(&shop(), &shop(), REGISTRATION_FEE + NEAR), Ok(NEAR));
    assert_eq!(sim.deposit_near(&treasury(), &alice(), REGISTRATION_FEE), Ok(0));
    assert_eq!(sim.deposit_near(&alice(), &account("sub.alice.holdup.near"), REGISTRATION_FEE), Ok(0));
}

#[test]
fn later_deposits_credit_everything_and_anyone_can_pay() {
    let mut sim = Sim::new();
    sim.register(&alice(), NEAR);
    assert_eq!(sim.deposit_near(&alice(), &alice(), 3 * NEAR), Ok(4 * NEAR));
    assert_eq!(sim.deposit_near(&mallory(), &alice(), NEAR), Ok(5 * NEAR));
    assert_eq!(sim.event_names(), ["deposit"]);
    assert_eq!(sim.near_liabilities(), 5 * NEAR);
    sim.assert_solvent();
}

#[test]
fn unknown_accounts_read_as_empty() {
    let sim = Sim::new();
    let balances = sim.balances(&mallory());
    assert_eq!(balances.near, U128(0));
    assert_eq!(balances.tokens[&husd()], U128(0));
    assert_eq!(sim.user(&mallory()), None);
    assert!(sim.view(|v| v.get_holds_for(mallory(), None)).is_empty());
    assert!(sim.view(|v| v.get_active_holds(mallory())).is_empty());
    assert_eq!(sim.stats(&mallory()), PayeeStatsView::default());
}

#[test]
fn token_deposits_come_only_from_allowed_tokens() {
    let mut sim = Sim::new();
    sim.register(&alice(), 0);
    assert_error(sim.ft_deposit(&account("fake-usdc.near"), &alice(), 5 * HUSD, ""), "E_NOT_TOKEN");
    assert_error(sim.ft_deposit(&alice(), &alice(), 5 * HUSD, ""), "E_NOT_TOKEN");
    assert_eq!(sim.husd_of(&alice()), 0);
}

#[test]
fn token_deposit_credits_the_sender_or_credit_to() {
    let mut sim = Sim::new();
    sim.register(&alice(), 0);
    sim.register(&bob(), 0);
    assert_eq!(sim.ft_deposit(&husd(), &alice(), 5 * HUSD, ""), Ok(0));
    assert_eq!(sim.husd_of(&alice()), 5 * HUSD);

    assert_eq!(sim.ft_deposit(&husd(), &treasury(), 7 * HUSD, r#"{"credit_to":"bob.holdup.near"}"#), Ok(0));
    assert_eq!(sim.husd_of(&bob()), 7 * HUSD);
    let deposit = sim.event("deposit");
    assert_eq!(deposit["account_id"], "bob.holdup.near");
    assert_eq!(deposit["asset"], husd().as_str());
    assert_eq!(deposit["amount"], (7 * HUSD).to_string());
    // Tokens are not NEAR liabilities.
    assert_eq!(sim.near_liabilities(), 0);
}

#[test]
fn token_deposit_for_an_unknown_account_is_returned() {
    let mut sim = Sim::new();
    assert_eq!(sim.ft_deposit(&husd(), &mallory(), 5 * HUSD, ""), Ok(5 * HUSD));
    assert_eq!(
        sim.ft_deposit(&husd(), &treasury(), 5 * HUSD, r#"{"credit_to":"nobody.near"}"#),
        Ok(5 * HUSD)
    );
    assert!(sim.events().is_empty());
    assert_eq!(sim.husd_of(&mallory()), 0);
}

#[test]
fn malformed_transfer_messages_are_rejected() {
    let mut sim = Sim::new();
    sim.register(&alice(), 0);
    for msg in ["hello", "{}", r#"{"credit_to":"NOT VALID"}"#, r#"{"credit_to":"alice.holdup.near","x":1}"#] {
        assert_error(sim.ft_deposit(&husd(), &alice(), HUSD, msg), "E_BAD_MSG");
    }
    assert_eq!(sim.husd_of(&alice()), 0);
}

#[test]
fn balance_overflow_is_refused() {
    let mut sim = Sim::new();
    sim.register(&alice(), 0);
    sim.fund_husd(&alice(), u128::MAX);
    assert_error(sim.ft_deposit(&husd(), &alice(), 1, ""), "E_OVERFLOW");
    assert_eq!(sim.husd_of(&alice()), u128::MAX);
}
