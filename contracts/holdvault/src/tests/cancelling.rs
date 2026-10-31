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
    // The amount comes back; the storage fee stays with the vault, which keeps the record.
    assert_eq!(sim.near_of(&alice()), 20 * NEAR - HOLD_FEE);
    assert_eq!(sim.near_liabilities(), 20 * NEAR - HOLD_FEE);
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
    assert_eq!(stats.last_report_ms, Some(U64(START_MS)));
}

/// The review's attack: one sender flagged-and-cancelled two tiny payments and the payee read as
/// reported by two senders, braking every later payment to it. Each sender now counts once.
#[test]
fn each_sender_reports_a_payee_only_once() {
    let mut sim = funded();
    sim.register(&bob(), NEAR);
    for _ in 0..3 {
        sim.send(&alice(), &shop(), "near", 1).unwrap();
    }
    for id in 0..3 {
        sim.extend(&oracle(), id, START_MS + HOUR_MS, 1, "{}").unwrap();
    }
    sim.advance(MINUTE_MS);
    sim.cancel(&alice(), 0).unwrap();
    assert_eq!(sim.event("hold_cancelled")["report"], true);
    sim.advance(MINUTE_MS);
    sim.cancel(&alice(), 1).unwrap();
    assert_eq!(sim.event("hold_cancelled")["report"], false);
    // A guardian's cancel reports for the sender, who has reported already.
    sim.set_guardian(&alice(), Some(&carol())).unwrap();
    sim.guardian_cancel(&carol(), 2).unwrap();
    assert_eq!(sim.event("hold_cancelled")["report"], false);
    let stats = sim.stats(&shop());
    assert_eq!((stats.cancelled, stats.cancelled_after_flag), (3, 1));
    assert_eq!(stats.last_report_ms, Some(U64(START_MS + MINUTE_MS)));

    // A second sender is a second report.
    let hold = sim.send(&bob(), &shop(), "near", 1).unwrap().id.0;
    sim.extend(&oracle(), hold, sim.now_ms + HOUR_MS, 1, "{}").unwrap();
    sim.advance(DAY_MS);
    sim.cancel(&bob(), hold).unwrap();
    assert_eq!(sim.event("hold_cancelled")["report"], true);
    let stats = sim.stats(&shop());
    assert_eq!((stats.cancelled, stats.cancelled_after_flag), (4, 2));
    assert_eq!(stats.last_report_ms, Some(U64(sim.now_ms)));
    // Reports are per payee: Alice can still report another one.
    let other = sim.send(&alice(), &mallory(), "near", 1).unwrap().id.0;
    sim.extend(&oracle(), other, sim.now_ms + HOUR_MS, 1, "{}").unwrap();
    sim.cancel(&alice(), other).unwrap();
    assert_eq!(sim.stats(&mallory()).cancelled_after_flag, 1);
}

/// A flag says nothing about reports: an honest payment braked a year later and then released
/// moves `last_flag_ms` but leaves `last_report_ms`, so year-old reports stay old.
#[test]
fn a_later_flag_does_not_refresh_old_reports() {
    let mut sim = funded();
    sim.register(&bob(), NEAR);
    sim.register(&carol(), NEAR);
    for sender in [alice(), bob()] {
        let hold = sim.send(&sender, &shop(), "near", 1).unwrap().id.0;
        sim.extend(&oracle(), hold, sim.now_ms + HOUR_MS, 1, "{}").unwrap();
        sim.cancel(&sender, hold).unwrap();
    }
    let reported_ms = sim.now_ms;
    sim.now_ms = START_MS + 365 * DAY_MS;
    let honest = sim.send(&carol(), &shop(), "near", NEAR / 2).unwrap().id.0;
    sim.extend(&oracle(), honest, sim.now_ms + HOLD_MS + 1, 1, "{}").unwrap();
    sim.advance(HOLD_MS + 1);
    sim.release(&mallory(), honest).unwrap();
    sim.on_payout(honest, ok()).unwrap();
    let stats = sim.stats(&shop());
    assert_eq!(stats.cancelled_after_flag, 2);
    assert_eq!(stats.last_flag_ms, Some(U64(START_MS + 365 * DAY_MS)));
    assert_eq!(stats.last_report_ms, Some(U64(reported_ms)));
}

#[test]
fn a_hold_settles_only_once() {
    let mut sim = funded();
    sim.send(&alice(), &shop(), "near", NEAR).unwrap();
    sim.cancel(&alice(), 0).unwrap();
    assert_error(sim.cancel(&alice(), 0), "E_HOLD_NOT_ACTIVE");
    sim.advance(DAY_MS);
    assert_error(sim.release(&mallory(), 0), "E_HOLD_NOT_ACTIVE");
    assert_eq!(sim.near_of(&alice()), 20 * NEAR - HOLD_FEE);
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
    assert_eq!(sim.near_of(&alice()), 15 * NEAR - HOLD_FEE - ENTRY_FEE);
}
