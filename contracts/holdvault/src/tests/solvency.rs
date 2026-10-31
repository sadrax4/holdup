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
    sim.register(&alice(), 7 * NEAR);
    sim.register(&bob(), 3 * NEAR);
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
    // Every user yoctoNEAR left; the vault keeps exactly the deployer's reserve and the fees,
    // which still pay for its storage.
    assert_eq!(sim.balance, deployed_balance + 2 * REGISTRATION_FEE);
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

#[test]
fn a_low_reserve_blocks_new_holds_but_never_exits() {
    let mut sim = Sim::try_init(config(), 10 * MILLINEAR).unwrap();
    sim.register(&alice(), 20 * NEAR);
    sim.fund_husd(&alice(), 100 * HUSD);
    sim.register(&bob(), 0);

    let mut held = Vec::new();
    let refused = loop {
        let payee = if held.len() % 2 == 0 { shop() } else { bob() };
        match sim.send(&alice(), &payee, "near", NEAR) {
            Ok(hold) => held.push(hold.id.0),
            Err(message) => break message,
        }
        assert!(held.len() < 10, "the reserve should run out");
    };
    assert!(refused.contains("E_INSOLVENT:"), "{refused}");
    assert!(held.len() >= 2, "a 0.01 NEAR reserve pays for a few holds");
    assert_error(sim.send(&alice(), &shop(), husd().as_str(), HUSD), "E_INSOLVENT");
    sim.assert_solvent();

    // Brakes, cancels, releases and payout callbacks still work and the vault stays solvent.
    for hold_id in &held {
        sim.extend(&oracle(), *hold_id, START_MS + HOUR_MS, 1, &"e".repeat(4096)).unwrap();
        sim.assert_solvent();
    }
    sim.cancel(&alice(), held[0]).unwrap();
    sim.assert_solvent();
    sim.now_ms = START_MS + HOUR_MS;
    for hold_id in &held[1..] {
        if sim.release(&mallory(), *hold_id).unwrap().is_none() {
            sim.on_payout(*hold_id, failed()).unwrap();
            sim.refund(NEAR);
        }
        sim.assert_solvent();
    }
    assert_eq!(sim.near_of(&alice()) + sim.near_of(&bob()), 20 * NEAR);
    assert_eq!(settlement_reserve(&sim), 0);
}

#[test]
fn solvency_view_reports_the_books() {
    let mut sim = funded();
    sim.send(&alice(), &shop(), "near", NEAR).unwrap();
    let solvency = sim.solvency();
    assert_eq!(solvency.account_balance, U128(sim.balance));
    assert_eq!(solvency.locked, U128(0));
    assert_eq!(solvency.near_liabilities, U128(20 * NEAR));
    assert_eq!(solvency.settlement_reserve, U128(storage_reserve()));
    assert_eq!(solvency.storage_usage, U64(sim.storage_usage));
    let storage_cost = u128::from(sim.storage_usage) * env::storage_byte_cost().as_yoctonear();
    assert_eq!(solvency.storage_cost, U128(storage_cost));
    assert_eq!(solvency.surplus, U128(sim.balance - 20 * NEAR - storage_reserve() - storage_cost));
}
