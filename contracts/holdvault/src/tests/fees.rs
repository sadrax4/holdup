//! Storage fees: holds, trusted payees and guardian changes pay for the storage they leave
//! behind, so no stream of calls can use up the reserve every user depends on.

use super::sending::funded;
use super::*;
use crate::HOLD_SETTLEMENT_BYTES;

/// What the vault holds beyond its debts, reserves and storage.
fn surplus(sim: &Sim) -> u128 {
    sim.solvency().surplus.0
}

/// A distinct account id of 64 characters, the longest NEAR allows.
fn long_account(first: char) -> AccountId {
    account(&format!("{first}{}.near", "x".repeat(58)))
}

#[test]
fn every_hold_pays_the_same_fee_whatever_happens_to_it() {
    let mut sim = funded();
    sim.register(&bob(), 0);
    sim.add_trusted(&alice(), &bob()).unwrap();
    sim.advance(TRUST_DELAY_MS);
    let near = sim.near_of(&alice());

    let cancelled = sim.send(&alice(), &shop(), "near", NEAR).unwrap();
    assert_eq!(sim.event("hold_created")["storage_fee"], HOLD_FEE.to_string());
    sim.cancel(&alice(), cancelled.id.0).unwrap();
    sim.send(&alice(), &shop(), husd().as_str(), HUSD).unwrap();
    let instant = sim.send(&alice(), &bob(), "near", NEAR).unwrap();
    assert!(instant.instant);
    assert_eq!(sim.event("hold_created")["storage_fee"], HOLD_FEE.to_string());

    assert_eq!(sim.near_of(&alice()), near - NEAR - 3 * HOLD_FEE);
    assert_eq!(sim.near_of(&bob()), NEAR);
    sim.assert_solvent();
}

/// The review's attack: a stranger registers itself and loops send(1 yoctoNEAR) + cancel. Every
/// settled hold stays in storage, so without a fee the loop used up the reserve for the price of
/// gas and every user's payments failed with E_INSOLVENT. Now each round pays for itself.
#[test]
fn churning_tiny_payments_cannot_use_up_the_reserve() {
    let mut sim = Sim::new();
    sim.register(&alice(), 20 * NEAR);
    sim.register(&bob(), 0);
    let rounds = 200;
    sim.deposit_near(&mallory(), &mallory(), REGISTRATION_FEE + rounds * HOLD_FEE + 1).unwrap();
    let before = surplus(&sim);
    let mut lowest = before;
    let refused = loop {
        match sim.send(&mallory(), &shop(), "near", 1) {
            Ok(hold) => {
                lowest = lowest.min(surplus(&sim));
                sim.cancel(&mallory(), hold.id.0).unwrap();
                lowest = lowest.min(surplus(&sim));
            }
            Err(message) => break message,
        }
    };
    // Mallory runs out of her own NEAR long before the vault runs out of reserve.
    assert!(refused.contains("E_INSUFFICIENT_BALANCE:"), "{refused}");
    assert_eq!(sim.near_of(&mallory()), 1);
    assert_eq!(sim.view(|v| v.next_hold_id), rounds as u64);
    assert!(lowest >= before, "the surplus fell from {before} to {lowest}");
    assert!(surplus(&sim) >= before);
    assert!(sim.send(&alice(), &bob(), "near", NEAR).is_ok());
    assert!(sim.send(&alice(), &shop(), "near", NEAR).is_ok());
}

/// Instant payments are capped by amount, not by count: dust payments to a trusted vault user
/// each leave a hold record behind, and each pays for it.
#[test]
fn instant_dust_payments_pay_for_their_records() {
    let mut sim = Sim::try_init(config(), NEAR).unwrap();
    sim.deposit_near(&mallory(), &mallory(), REGISTRATION_FEE + ENTRY_FEE + 100 * HOLD_FEE + 100).unwrap();
    sim.register(&bob(), 0);
    sim.add_trusted(&mallory(), &bob()).unwrap();
    sim.advance(TRUST_DELAY_MS);
    let before = surplus(&sim);
    for _ in 0..100 {
        assert!(sim.send(&mallory(), &bob(), "near", 1).unwrap().instant);
        assert!(surplus(&sim) >= before);
    }
    assert_error(sim.send(&mallory(), &bob(), "near", 1), "E_INSUFFICIENT_BALANCE");
    assert_eq!(sim.near_of(&bob()), 100);
}

