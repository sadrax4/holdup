use super::sending::funded;
use super::*;
use crate::{FT_STORAGE_DEPOSIT, HOLD_SETTLEMENT_BYTES};

fn storage_reserve() -> u128 {
    u128::from(HOLD_SETTLEMENT_BYTES) * env::storage_byte_cost().as_yoctonear()
}

fn settlement_reserve(sim: &Sim) -> u128 {
    sim.view(|v| v.settlement_reserve)
}

#[test]
fn every_open_hold_reserves_what_it_needs_to_settle() {
    let mut sim = funded();
    let token_deposits = FT_STORAGE_DEPOSIT.as_yoctonear() + 1;
    sim.send(&alice(), &shop(), "near", NEAR).unwrap();
    assert_eq!(settlement_reserve(&sim), storage_reserve());
    sim.send(&alice(), &shop(), husd().as_str(), HUSD).unwrap();
    assert_eq!(settlement_reserve(&sim), 2 * storage_reserve() + token_deposits);
    sim.assert_solvent();

    sim.cancel(&alice(), 0).unwrap();
    assert_eq!(settlement_reserve(&sim), storage_reserve() + token_deposits);
    sim.assert_solvent();

    sim.advance(HOLD_MS);
    sim.release(&mallory(), 1).unwrap();
    assert_eq!(settlement_reserve(&sim), storage_reserve());
    sim.assert_solvent();

    sim.on_payout(1, ok()).unwrap();
    assert_eq!(settlement_reserve(&sim), 0);
    sim.assert_solvent();
}

#[test]
fn the_last_users_can_withdraw_everything() {
    let mut sim = Sim::new();
    let deployed_balance = sim.balance;
    sim.register(&alice(), 7 * NEAR + HOLD_FEE);
    sim.register(&bob(), 3 * NEAR + HOLD_FEE);
    sim.send(&alice(), &shop(), "near", 7 * NEAR).unwrap();
    sim.send(&bob(), &carol(), "near", 3 * NEAR).unwrap();
    sim.advance(HOLD_MS);
    for hold_id in [0, 1] {
        sim.release(&mallory(), hold_id).unwrap();
        sim.on_payout(hold_id, ok()).unwrap();
        sim.assert_solvent();
    }
    assert_eq!(sim.near_of(&alice()) + sim.near_of(&bob()), 0);
    assert_eq!(sim.near_liabilities(), 0);
    assert_eq!(settlement_reserve(&sim), 0);
    // Every user yoctoNEAR left; the vault keeps exactly the deployer's reserve, the registration
    // fees and the hold storage fees, which still pay for its storage.
    assert_eq!(sim.balance, deployed_balance + 2 * REGISTRATION_FEE + 2 * HOLD_FEE);
    let solvency = sim.solvency();
    assert!(solvency.account_balance.0 >= solvency.storage_cost.0);
    sim.assert_solvent();
}

#[test]
fn registration_is_refused_when_nothing_pays_for_storage() {
    let mut input = config();
    input.registration_fee = U128(0);
    let mut sim = Sim::try_init(input, 0).unwrap();
    assert_error(sim.deposit_near(&treasury(), &alice(), 0), "E_INSOLVENT");
    assert!(!sim.view(|v| v.is_registered(alice())));
    // Anyone can top up the reserve with a plain transfer to the vault account.
    sim.balance += NEAR;
    assert_eq!(sim.deposit_near(&treasury(), &alice(), 0), Ok(0));
    sim.assert_solvent();
}

/// Storage fees mean new records never cost the vault its reserve, so the vault falls short only
/// for a moment: when a failed NEAR payout refunds the sender before the transfer's deposit has
/// come back as a refund receipt. In that window new records are refused, and every exit still
/// works.
#[test]
fn a_vault_short_of_funds_refuses_new_records_but_never_exits() {
    let mut sim = funded();
    sim.register(&bob(), 0);
    let failing = sim.send(&alice(), &shop(), "near", 10 * NEAR).unwrap().id.0;
    let braked = sim.send(&alice(), &shop(), "near", NEAR).unwrap().id.0;
    let to_bob = sim.send(&alice(), &bob(), "near", NEAR).unwrap().id.0;
    let token = sim.send(&alice(), &shop(), husd().as_str(), 10 * HUSD).unwrap().id.0;
    sim.advance(HOLD_MS);
    sim.release(&mallory(), failing).unwrap();
    sim.on_payout(failing, failed()).unwrap();
    // The vault owes Alice the 10 NEAR again but has not received them back yet.
    assert_eq!(sim.solvency().surplus, U128(0));

    assert_error(sim.send(&alice(), &shop(), "near", NEAR), "E_INSOLVENT");
    assert_error(sim.send(&alice(), &shop(), husd().as_str(), HUSD), "E_INSOLVENT");
    assert_error(sim.add_trusted(&alice(), &carol()), "E_INSOLVENT");
    assert_error(sim.set_guardian(&alice(), Some(&carol())), "E_INSOLVENT");
    assert_error(sim.deposit_near(&treasury(), &carol(), REGISTRATION_FEE), "E_INSOLVENT");

    // Brakes, cancels, releases and payout callbacks still work.
    sim.extend(&oracle(), braked, sim.now_ms + HOUR_MS, 1, &"e".repeat(4096)).unwrap();
    sim.cancel(&alice(), braked).unwrap();
    assert!(sim.release(&mallory(), to_bob).unwrap().is_some());
    assert_eq!(sim.release(&mallory(), token), Ok(None));
    sim.on_payout(token, ok()).unwrap();
    assert_eq!(sim.near_of(&bob()), NEAR);
    // Deposits to registered users only move money, so they work too.
    sim.deposit_near(&treasury(), &bob(), NEAR).unwrap();

    // The refund receipt arrives and everything works again.
    sim.refund(10 * NEAR);
    sim.assert_solvent();
    assert!(sim.send(&alice(), &shop(), "near", NEAR).is_ok());
    assert_eq!(settlement_reserve(&sim), storage_reserve());
}

#[test]
fn solvency_view_reports_the_books() {
    let mut sim = funded();
    sim.send(&alice(), &shop(), "near", NEAR).unwrap();
    let solvency = sim.solvency();
    assert_eq!(solvency.account_balance, U128(sim.balance));
    assert_eq!(solvency.locked, U128(0));
    assert_eq!(solvency.near_liabilities, U128(20 * NEAR - HOLD_FEE));
    assert_eq!(solvency.settlement_reserve, U128(storage_reserve()));
    assert_eq!(solvency.storage_usage, U64(sim.storage_usage));
    let storage_cost = u128::from(sim.storage_usage) * env::storage_byte_cost().as_yoctonear();
    assert_eq!(solvency.storage_cost, U128(storage_cost));
    assert_eq!(
        solvency.surplus,
        U128(sim.balance - (20 * NEAR - HOLD_FEE) - storage_reserve() - storage_cost)
    );
}
