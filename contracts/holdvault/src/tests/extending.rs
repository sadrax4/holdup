use super::sending::funded;
use super::*;

/// Canonical evidence JSON as `holdup.risk.evidence.build_evidence` produces it.
const EVIDENCE: &str = r#"{"amount":"5000000000000000000000000","asset":"near","decided_ms":1767225660000,"hold":0,"network":"sandbox","payee":"safe-wallet-77.testnet","prompts":{},"reasons":[{"bit":1,"citations":[],"detail":"Payee account is 2 hours old","model":null,"reason":"NEW_ACCOUNT","source":"rules"}],"steps":[],"v":1,"vault":"vault.holdup.near"}"#;
/// `sha256(EVIDENCE)`, computed independently with Python's hashlib.
const EVIDENCE_SHA256: &str = "2df194ce77a775129d9574ee7363e0d2ab9474439b859fecda29d5e31458e237";
const EMPTY_OBJECT_SHA256: &str = "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a";

/// Alice holds 5 NEAR for the shop (hold 0, due at START_MS + HOLD_MS).
fn held() -> Sim {
    let mut sim = funded();
    sim.send(&alice(), &shop(), "near", 5 * NEAR).unwrap();
    sim
}

#[test]
fn only_oracles_can_extend() {
    let mut sim = held();
    for caller in [alice(), shop(), mallory(), treasury(), vault(), husd()] {
        assert_error(sim.extend(&caller, 0, START_MS + HOUR_MS, 1, EVIDENCE), "E_NOT_ORACLE");
    }
    assert_eq!(sim.hold(0).release_at_ms, U64(START_MS + HOLD_MS));
    assert!(!sim.hold(0).flagged);
}

#[test]
fn every_configured_oracle_can_extend() {
    let oracles: Vec<AccountId> = (0..3).map(|i| account(&format!("oracle{i}.near"))).collect();
    let mut input = config();
    input.oracles = oracles.clone();
    let mut sim = Sim::try_init(input, DEFAULT_RESERVE).unwrap();
    sim.register(&alice(), NEAR);
    sim.send(&alice(), &shop(), "near", NEAR / 2).unwrap();
    for (i, oracle) in oracles.iter().enumerate() {
        let release_at_ms = START_MS + HOUR_MS * (i as u64 + 1);
        assert_eq!(sim.extend(oracle, 0, release_at_ms, 1, "{}").unwrap().release_at_ms, U64(release_at_ms));
    }
    assert_eq!(sim.hold(0).extensions, 3);
}

#[test]
fn extension_records_reasons_and_the_evidence_hash() {
    let mut sim = held();
    sim.advance(MINUTE_MS);
    let release_at_ms = START_MS + 24 * HOUR_MS;
    let extended = sim.extend(&oracle(), 0, release_at_ms, 0b1001, EVIDENCE).unwrap();
    assert_eq!(extended.release_at_ms, U64(release_at_ms));
    assert_eq!(extended.reason_bits, 0b1001);
    assert_eq!(extended.extensions, 1);
    assert!(extended.flagged);
    assert_eq!(extended.evidence_hash.as_deref(), Some(EVIDENCE_SHA256));
    assert_eq!(extended.status, HoldStatus::Held);

    let event = sim.event("hold_extended");
    assert_eq!(event["hold_id"], "0");
    assert_eq!(event["sender"], "alice.holdup.near");
    assert_eq!(event["payee"], "shop.near");
    assert_eq!(event["asset"], "near");
    assert_eq!(event["amount"], (5 * NEAR).to_string());
    assert_eq!(event["previous_release_at_ms"], (START_MS + HOLD_MS).to_string());
    assert_eq!(event["release_at_ms"], release_at_ms.to_string());
    assert_eq!(event["reason_bits"], 0b1001);
    assert_eq!(event["evidence_hash"], EVIDENCE_SHA256);
    assert_eq!(event["extensions"], 1);

    let stats = sim.stats(&shop());
    assert_eq!((stats.flagged, stats.last_flag_ms), (1, Some(U64(START_MS + MINUTE_MS))));

    // A second extension adds reasons and replaces the hash; the hold was already flagged.
    sim.advance(MINUTE_MS);
    let again = sim.extend(&oracle(), 0, START_MS + 48 * HOUR_MS, 1 << 12, "{}").unwrap();
    assert_eq!(again.reason_bits, 0b1001 | 1 << 12);
    assert_eq!(again.extensions, 2);
    assert_eq!(again.evidence_hash.as_deref(), Some(EMPTY_OBJECT_SHA256));
    let stats = sim.stats(&shop());
    assert_eq!((stats.flagged, stats.last_flag_ms), (1, Some(U64(START_MS + MINUTE_MS))));

    // Extending never moves money.
    assert_eq!(sim.near_of(&alice()), 15 * NEAR - HOLD_FEE);
    assert_eq!(sim.near_liabilities(), 20 * NEAR - HOLD_FEE);
    sim.assert_solvent();
}

