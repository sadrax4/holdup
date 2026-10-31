use super::sending::funded;
use super::*;

fn dave() -> AccountId {
    account("dave.near")
}

fn guardian_of(sim: &Sim, owner: &AccountId) -> Option<AccountId> {
    sim.user(owner).unwrap().guardian
}

#[test]
fn the_first_guardian_takes_effect_at_once() {
    let mut sim = funded();
    let view = sim.set_guardian(&alice(), Some(&carol())).unwrap();
    assert_eq!(view.guardian, Some(carol()));
    assert_eq!(view.pending_guardian, None);
    let event = sim.event("guardian_changed");
    assert_eq!(event["owner"], "alice.holdup.near");
    assert_eq!(event["guardian"], "carol.holdup.near");
    assert_eq!(event["effective_ms"], START_MS.to_string());

    sim.send(&alice(), &shop(), "near", 5 * NEAR).unwrap();
    let cancelled = sim.guardian_cancel(&carol(), 0).unwrap();
    assert_eq!(cancelled.status, HoldStatus::Cancelled);
    assert_eq!(cancelled.cancelled_by, Some(carol()));
    let event = sim.event("hold_cancelled");
    assert_eq!(
        (event["by"].as_str(), event["cancelled_by"].as_str()),
        (Some("guardian"), Some("carol.holdup.near"))
    );
    // The money goes back to the sender, never to the guardian.
    assert_eq!(sim.near_of(&alice()), 20 * NEAR);
    assert_eq!(sim.near_of(&carol()), 0);
    // The sender keeps the right to cancel their own holds.
    sim.send(&alice(), &shop(), "near", NEAR).unwrap();
    assert!(sim.cancel(&alice(), 1).is_ok());
}

#[test]
fn a_guardian_change_waits_and_the_current_guardian_stays_in_charge() {
    let mut sim = funded();
    sim.set_guardian(&alice(), Some(&carol())).unwrap();
    let view = sim.set_guardian(&alice(), Some(&mallory())).unwrap();
    assert_eq!(view.guardian, Some(carol()));
    let pending = view.pending_guardian.unwrap();
    assert_eq!(
        (pending.guardian, pending.effective_ms),
        (Some(mallory()), U64(START_MS + GUARDIAN_DELAY_MS))
    );
    assert_eq!(sim.event("guardian_changed")["effective_ms"], (START_MS + GUARDIAN_DELAY_MS).to_string());

    // A compromised session cannot swap the guardian and cancel-proof a payment in one go.
    sim.send(&alice(), &shop(), "near", NEAR).unwrap();
    assert_error(sim.guardian_cancel(&mallory(), 0), "E_NOT_GUARDIAN");
    sim.advance(GUARDIAN_DELAY_MS - 1);
    assert!(sim.guardian_cancel(&carol(), 0).is_ok());

    sim.advance(1);
    assert_eq!(guardian_of(&sim, &alice()), Some(mallory()));
    assert_eq!(sim.user(&alice()).unwrap().pending_guardian, None);
    sim.send(&alice(), &shop(), "near", NEAR).unwrap();
    assert_error(sim.guardian_cancel(&carol(), 1), "E_NOT_GUARDIAN");
    assert!(sim.guardian_cancel(&mallory(), 1).is_ok());
}

#[test]
fn removing_the_guardian_waits_too() {
    let mut sim = funded();
    sim.set_guardian(&alice(), Some(&carol())).unwrap();
    let view = sim.set_guardian(&alice(), None).unwrap();
    assert_eq!(view.guardian, Some(carol()));
    assert_eq!(view.pending_guardian.unwrap().guardian, None);
    assert_eq!(sim.event("guardian_changed")["guardian"], Value::Null);

    sim.send(&alice(), &shop(), "near", NEAR).unwrap();
    sim.advance(GUARDIAN_DELAY_MS - 1);
    assert!(sim.guardian_cancel(&carol(), 0).is_ok());
    sim.advance(1);
    assert_eq!(guardian_of(&sim, &alice()), None);
    sim.send(&alice(), &shop(), "near", NEAR).unwrap();
    assert_error(sim.guardian_cancel(&carol(), 1), "E_NOT_GUARDIAN");

    // With no guardian left, the next one takes effect at once again.
    assert_eq!(sim.set_guardian(&alice(), Some(&dave())).unwrap().guardian, Some(dave()));
    assert!(sim.guardian_cancel(&dave(), 1).is_ok());
}

#[test]
fn a_newer_change_replaces_the_pending_one_with_a_full_delay() {
    let mut sim = funded();
    sim.set_guardian(&alice(), Some(&carol())).unwrap();
    sim.set_guardian(&alice(), Some(&mallory())).unwrap();
    sim.advance(GUARDIAN_DELAY_MS / 2);
    let view = sim.set_guardian(&alice(), Some(&dave())).unwrap();
    let replaced_at = sim.now_ms;
    assert_eq!(view.pending_guardian.unwrap().effective_ms, U64(replaced_at + GUARDIAN_DELAY_MS));

    // Mallory's change never happens; Carol stays until Dave's full delay has passed.
    sim.now_ms = START_MS + GUARDIAN_DELAY_MS;
    assert_eq!(guardian_of(&sim, &alice()), Some(carol()));
    sim.now_ms = replaced_at + GUARDIAN_DELAY_MS - 1;
    assert_eq!(guardian_of(&sim, &alice()), Some(carol()));
    sim.now_ms = replaced_at + GUARDIAN_DELAY_MS;
    assert_eq!(guardian_of(&sim, &alice()), Some(dave()));
}

#[test]
fn a_guardian_only_guards_their_own_user() {
    let mut sim = funded();
    sim.register(&bob(), NEAR);
    sim.set_guardian(&alice(), Some(&carol())).unwrap();
    sim.send(&bob(), &shop(), "near", NEAR).unwrap();
    assert_error(sim.guardian_cancel(&carol(), 0), "E_NOT_GUARDIAN");
}

#[test]
fn guardian_rules() {
    let mut sim = funded();
    assert_error(sim.set_guardian(&alice(), Some(&alice())), "E_BAD_GUARDIAN");
    assert_error(sim.set_guardian(&alice(), Some(&vault())), "E_BAD_GUARDIAN");
    assert_error(sim.set_guardian(&mallory(), Some(&carol())), "E_NOT_REGISTERED");
    assert_eq!(guardian_of(&sim, &alice()), None);
}
