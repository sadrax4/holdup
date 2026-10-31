use super::sending::funded;
use super::*;

#[test]
fn sender_cancels_and_gets_the_money_back() {
    let mut sim = funded();
    sim.send(&alice(), &shop(), "near", 5 * NEAR).unwrap();
    sim.advance(MINUTE_MS);
    let cancelled = sim.cancel(&alice(), 0).unwrap();
    assert_eq!(cancelled.status, HoldStatus::Cancelled);
    assert_eq!(cancelled.cancelled_by, Some(alice()));
    assert_eq!(cancelled.settled_ms, Some(U64(START_MS + MINUTE_MS)));
    assert_eq!(sim.near_of(&alice()), 20 * NEAR);
    assert_eq!(sim.near_liabilities(), 20 * NEAR);
    assert_eq!(sim.user(&alice()).unwrap().active_holds, 0);
    assert!(sim.view(|v| v.get_active_holds(alice())).is_empty());

    let event = sim.event("hold_cancelled");
    assert_eq!(event["hold_id"], "0");
    assert_eq!(event["by"], "sender");
    assert_eq!(event["cancelled_by"], "alice.holdup.near");
    assert_eq!(event["flagged"], false);
    assert_eq!(event["amount"], (5 * NEAR).to_string());

    let stats = sim.stats(&shop());
    assert_eq!((stats.holds, stats.cancelled, stats.cancelled_after_flag), (1, 1, 0));
    // A cancelled hold never becomes due.
    sim.advance(DAY_MS);
    assert!(sim.view(|v| v.get_due_holds(None)).is_empty());
    sim.assert_solvent();
}

#[test]
fn cancelling_a_token_hold_returns_the_tokens() {
    let mut sim = funded();
    sim.send(&alice(), &shop(), husd().as_str(), 40 * HUSD).unwrap();
    assert_eq!(sim.husd_of(&alice()), 960 * HUSD);
    sim.cancel(&alice(), 0).unwrap();
    assert_eq!(sim.husd_of(&alice()), 1_000 * HUSD);
    sim.assert_solvent();
}

#[test]
fn only_the_sender_or_guardian_can_cancel() {
    let mut sim = funded();
    sim.send(&alice(), &shop(), "near", 5 * NEAR).unwrap();
    for caller in [shop(), mallory(), oracle(), treasury(), vault()] {
        assert_error(sim.cancel(&caller, 0), "E_NOT_SENDER");
    }
    // Without a guardian nobody can use guardian_cancel, not even the sender.
    for caller in [alice(), shop(), mallory(), oracle()] {
        assert_error(sim.guardian_cancel(&caller, 0), "E_NOT_GUARDIAN");
    }
    assert_error(sim.cancel(&alice(), 42), "E_HOLD_NOT_FOUND");
    assert_eq!(sim.status(0), HoldStatus::Held);
}

#[test]
fn cancelling_after_a_flag_counts_against_the_payee() {
    let mut sim = funded();
    sim.send(&alice(), &shop(), "near", NEAR).unwrap();
    sim.send(&alice(), &shop(), "near", NEAR).unwrap();
    sim.extend(&oracle(), 0, START_MS + DAY_MS, 1 << 7, "{}").unwrap();
    sim.cancel(&alice(), 0).unwrap();
    assert_eq!(sim.event("hold_cancelled")["flagged"], true);
    sim.cancel(&alice(), 1).unwrap();
    let stats = sim.stats(&shop());
    assert_eq!(
        (stats.holds, stats.flagged, stats.cancelled, stats.cancelled_after_flag, stats.released),
        (2, 1, 2, 1, 0)
    );
}

#[test]
fn a_hold_settles_only_once() {
    let mut sim = funded();
    sim.send(&alice(), &shop(), "near", NEAR).unwrap();
    sim.cancel(&alice(), 0).unwrap();
    assert_error(sim.cancel(&alice(), 0), "E_HOLD_NOT_ACTIVE");
    sim.advance(DAY_MS);
    assert_error(sim.release(&mallory(), 0), "E_HOLD_NOT_ACTIVE");
    assert_eq!(sim.near_of(&alice()), 20 * NEAR);
}

#[test]
fn nothing_can_cancel_a_payout_in_flight() {
    let mut sim = funded();
    sim.set_guardian(&alice(), Some(&carol())).unwrap();
    sim.send(&alice(), &shop(), "near", 5 * NEAR).unwrap();
    sim.advance(HOLD_MS);
    assert_eq!(sim.release(&mallory(), 0), Ok(None));
    assert_eq!(sim.status(0), HoldStatus::Releasing);
    assert_error(sim.cancel(&alice(), 0), "E_HOLD_NOT_ACTIVE");
    assert_error(sim.guardian_cancel(&carol(), 0), "E_HOLD_NOT_ACTIVE");
    assert_error(sim.extend(&oracle(), 0, START_MS + DAY_MS, 1, "{}"), "E_HOLD_NOT_ACTIVE");

    sim.on_payout(0, ok()).unwrap();
    assert_error(sim.cancel(&alice(), 0), "E_HOLD_NOT_ACTIVE");
    assert_eq!(sim.near_of(&alice()), 15 * NEAR);
}
