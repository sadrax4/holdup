use near_sdk::Gas;

use super::sending::funded;
use super::*;
use crate::{FT_STORAGE_DEPOSIT, GAS_FOR_FT_TRANSFER, GAS_FOR_ON_PAYOUT, GAS_FOR_STORAGE_DEPOSIT};

/// The single function call of a receipt: (method, args, deposit, gas, gas weight).
fn call_of(receipt: &Receipt) -> (String, Value, u128, Gas, u64) {
    match receipt.actions.as_slice() {
        [MockAction::FunctionCallWeight {
            method_name,
            args,
            attached_deposit,
            prepaid_gas,
            gas_weight,
            ..
        }] => (
            String::from_utf8(method_name.clone()).unwrap(),
            serde_json::from_slice(args).unwrap(),
            attached_deposit.as_yoctonear(),
            *prepaid_gas,
            gas_weight.0,
        ),
        actions => panic!("expected one function call, got {actions:?}"),
    }
}

#[test]
fn nobody_can_release_early() {
    let mut sim = funded();
    sim.send(&alice(), &shop(), "near", NEAR).unwrap();
    for caller in [mallory(), alice(), oracle(), shop()] {
        assert_error(sim.release(&caller, 0), "E_NOT_DUE");
    }
    sim.advance(HOLD_MS - 1);
    assert_error(sim.release(&mallory(), 0), "E_NOT_DUE");
    sim.advance(1);
    assert_eq!(sim.release(&mallory(), 0), Ok(None));
    assert_error(sim.release(&mallory(), 99), "E_HOLD_NOT_FOUND");
}

#[test]
fn release_to_a_vault_user_is_internal() {
    let mut sim = funded();
    sim.register(&bob(), 0);
    sim.send(&alice(), &bob(), "near", 3 * NEAR).unwrap();
    sim.advance(HOLD_MS);
    let released = sim.release(&mallory(), 0).unwrap().expect("settled at once");
    assert_eq!(released.status, HoldStatus::Released);
    assert_eq!(released.settled_ms, Some(U64(START_MS + HOLD_MS)));
    assert!(sim.receipts().is_empty(), "no money leaves the vault");
    assert_eq!(sim.near_of(&bob()), 3 * NEAR);
    assert_eq!(sim.near_of(&alice()), 17 * NEAR - HOLD_FEE);
    assert_eq!(sim.near_liabilities(), 20 * NEAR - HOLD_FEE);
    let event = sim.event("hold_released");
    assert_eq!(event["internal"], true);
    assert_eq!(event["payee"], "bob.holdup.near");
    assert_eq!(sim.stats(&bob()).released, 1);
    assert_error(sim.release(&mallory(), 0), "E_HOLD_NOT_ACTIVE");
    sim.assert_solvent();
}

#[test]
fn payee_that_registers_later_is_paid_internally() {
    let mut sim = funded();
    sim.send(&alice(), &shop(), husd().as_str(), 25 * HUSD).unwrap();
    sim.register(&shop(), 0);
    sim.advance(HOLD_MS);
    assert!(sim.release(&mallory(), 0).unwrap().is_some());
    assert_eq!(sim.husd_of(&shop()), 25 * HUSD);
    assert!(sim.receipts().is_empty());
}

