use super::*;
use crate::config::MAX_DURATION_MS;

#[test]
fn new_fixes_the_configuration() {
    let sim = Sim::new();
    let view = sim.view(|v| v.get_config());
    assert_eq!(view.version, "1.0.0");
    assert_eq!(view.oracles, vec![oracle()]);
    assert_eq!(view.tokens, vec![husd()]);
    assert_eq!(view.new_payee_hold_ms, U64(HOLD_MS));
    assert_eq!(view.max_hold_ms, U64(MAX_HOLD_MS));
    assert_eq!(view.trust_delay_ms, U64(TRUST_DELAY_MS));
    assert_eq!(view.guardian_delay_ms, U64(GUARDIAN_DELAY_MS));
    assert_eq!(view.instant_caps["near"], U128(10 * NEAR));
    assert_eq!(view.instant_caps[husd().as_str()], U128(100 * HUSD));
    assert_eq!(view.max_active_holds, 10);
    assert_eq!(view.registration_fee, U128(REGISTRATION_FEE));
}

#[test]
fn new_rejects_invalid_configurations() {
    type Mutation = fn(&mut ConfigInput);
    let cases: [(&str, Mutation); 14] = [
        ("no oracle", |c| c.oracles.clear()),
        ("four oracles", |c| {
            c.oracles = (0..4).map(|i| account(&format!("oracle{i}.near"))).collect();
        }),
        ("duplicate oracle", |c| c.oracles = vec![oracle(), oracle()]),
        ("vault as oracle", |c| c.oracles = vec![vault()]),
        ("five tokens", |c| c.tokens = (0..5).map(|i| account(&format!("token{i}.near"))).collect()),
        ("duplicate token", |c| c.tokens = vec![husd(), husd()]),
        ("token named like the native asset", |c| c.tokens = vec![account("near")]),
        ("vault as token", |c| c.tokens = vec![vault()]),
        ("zero default hold", |c| c.new_payee_hold_ms = U64(0)),
        ("max hold below default hold", |c| c.max_hold_ms = U64(HOLD_MS - 1)),
        ("trust delay above a year", |c| c.trust_delay_ms = U64(MAX_DURATION_MS + 1)),
        ("cap for an asset that is not accepted", |c| {
            c.instant_caps.insert("usdc.near".to_owned(), U128(1));
        }),
        ("zero max active holds", |c| c.max_active_holds = 0),
        ("max active holds above the limit", |c| c.max_active_holds = 101),
    ];
    for (name, mutate) in cases {
        let mut input = config();
        mutate(&mut input);
        match Sim::try_init(input, DEFAULT_RESERVE) {
            Ok(_) => panic!("{name}: config was accepted"),
            Err(message) => assert!(message.contains("E_BAD_CONFIG:"), "{name}: {message}"),
        }
    }
}

#[test]
fn only_the_vault_or_its_deployer_can_initialize() {
    assert_error(Sim::try_init_as(&mallory(), config(), DEFAULT_RESERVE).map(|_| ()), "E_BAD_CONFIG");
    assert_error(Sim::try_init_as(&alice(), config(), DEFAULT_RESERVE).map(|_| ()), "E_BAD_CONFIG");
    // The vault is `vault.holdup.near`: its parent deploys and initializes it in one transaction.
    assert!(Sim::try_init_as(&treasury(), config(), DEFAULT_RESERVE).is_ok());
    assert!(Sim::try_init_as(&vault(), config(), DEFAULT_RESERVE).is_ok());
}

#[test]
fn edge_values_are_accepted() {
    let mut input = config();
    input.oracles = (0..3).map(|i| account(&format!("oracle{i}.near"))).collect();
    input.tokens = (0..4).map(|i| account(&format!("token{i}.near"))).collect();
    input.instant_caps.clear();
    input.max_hold_ms = U64(HOLD_MS);
    input.max_active_holds = 100;
    input.registration_fee = U128(0);
    assert!(Sim::try_init(input, DEFAULT_RESERVE).is_ok());
}

#[test]
fn config_json_rejects_unknown_fields() {
    let json = r#"{"oracles":["o.near"],"tokens":[],"new_payee_hold_ms":"1","max_hold_ms":"1",
        "trust_delay_ms":"0","guardian_delay_ms":"0","instant_caps":{},"max_active_holds":1,
        "registration_fee":"0","owner":"evil.near"}"#;
    assert!(serde_json::from_str::<ConfigInput>(json).is_err());
    let valid = json.replace(r#","owner":"evil.near""#, "");
    assert!(serde_json::from_str::<ConfigInput>(&valid).is_ok());
}
