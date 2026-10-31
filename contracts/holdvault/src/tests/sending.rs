use super::*;

/// Alice has 20 NEAR and 1000 HUSD in the vault.
pub fn funded() -> Sim {
    let mut sim = Sim::new();
    sim.register(&alice(), 20 * NEAR);
    sim.fund_husd(&alice(), 1_000 * HUSD);
    sim
}

#[test]
fn payment_to_a_new_payee_is_held() {
    let mut sim = funded();
    let hold = sim.send(&alice(), &shop(), "near", 2 * NEAR).unwrap();
    assert_eq!(hold.id, U64(0));
    assert_eq!(hold.sender, alice());
    assert_eq!(hold.payee, shop());
    assert_eq!(hold.asset, "near");
    assert_eq!(hold.amount, U128(2 * NEAR));
    assert_eq!(hold.status, HoldStatus::Held);
    assert!(!hold.instant && !hold.flagged);
    assert_eq!(hold.created_ms, U64(START_MS));
    assert_eq!(hold.release_at_ms, U64(START_MS + HOLD_MS));
    assert_eq!((hold.extensions, hold.reason_bits), (0, 0));
    assert_eq!((hold.evidence_hash.clone(), hold.settled_ms, hold.cancelled_by.clone()), (None, None, None));

    assert_eq!(sim.near_of(&alice()), 18 * NEAR);
    // NEAR inside a hold is still owed by the vault.
    assert_eq!(sim.near_liabilities(), 20 * NEAR);
    assert_eq!(sim.stats(&shop()).holds, 1);
    assert_eq!(sim.user(&alice()).unwrap().active_holds, 1);
    assert_eq!(sim.view(|v| v.get_active_holds(alice())), vec![hold]);

    let created = sim.event("hold_created");
    assert_eq!(created["hold_id"], "0");
    assert_eq!(created["sender"], "alice.holdup.near");
    assert_eq!(created["payee"], "shop.near");
    assert_eq!(created["asset"], "near");
    assert_eq!(created["amount"], (2 * NEAR).to_string());
    assert_eq!(created["release_at_ms"], (START_MS + HOLD_MS).to_string());
    assert_eq!(created["instant"], false);
    sim.assert_solvent();
}

#[test]
fn token_payments_are_held_too() {
    let mut sim = funded();
    let hold = sim.send(&alice(), &shop(), husd().as_str(), 50 * HUSD).unwrap();
    assert_eq!(hold.asset, husd().as_str());
    assert_eq!(hold.status, HoldStatus::Held);
    assert_eq!(sim.husd_of(&alice()), 950 * HUSD);
    assert_eq!(sim.near_liabilities(), 20 * NEAR);
    sim.assert_solvent();
}

#[test]
fn send_validates_every_input() {
    let mut sim = funded();
    let token = husd().to_string();
    assert_error(sim.send(&mallory(), &shop(), "near", NEAR), "E_NOT_REGISTERED");
    assert_error(sim.send(&alice(), &shop(), "near", 0), "E_ZERO_AMOUNT");
    assert_error(sim.send(&alice(), &shop(), "usdc.near", NEAR), "E_UNKNOWN_ASSET");
    assert_error(sim.send(&alice(), &shop(), "NEAR", NEAR), "E_UNKNOWN_ASSET");
    assert_error(sim.send(&alice(), &alice(), "near", NEAR), "E_SELF_PAYMENT");
    assert_error(sim.send(&alice(), &vault(), "near", NEAR), "E_SELF_PAYMENT");
    assert_error(sim.send(&alice(), &shop(), "near", 20 * NEAR + 1), "E_INSUFFICIENT_BALANCE");
    assert_error(sim.send(&alice(), &shop(), "near", u128::MAX), "E_INSUFFICIENT_BALANCE");
    assert_error(sim.send(&alice(), &shop(), &token, 1_000 * HUSD + 1), "E_INSUFFICIENT_BALANCE");
    let too_long = Some("a".repeat(65));
    assert_error(sim.send_with_memo(&alice(), &shop(), "near", NEAR, too_long), "E_MEMO_TOO_LONG");
    // None of the refused calls moved money.
    assert_eq!(sim.near_of(&alice()), 20 * NEAR);
    assert_eq!(sim.view(|v| v.next_hold_id), 0);

    let memo = Some("ab".repeat(32));
    let hold = sim.send_with_memo(&alice(), &shop(), "near", NEAR, memo.clone()).unwrap();
    assert_eq!(hold.memo_hash, memo);
}

#[test]
fn the_whole_balance_can_be_sent() {
    let mut sim = funded();
    sim.send(&alice(), &shop(), "near", 20 * NEAR).unwrap();
    assert_eq!(sim.near_of(&alice()), 0);
    assert_error(sim.send(&alice(), &shop(), "near", 1), "E_INSUFFICIENT_BALANCE");
}

#[test]
fn senders_have_a_limit_on_held_payments() {
    let mut sim = funded();
    for _ in 0..10 {
        sim.send(&alice(), &shop(), "near", NEAR / 10).unwrap();
    }
    assert_error(sim.send(&alice(), &shop(), "near", NEAR / 10), "E_TOO_MANY_HOLDS");

    // The limit is per sender.
    sim.register(&bob(), NEAR);
    assert!(sim.send(&bob(), &shop(), "near", NEAR / 10).is_ok());

    // Settling a hold frees a slot.
    sim.cancel(&alice(), 3).unwrap();
    assert!(sim.send(&alice(), &shop(), "near", NEAR / 10).is_ok());
    assert_error(sim.send(&alice(), &shop(), "near", NEAR / 10), "E_TOO_MANY_HOLDS");
}

#[test]
fn history_is_newest_first_and_bounded() {
    let mut sim = funded();
    sim.register(&bob(), NEAR);
    sim.send(&bob(), &shop(), "near", NEAR / 2).unwrap();
    let mut alice_ids = Vec::new();
    for _ in 0..12 {
        let hold = sim.send(&alice(), &shop(), "near", NEAR / 10).unwrap();
        sim.cancel(&alice(), hold.id.0).unwrap();
        alice_ids.push(hold.id.0);
    }
    let ids = |holds: Vec<HoldView>| holds.into_iter().map(|hold| hold.id.0).collect::<Vec<_>>();
    let newest_first: Vec<u64> = alice_ids.iter().rev().copied().collect();
    assert_eq!(ids(sim.view(|v| v.get_holds_for(alice(), None))), newest_first);
    assert_eq!(ids(sim.view(|v| v.get_holds_for(alice(), Some(2)))), newest_first[..2]);
    assert_eq!(ids(sim.view(|v| v.get_holds_for(alice(), Some(1_000)))), newest_first);
    assert!(sim.view(|v| v.get_holds_for(alice(), Some(0))).is_empty());
    assert_eq!(ids(sim.view(|v| v.get_holds_for(bob(), None))), vec![0]);
}

#[test]
fn history_view_caps_at_one_hundred() {
    let mut sim = funded();
    for _ in 0..105 {
        let hold = sim.send(&alice(), &shop(), "near", 1).unwrap();
        sim.cancel(&alice(), hold.id.0).unwrap();
    }
    assert_eq!(sim.view(|v| v.get_holds_for(alice(), None)).len(), 50);
    let all = sim.view(|v| v.get_holds_for(alice(), Some(500)));
    assert_eq!(all.len(), 100);
    assert_eq!(all[0].id, U64(104));
}