#[test]
fn holds_only_get_longer() {
    let mut sim = held();
    let release_at_ms = START_MS + HOLD_MS;
    assert_error(sim.extend(&oracle(), 0, release_at_ms, 1, "{}"), "E_ONLY_LONGER");
    assert_error(sim.extend(&oracle(), 0, release_at_ms - 1, 1, "{}"), "E_ONLY_LONGER");
    assert_error(sim.extend(&oracle(), 0, 0, 1, "{}"), "E_ONLY_LONGER");
    sim.extend(&oracle(), 0, release_at_ms + 1, 1, "{}").unwrap();
    assert_error(sim.extend(&oracle(), 0, release_at_ms, 1, "{}"), "E_ONLY_LONGER");
}

#[test]
fn extensions_stop_at_the_max_hold() {
    let mut sim = held();
    let latest = START_MS + MAX_HOLD_MS;
    assert_error(sim.extend(&oracle(), 0, latest + 1, 1, "{}"), "E_ABOVE_MAX_HOLD");
    assert_error(sim.extend(&oracle(), 0, u64::MAX, 1, "{}"), "E_ABOVE_MAX_HOLD");
    sim.extend(&oracle(), 0, latest, 1, "{}").unwrap();
    // The cap is measured from creation, so waiting does not move it.
    sim.advance(10 * HOUR_MS);
    assert_error(sim.extend(&oracle(), 0, latest + 1, 1, "{}"), "E_ABOVE_MAX_HOLD");
    sim.now_ms = latest;
    assert!(sim.release(&mallory(), 0).is_ok());
}

#[test]
fn evidence_is_limited_to_4096_bytes() {
    let mut sim = held();
    let release_at_ms = START_MS + HOUR_MS;
    assert_error(sim.extend(&oracle(), 0, release_at_ms, 1, &"x".repeat(4097)), "E_EVIDENCE_TOO_LONG");
    // The limit counts bytes, not characters.
    let multibyte = "\u{e9}".repeat(2048);
    assert_error(sim.extend(&oracle(), 0, release_at_ms, 1, &format!("{multibyte}x")), "E_EVIDENCE_TOO_LONG");
    let extended = sim.extend(&oracle(), 0, release_at_ms, 1, &multibyte).unwrap();
    assert_eq!(
        extended.evidence_hash.as_deref(),
        Some("acd529f4c4a07a050b052f0f4a5ffb626d61852cca37d68efc46a8fee780b766")
    );
}

#[test]
fn a_reason_is_required() {
    let mut sim = held();
    assert_error(sim.extend(&oracle(), 0, START_MS + HOUR_MS, 0, EVIDENCE), "E_NO_REASON");
}

#[test]
fn missing_or_settled_holds_cannot_be_extended() {
    let mut sim = held();
    assert_error(sim.extend(&oracle(), 7, START_MS + HOUR_MS, 1, "{}"), "E_HOLD_NOT_FOUND");
    sim.cancel(&alice(), 0).unwrap();
    assert_error(sim.extend(&oracle(), 0, START_MS + HOUR_MS, 1, "{}"), "E_HOLD_NOT_ACTIVE");
}

#[test]
fn a_due_hold_can_still_be_braked_before_release() {
    let mut sim = held();
    sim.advance(HOLD_MS + 5 * MINUTE_MS);
    let release_at_ms = sim.now_ms + HOUR_MS;
    sim.extend(&oracle(), 0, release_at_ms, 1, "{}").unwrap();
    assert_error(sim.release(&mallory(), 0), "E_NOT_DUE");
    sim.now_ms = release_at_ms;
    assert!(sim.release(&mallory(), 0).is_ok());
}