/// With 64-character account ids everywhere, a token hold to a new payee, braked with the
/// longest evidence and then either cancelled by a guardian (adding a report) or paid out to an
/// outside account (attaching the storage deposit), never costs the vault more than its fee.
#[test]
fn the_hold_fee_covers_the_largest_hold() {
    let token = long_account('t');
    let mut input = config();
    input.tokens = vec![token.clone()];
    input.instant_caps = HashMap::new();
    let mut sim = Sim::try_init(input, NEAR).unwrap();
    let (sender, guardian) = (long_account('s'), long_account('g'));
    sim.deposit_near(&sender, &sender, REGISTRATION_FEE + NEAR).unwrap();
    assert_eq!(sim.ft_deposit(&token, &sender, 100 * HUSD, ""), Ok(0));
    sim.set_guardian(&sender, Some(&guardian)).unwrap();
    let memo = Some("m".repeat(64));

    for (payee, cancel) in [(long_account('a'), true), (long_account('b'), false)] {
        let before = surplus(&sim);
        let storage_before = sim.storage_usage;
        let hold = sim.send_with_memo(&sender, &payee, token.as_str(), HUSD, memo.clone()).unwrap().id.0;
        assert!(surplus(&sim) >= before, "send");
        let storage_sent = sim.storage_usage;
        sim.extend(&oracle(), hold, sim.now_ms + HOUR_MS, 1, &"e".repeat(4096)).unwrap();
        assert!(surplus(&sim) >= before, "extend");
        if cancel {
            sim.guardian_cancel(&guardian, hold).unwrap();
            assert_eq!(sim.event("hold_cancelled")["report"], true);
        } else {
            sim.advance(HOUR_MS);
            assert_eq!(sim.release(&mallory(), hold), Ok(None));
            sim.on_payout(hold, ok()).unwrap();
        }
        assert!(surplus(&sim) >= before, "settle");
        assert!(sim.hold(hold).settled_ms.is_some());
        // What settling added stays within the bytes reserved for it at send.
        assert!(
            sim.storage_usage <= storage_sent + HOLD_SETTLEMENT_BYTES,
            "settling added {} bytes",
            sim.storage_usage - storage_sent
        );
        assert!(sim.storage_usage > storage_before);
    }
    sim.assert_solvent();
}

#[test]
fn trusted_payees_and_guardian_changes_pay_for_their_storage() {
    let mut sim = funded();
    let near = sim.near_of(&alice());
    sim.add_trusted(&alice(), &bob()).unwrap();
    assert_eq!(sim.event("trust_added")["storage_fee"], ENTRY_FEE.to_string());
    // Re-adding stores nothing new, so it is free.
    sim.add_trusted(&alice(), &bob()).unwrap();
    assert_eq!(sim.event("trust_added")["storage_fee"], "0");
    sim.set_guardian(&alice(), Some(&carol())).unwrap();
    sim.set_guardian(&alice(), None).unwrap();
    assert_eq!(sim.event("guardian_changed")["storage_fee"], ENTRY_FEE.to_string());
    assert_eq!(sim.near_of(&alice()), near - 3 * ENTRY_FEE);
    assert_eq!(sim.near_liabilities(), near - 3 * ENTRY_FEE);

    // A user with no NEAR in the vault cannot add records.
    sim.register(&bob(), 0);
    sim.fund_husd(&bob(), 10 * HUSD);
    assert_error(sim.add_trusted(&bob(), &carol()), "E_INSUFFICIENT_BALANCE");
    assert_error(sim.set_guardian(&bob(), Some(&carol())), "E_INSUFFICIENT_BALANCE");
    assert_error(sim.send(&bob(), &carol(), husd().as_str(), HUSD), "E_INSUFFICIENT_BALANCE");
    // The checks before the fee keep their order.
    assert_error(sim.add_trusted(&bob(), &bob()), "E_SELF_PAYMENT");
    assert_error(sim.set_guardian(&bob(), Some(&bob())), "E_BAD_GUARDIAN");
}

#[test]
fn the_entry_fee_covers_the_longest_entries() {
    let mut sim = Sim::new();
    let owner = long_account('o');
    sim.deposit_near(&owner, &owner, REGISTRATION_FEE + NEAR).unwrap();
    let fee_bytes = u64::try_from(ENTRY_FEE / env::storage_byte_cost().as_yoctonear()).unwrap();
    let grew = |sim: &mut Sim, call: &dyn Fn(&mut Sim)| {
        let (before, storage) = (surplus(sim), sim.storage_usage);
        call(sim);
        assert!(
            sim.storage_usage <= storage + fee_bytes,
            "an entry took {} bytes",
            sim.storage_usage - storage
        );
        assert!(surplus(sim) >= before);
    };
    for first in ['a', 'b'] {
        grew(&mut sim, &|sim: &mut Sim| {
            sim.add_trusted(&owner, &long_account(first)).unwrap();
        });
    }
    grew(&mut sim, &|sim: &mut Sim| {
        sim.set_guardian(&owner, Some(&long_account('g'))).unwrap();
    });
    grew(&mut sim, &|sim: &mut Sim| {
        sim.set_guardian(&owner, Some(&long_account('h'))).unwrap();
    });
    assert!(sim.user(&owner).unwrap().pending_guardian.is_some());
}
