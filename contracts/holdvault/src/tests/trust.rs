use super::sending::funded;
use super::*;

/// Alice trusts Bob (a vault user) and the trust delay has passed.
fn trusting_bob() -> Sim {
    let mut sim = funded();
    sim.register(&bob(), 0);
    sim.add_trusted(&alice(), &bob()).unwrap();
    sim.advance(TRUST_DELAY_MS);
    sim
}

#[test]
fn a_trusted_payee_waits_for_the_trust_delay() {
    let mut sim = funded();
    sim.register(&bob(), 0);
    let trusted = sim.add_trusted(&alice(), &bob()).unwrap();
    assert_eq!(trusted.effective_ms, U64(START_MS + TRUST_DELAY_MS));
    let event = sim.event("trust_added");
    assert_eq!(event["owner"], "alice.holdup.near");
    assert_eq!(event["payee"], "bob.holdup.near");
    assert_eq!(event["effective_ms"], (START_MS + TRUST_DELAY_MS).to_string());

    sim.advance(TRUST_DELAY_MS - 1);
    let early = sim.send(&alice(), &bob(), "near", NEAR).unwrap();
    assert!(!early.instant);
    assert_eq!(early.status, HoldStatus::Held);

    sim.advance(1);
    let instant = sim.send(&alice(), &bob(), "near", 2 * NEAR).unwrap();
    assert!(instant.instant);
    assert_eq!(instant.status, HoldStatus::Released);
    assert_eq!(instant.release_at_ms, instant.created_ms);
    assert_eq!(instant.settled_ms, Some(instant.created_ms));
    assert_eq!(sim.event_names(), ["hold_created", "hold_released"]);
    assert_eq!(sim.event("hold_created")["instant"], true);
    assert_eq!(sim.event("hold_released")["internal"], true);
    assert_eq!(sim.near_of(&bob()), 2 * NEAR);
    // Only the held payment counts in the payee's statistics.
    let stats = sim.stats(&bob());
    assert_eq!((stats.holds, stats.released), (1, 0));
    assert_eq!(sim.user(&alice()).unwrap().active_holds, 1);
    sim.assert_solvent();
}

#[test]
fn instant_payment_to_an_outside_account_pays_out() {
    let mut sim = funded();
    sim.add_trusted(&alice(), &shop()).unwrap();
    sim.advance(TRUST_DELAY_MS);
    let hold = sim.send(&alice(), &shop(), "near", 2 * NEAR).unwrap();
    assert!(hold.instant);
    assert_eq!(hold.status, HoldStatus::Releasing);
    assert_eq!(function_calls(sim.receipts()), vec![(vault(), "on_payout".to_owned())]);
    assert_eq!(sim.near_liabilities(), 18 * NEAR);
    sim.assert_solvent();
    sim.on_payout(hold.id.0, ok()).unwrap();
    assert_eq!(sim.status(hold.id.0), HoldStatus::Released);
    assert_eq!(sim.stats(&shop()).released, 0);
    sim.assert_solvent();
}

#[test]
fn instant_payments_are_capped_per_sender_asset_and_utc_day() {
    let mut sim = trusting_bob();
    sim.deposit_near(&alice(), &alice(), 40 * NEAR).unwrap();
    let token = husd().to_string();
    let instant =
        |sim: &mut Sim, asset: &str, amount: u128| sim.send(&alice(), &bob(), asset, amount).unwrap().instant;

    assert!(instant(&mut sim, "near", 6 * NEAR));
    assert!(instant(&mut sim, "near", 4 * NEAR));
    assert!(!instant(&mut sim, "near", 1), "the 10 NEAR cap is used up");
    // HUSD has its own cap.
    assert!(instant(&mut sim, &token, 100 * HUSD));
    assert!(!instant(&mut sim, &token, 1));

    // Still the same UTC day one millisecond before midnight.
    sim.now_ms = START_MS + DAY_MS - 1;
    assert!(!instant(&mut sim, "near", 1));
    // A new UTC day starts a fresh cap, and never more than the cap.
    sim.now_ms = START_MS + DAY_MS;
    assert!(!instant(&mut sim, "near", 10 * NEAR + 1));
    assert!(instant(&mut sim, "near", 3 * NEAR));
    assert!(!instant(&mut sim, "near", 7 * NEAR + 1));
    assert!(instant(&mut sim, "near", 7 * NEAR));
    assert!(!instant(&mut sim, "near", 1));

    // Another sender has an independent cap.
    sim.register(&carol(), 20 * NEAR);
    sim.add_trusted(&carol(), &bob()).unwrap();
    sim.advance(TRUST_DELAY_MS);
    assert!(sim.send(&carol(), &bob(), "near", 10 * NEAR).unwrap().instant);
}