#[test]
fn near_release_to_an_outside_account() {
    let mut sim = funded();
    sim.send(&alice(), &shop(), "near", 3 * NEAR).unwrap();
    sim.advance(HOLD_MS);
    let balance_before = sim.balance;
    assert_eq!(sim.release(&mallory(), 0), Ok(None));
    assert_eq!(sim.status(0), HoldStatus::Releasing);

    let receipts = sim.receipts();
    assert_eq!(receipts.len(), 2);
    assert_eq!(receipts[0].receiver_id, shop());
    assert!(matches!(
        receipts[0].actions.as_slice(),
        [MockAction::Transfer { deposit, .. }] if deposit.as_yoctonear() == 3 * NEAR
    ));
    assert_eq!(receipts[1].receiver_id, vault());
    let transfer_index = receipt_index(&receipts[0]);
    assert_eq!(receipts[1].receipt_indices, vec![transfer_index], "callback waits on the transfer only");
    let (method, args, deposit, gas, _) = call_of(&receipts[1]);
    assert_eq!((method.as_str(), deposit, gas), ("on_payout", 0, GAS_FOR_ON_PAYOUT));
    assert_eq!(args, serde_json::json!({ "hold_id": "0" }));

    // The amount left the account balance together with the liability.
    assert_eq!(sim.balance, balance_before - 3 * NEAR);
    assert_eq!(sim.near_liabilities(), 17 * NEAR - HOLD_FEE);
    sim.assert_solvent();
    assert_error(sim.release(&mallory(), 0), "E_HOLD_NOT_ACTIVE");

    sim.advance(1_000);
    let settled = sim.on_payout(0, ok()).unwrap();
    assert_eq!(settled.status, HoldStatus::Released);
    assert_eq!(settled.settled_ms, Some(U64(START_MS + HOLD_MS + 1_000)));
    assert_eq!(sim.event("hold_released")["internal"], false);
    assert_eq!(sim.stats(&shop()).released, 1);
    assert_eq!(sim.near_of(&alice()), 17 * NEAR - HOLD_FEE);
    assert_eq!(sim.view(|v| v.settlement_reserve), 0);
    sim.assert_solvent();
    assert_error(sim.on_payout(0, ok()), "E_HOLD_NOT_ACTIVE");
}

#[test]
fn failed_near_payout_refunds_the_sender() {
    let mut sim = funded();
    sim.send(&alice(), &shop(), "near", 3 * NEAR).unwrap();
    sim.advance(HOLD_MS);
    sim.release(&mallory(), 0).unwrap();
    let failed_hold = sim.on_payout(0, failed()).unwrap();
    assert_eq!(failed_hold.status, HoldStatus::Failed);
    assert!(failed_hold.settled_ms.is_some());
    assert_eq!(sim.near_of(&alice()), 20 * NEAR - HOLD_FEE);
    assert_eq!(sim.near_liabilities(), 20 * NEAR - HOLD_FEE);
    let event = sim.event("hold_failed");
    assert_eq!(event["refunded_to"], "alice.holdup.near");
    assert_eq!(event["amount"], (3 * NEAR).to_string());
    assert_eq!(sim.stats(&shop()).released, 0);
    // The chain returns the failed transfer's deposit to the vault.
    sim.refund(3 * NEAR);
    sim.assert_solvent();
    // The refunded money can be sent again.
    assert!(sim.send(&alice(), &carol(), "near", 20 * NEAR - 2 * HOLD_FEE).is_ok());
}

#[test]
fn token_release_registers_the_payee_then_transfers() {
    let mut sim = funded();
    sim.send(&alice(), &shop(), husd().as_str(), 50 * HUSD).unwrap();
    sim.advance(HOLD_MS);
    let (balance_before, reserve_before) = (sim.balance, sim.view(|v| v.settlement_reserve));
    assert_eq!(sim.release(&mallory(), 0), Ok(None));
    assert_eq!(sim.status(0), HoldStatus::Releasing);

    let receipts = sim.receipts();
    assert_eq!(receipts.len(), 3);
    assert!(receipts[..2].iter().all(|receipt| receipt.receiver_id == husd()));
    let (method, args, deposit, gas, weight) = call_of(&receipts[0]);
    assert_eq!(method, "storage_deposit");
    assert_eq!(args, serde_json::json!({ "account_id": "shop.near", "registration_only": true }));
    assert_eq!((deposit, gas, weight), (FT_STORAGE_DEPOSIT.as_yoctonear(), GAS_FOR_STORAGE_DEPOSIT, 0));

    let (method, args, deposit, gas, weight) = call_of(&receipts[1]);
    assert_eq!(method, "ft_transfer");
    assert_eq!(
        args,
        serde_json::json!({ "receiver_id": "shop.near", "amount": "50000000", "memo": "HoldUp hold 0" })
    );
    assert_eq!((deposit, gas, weight), (1, GAS_FOR_FT_TRANSFER, 0));
    assert_eq!(receipts[1].receipt_indices, vec![receipt_index(&receipts[0])]);

    // on_payout waits on ft_transfer alone, so its promise result 0 is the transfer.
    assert_eq!(receipts[2].receiver_id, vault());
    assert_eq!(receipts[2].receipt_indices, vec![receipt_index(&receipts[1])]);
    assert_eq!(call_of(&receipts[2]).0, "on_payout");

    // The deposits came out of the reserve set aside at send time; user money is untouched.
    let deposits = FT_STORAGE_DEPOSIT.as_yoctonear() + 1;
    assert_eq!(sim.balance, balance_before - deposits);
    assert_eq!(sim.view(|v| v.settlement_reserve), reserve_before - deposits);
    assert_eq!(sim.near_liabilities(), 20 * NEAR - HOLD_FEE);
    sim.assert_solvent();

    sim.on_payout(0, ok()).unwrap();
    assert_eq!(sim.status(0), HoldStatus::Released);
    assert_eq!(sim.husd_of(&alice()), 950 * HUSD);
    assert_eq!(sim.view(|v| v.settlement_reserve), 0);
    sim.assert_solvent();
}