#[test]
fn held_payments_do_not_use_the_cap() {
    let mut sim = trusting_bob();
    let over_cap = sim.send(&alice(), &bob(), "near", 10 * NEAR + 1).unwrap();
    assert!(!over_cap.instant);
    assert!(sim.send(&alice(), &bob(), "near", 9 * NEAR).unwrap().instant);
}

#[test]
fn an_asset_without_a_cap_is_never_instant() {
    let mut input = config();
    input.instant_caps.remove(husd().as_str());
    let mut sim = Sim::try_init(input, DEFAULT_RESERVE).unwrap();
    sim.register(&alice(), NEAR);
    sim.fund_husd(&alice(), 10 * HUSD);
    sim.register(&bob(), 0);
    sim.add_trusted(&alice(), &bob()).unwrap();
    sim.advance(TRUST_DELAY_MS);
    assert!(!sim.send(&alice(), &bob(), husd().as_str(), 1).unwrap().instant);
    assert!(sim.send(&alice(), &bob(), "near", 1).unwrap().instant);
}

#[test]
fn re_adding_never_restarts_or_shortens_the_delay() {
    let mut sim = funded();
    let first = sim.add_trusted(&alice(), &bob()).unwrap().effective_ms;
    sim.advance(TRUST_DELAY_MS / 2);
    assert_eq!(sim.add_trusted(&alice(), &bob()).unwrap().effective_ms, first);
    sim.advance(TRUST_DELAY_MS);
    assert_eq!(sim.add_trusted(&alice(), &bob()).unwrap().effective_ms, first);
    assert_eq!(sim.user(&alice()).unwrap().trusted, vec![TrustedView { payee: bob(), effective_ms: first }]);
}

#[test]
fn removal_is_immediate_and_trusting_again_restarts_the_delay() {
    let mut sim = trusting_bob();
    assert_eq!(sim.remove_trusted(&alice(), &bob()), Ok(true));
    let event = sim.event("trust_removed");
    assert_eq!(
        (event["owner"].as_str(), event["payee"].as_str()),
        (Some("alice.holdup.near"), Some("bob.holdup.near"))
    );
    assert!(!sim.send(&alice(), &bob(), "near", NEAR).unwrap().instant);

    let again = sim.add_trusted(&alice(), &bob()).unwrap();
    assert_eq!(again.effective_ms, U64(sim.now_ms + TRUST_DELAY_MS));
    assert!(!sim.send(&alice(), &bob(), "near", NEAR).unwrap().instant);

    assert_eq!(sim.remove_trusted(&alice(), &carol()), Ok(false));
    assert!(sim.events().is_empty());
}

#[test]
fn the_trust_list_is_limited() {
    let mut sim = funded();
    for i in 0..50 {
        sim.add_trusted(&alice(), &account(&format!("payee{i}.near"))).unwrap();
    }
    assert_error(sim.add_trusted(&alice(), &account("payee50.near")), "E_TOO_MANY_TRUSTED");
    assert!(sim.add_trusted(&alice(), &account("payee7.near")).is_ok());
    sim.remove_trusted(&alice(), &account("payee7.near")).unwrap();
    assert!(sim.add_trusted(&alice(), &account("payee50.near")).is_ok());
    sim.assert_solvent();
}

#[test]
fn trust_needs_a_registered_owner_and_another_payee() {
    let mut sim = funded();
    assert_error(sim.add_trusted(&mallory(), &bob()), "E_NOT_REGISTERED");
    assert_error(sim.remove_trusted(&mallory(), &bob()), "E_NOT_REGISTERED");
    assert_error(sim.add_trusted(&alice(), &alice()), "E_SELF_PAYMENT");
    assert_error(sim.add_trusted(&alice(), &vault()), "E_SELF_PAYMENT");
}