#[test]
fn failed_token_payout_refunds_the_sender() {
    let mut sim = funded();
    sim.send(&alice(), &shop(), husd().as_str(), 50 * HUSD).unwrap();
    sim.advance(HOLD_MS);
    sim.release(&mallory(), 0).unwrap();
    assert_eq!(sim.on_payout(0, failed()).unwrap().status, HoldStatus::Failed);
    assert_eq!(sim.husd_of(&alice()), 1_000 * HUSD);
    assert_eq!(sim.near_liabilities(), 20 * NEAR - HOLD_FEE);
    assert_eq!(sim.event_names(), ["hold_failed"]);
    sim.assert_solvent();
}

#[test]
fn transfer_return_data_does_not_matter() {
    let mut sim = funded();
    sim.send(&alice(), &shop(), husd().as_str(), HUSD).unwrap();
    sim.advance(HOLD_MS);
    sim.release(&mallory(), 0).unwrap();
    let result = PromiseResult::Successful(b"\"unexpected\"".to_vec());
    assert_eq!(sim.on_payout(0, result).unwrap().status, HoldStatus::Released);
}

#[test]
fn payout_callback_needs_a_payout_in_flight() {
    let mut sim = funded();
    sim.send(&alice(), &shop(), "near", NEAR).unwrap();
    assert_error(sim.on_payout(0, ok()), "E_HOLD_NOT_ACTIVE");
    assert_error(sim.on_payout(5, ok()), "E_HOLD_NOT_FOUND");
    assert_eq!(sim.status(0), HoldStatus::Held);
}

#[test]
fn due_holds_are_listed_earliest_first() {
    let mut sim = funded();
    sim.send(&alice(), &shop(), "near", NEAR).unwrap(); // due at +10 min
    sim.advance(MINUTE_MS);
    sim.send(&alice(), &carol(), "near", NEAR).unwrap(); // due at +11 min
    sim.send(&alice(), &mallory(), "near", NEAR).unwrap(); // due at +11 min, braked below
    sim.extend(&oracle(), 2, START_MS + DAY_MS, 1, "{}").unwrap();
    let due_ids = |sim: &Sim, limit: Option<u32>| {
        sim.view(|v| v.get_due_holds(limit)).into_iter().map(|hold| hold.id.0).collect::<Vec<_>>()
    };
    assert!(due_ids(&sim, None).is_empty());
    sim.now_ms = START_MS + HOLD_MS;
    assert_eq!(due_ids(&sim, None), vec![0]);
    sim.now_ms = START_MS + HOLD_MS + MINUTE_MS;
    assert_eq!(due_ids(&sim, None), vec![0, 1]);
    assert_eq!(due_ids(&sim, Some(1)), vec![0]);
    sim.release(&mallory(), 0).unwrap();
    assert_eq!(due_ids(&sim, None), vec![1]);
    sim.now_ms = START_MS + DAY_MS;
    assert_eq!(due_ids(&sim, None), vec![1, 2]);
}
