//! Randomized sequences of calls from users, strangers, the oracle and the keeper, with the
//! vault's invariants checked after every step.

use std::collections::BTreeSet;

use super::*;
use crate::{FT_STORAGE_DEPOSIT, HOLD_SETTLEMENT_BYTES};

/// xorshift64: tiny, deterministic, good enough to pick test actions.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }

    fn below(&mut self, bound: u64) -> u64 {
        self.next() % bound
    }

    fn chance(&mut self, percent: u64) -> bool {
        self.below(100) < percent
    }

    fn pick<T: Clone>(&mut self, items: &[T]) -> T {
        items[self.below(items.len() as u64) as usize].clone()
    }

    fn amount(&mut self, max: u128) -> u128 {
        if max == 0 {
            return 0;
        }
        u128::from(self.next()) * u128::from(self.next()) % max + 1
    }
}

struct World {
    sim: Sim,
    rng: Rng,
    users: Vec<AccountId>,
    everyone: Vec<AccountId>,
    /// Last observed view of every hold, indexed by id.
    seen: Vec<HoldView>,
    in_flight: Vec<u64>,
    husd_in: u128,
    husd_out: u128,
}

impl World {
    fn new(seed: u64) -> Self {
        let mut sim = Sim::new();
        let users = vec![alice(), bob(), carol()];
        for user in &users {
            sim.register(user, 30 * NEAR);
            sim.fund_husd(user, 500 * HUSD);
        }
        let mut everyone = users.clone();
        everyone.extend([shop(), mallory(), oracle()]);
        World {
            sim,
            rng: Rng(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1),
            users,
            everyone,
            seen: Vec::new(),
            in_flight: Vec::new(),
            husd_in: 3 * 500 * HUSD,
            husd_out: 0,
        }
    }

    fn asset(&mut self) -> String {
        if self.rng.chance(50) {
            "near".to_owned()
        } else {
            husd().to_string()
        }
    }

    fn some_hold(&mut self) -> u64 {
        let count = self.seen.len() as u64;
        if count == 0 || self.rng.chance(5) {
            count + self.rng.below(3)
        } else {
            self.rng.below(count)
        }
    }

    /// Runs one random action. Returns the hold id a successful `extend_hold` changed.
    fn step(&mut self) -> Option<u64> {
        match self.rng.below(100) {
            0..=6 => {
                let who = self.rng.pick(&self.users);
                let amount = self.rng.amount(5 * NEAR);
                self.sim.deposit_near(&treasury(), &who, amount).unwrap();
            }
            7..=11 => {
                let who = self.rng.pick(&self.everyone);
                let amount = self.rng.amount(200 * HUSD);
                if self.sim.ft_deposit(&husd(), &who, amount, "").unwrap() == 0 {
                    self.husd_in += amount;
                }
            }
            12..=36 => {
                let sender = if self.rng.chance(85) {
                    self.rng.pick(&self.users)
                } else {
                    self.rng.pick(&self.everyone)
                };
                let payee = self.rng.pick(&self.everyone);
                let asset = self.asset();
                let available =
                    if asset == "near" { self.sim.near_of(&sender) } else { self.sim.husd_of(&sender) };
                let amount = self.rng.amount(available + available / 10 + 2);
                if let Ok(hold) = self.sim.send(&sender, &payee, &asset, amount) {
                    if hold.status == HoldStatus::Releasing {
                        self.in_flight.push(hold.id.0);
                    }
                }
            }
            37..=51 => {
                let hold_id = self.some_hold();
                let caller = if self.rng.chance(85) { oracle() } else { self.rng.pick(&self.everyone) };
                let (created, current) = self
                    .seen
                    .get(hold_id as usize)
                    .map_or((START_MS, START_MS), |hold| (hold.created_ms.0, hold.release_at_ms.0));
                let release_at_ms = match self.rng.below(5) {
                    0 => created + MAX_HOLD_MS,
                    1 => created + MAX_HOLD_MS + 1 + self.rng.below(HOUR_MS),
                    2 => current.saturating_sub(self.rng.below(HOUR_MS)),
                    _ => current + 1 + self.rng.below(6 * HOUR_MS),
                };
                let bits = if self.rng.chance(10) { 0 } else { 1 << self.rng.below(17) };
                let evidence = "e".repeat(self.rng.below(4_200) as usize);
                if self.sim.extend(&caller, hold_id, release_at_ms, bits, &evidence).is_ok() {
                    return Some(hold_id);
                }
            }
            52..=59 => {
                let hold_id = self.some_hold();
                let caller = match self.seen.get(hold_id as usize) {
                    Some(hold) if self.rng.chance(70) => hold.sender.clone(),
                    _ => self.rng.pick(&self.everyone),
                };
                let _ = self.sim.cancel(&caller, hold_id);
            }
            60..=64 => {
                let hold_id = self.some_hold();
                let caller = self.rng.pick(&self.everyone);
                let _ = self.sim.guardian_cancel(&caller, hold_id);
            }
            65..=76 => {
                let hold_id = self.some_hold();
                let caller = self.rng.pick(&self.everyone);
                if let Ok(None) = self.sim.release(&caller, hold_id) {
                    self.in_flight.push(hold_id);
                }
            }
            77..=84 => {
                if self.in_flight.is_empty() {
                    return None;
                }
                let index = self.rng.below(self.in_flight.len() as u64) as usize;
                let hold_id = self.in_flight.swap_remove(index);
                let hold = self.sim.hold(hold_id);
                let near_refund = if hold.asset == "near" { hold.amount.0 } else { 0 };
                if self.rng.chance(60) {
                    self.sim.on_payout(hold_id, ok()).unwrap();
                    if hold.asset != "near" {
                        self.husd_out += hold.amount.0;
                    }
                } else if self.rng.chance(50) {
                    self.sim.refund(near_refund);
                    self.sim.on_payout(hold_id, failed()).unwrap();
                } else {
                    self.sim.on_payout(hold_id, failed()).unwrap();
                    self.sim.refund(near_refund);
                }
            }
            85..=88 => {
                let owner = self.rng.pick(&self.everyone);
                let payee = self.rng.pick(&self.everyone);
                let _ = self.sim.add_trusted(&owner, &payee);
            }
            89..=90 => {
                let owner = self.rng.pick(&self.everyone);
                let payee = self.rng.pick(&self.everyone);
                let _ = self.sim.remove_trusted(&owner, &payee);
            }
            91..=93 => {
                let owner = self.rng.pick(&self.everyone);
                if self.rng.chance(25) {
                    let guardian = self.rng.pick(&self.everyone);
                    let _ = self.sim.veto(&guardian, &owner);
                } else {
                    let guardian =
                        if self.rng.chance(20) { None } else { Some(self.rng.pick(&self.everyone)) };
                    let _ = self.sim.set_guardian(&owner, guardian.as_ref());
                }
            }
            _ => {
                let wait = match self.rng.below(4) {
                    0 => DAY_MS - self.sim.now_ms % DAY_MS - 1 + self.rng.below(3),
                    _ => self.rng.below(2 * HOUR_MS),
                };
                self.sim.advance(wait);
            }
        }
        None
    }

    fn check(&mut self, extended: Option<u64>) {
        let users = self.users.clone();
        let (holds, balances, active, active_set, liabilities, reserve, due) = self.sim.view(|v| {
            let holds: Vec<HoldView> = (0..v.next_hold_id).map(|id| v.get_hold(U64(id)).unwrap()).collect();
            let balances: Vec<BalanceView> = users.iter().map(|user| v.get_balance(user.clone())).collect();
            let active: Vec<Vec<u64>> = users
                .iter()
                .map(|user| v.get_active_holds(user.clone()).iter().map(|hold| hold.id.0).collect())
                .collect();
            let active_set: BTreeSet<u64> = v.active_holds.iter().copied().collect();
            let due: Vec<HoldView> = v.get_due_holds(Some(100));
            (holds, balances, active, active_set, v.near_liabilities, v.settlement_reserve, due)
        });
        let now_ms = self.sim.now_ms;

        for hold in &holds {
            let id = hold.id.0;
            assert!(hold.release_at_ms.0 <= hold.created_ms.0 + MAX_HOLD_MS, "hold {id} past the max hold");
            match self.seen.get(id as usize) {
                None => {
                    let expected = if hold.instant { hold.created_ms.0 } else { hold.created_ms.0 + HOLD_MS };
                    assert_eq!(hold.release_at_ms.0, expected, "new hold {id}");
                    assert!(
                        hold.status == HoldStatus::Held
                            || (hold.instant
                                && matches!(hold.status, HoldStatus::Released | HoldStatus::Releasing)),
                        "new hold {id} is {:?}",
                        hold.status
                    );
                }
                Some(before) => {
                    assert_eq!(
                        (&hold.sender, &hold.payee, &hold.asset, hold.amount, hold.created_ms, hold.instant),
                        (
                            &before.sender,
                            &before.payee,
                            &before.asset,
                            before.amount,
                            before.created_ms,
                            before.instant
                        ),
                        "hold {id} changed its terms"
                    );
                    assert!(hold.release_at_ms >= before.release_at_ms, "hold {id} got shorter");
                    if hold.release_at_ms != before.release_at_ms {
                        assert_eq!(extended, Some(id), "release_at of hold {id} changed outside extend_hold");
                    }
                    assert_eq!(hold.reason_bits & before.reason_bits, before.reason_bits);
                    assert!(
                        transition_allowed(before.status, hold.status),
                        "hold {id}: {:?} -> {:?}",
                        before.status,
                        hold.status
                    );
                    if before.status == HoldStatus::Held
                        && matches!(hold.status, HoldStatus::Releasing | HoldStatus::Released)
                    {
                        assert!(now_ms >= hold.release_at_ms.0, "hold {id} released early");
                    }
                }
            }
        }

        // NEAR owed to users = their balances + NEAR waiting in holds.
        let held_near: u128 = holds
            .iter()
            .filter(|hold| hold.status == HoldStatus::Held && hold.asset == "near")
            .map(|hold| hold.amount.0)
            .sum();
        let user_near: u128 = balances.iter().map(|balance| balance.near.0).sum();
        assert_eq!(liabilities, user_near + held_near, "NEAR liabilities");

        // Tokens in the vault = balances + held + paying out; nothing appears or disappears.
        let in_vault_husd: u128 = holds
            .iter()
            .filter(|hold| {
                hold.asset != "near" && matches!(hold.status, HoldStatus::Held | HoldStatus::Releasing)
            })
            .map(|hold| hold.amount.0)
            .sum();
        let user_husd: u128 = balances.iter().map(|balance| balance.tokens[&husd()].0).sum();
        assert_eq!(user_husd + in_vault_husd, self.husd_in - self.husd_out, "HUSD conservation");

        // The settlement reserve matches what open holds can still need.
        let storage = u128::from(HOLD_SETTLEMENT_BYTES) * env::storage_byte_cost().as_yoctonear();
        let token_deposits = FT_STORAGE_DEPOSIT.as_yoctonear() + 1;
        let expected_reserve: u128 = holds
            .iter()
            .map(|hold| match hold.status {
                HoldStatus::Held if hold.asset == "near" => storage,
                HoldStatus::Held => storage + token_deposits,
                HoldStatus::Releasing => storage,
                _ => 0,
            })
            .sum();
        assert_eq!(reserve, expected_reserve, "settlement reserve");
        self.sim.assert_solvent();

        // Active-hold indexes agree with hold statuses.
        let held: BTreeSet<u64> =
            holds.iter().filter(|hold| hold.status == HoldStatus::Held).map(|hold| hold.id.0).collect();
        assert_eq!(active_set, held, "global active set");
        for (user, ids) in self.users.iter().zip(&active) {
            let expected: Vec<u64> = holds
                .iter()
                .filter(|hold| hold.status == HoldStatus::Held && &hold.sender == user)
                .map(|hold| hold.id.0)
                .collect();
            assert_eq!(ids, &expected, "active holds of {user}");
        }
        for hold in &due {
            assert!(hold.status == HoldStatus::Held && hold.release_at_ms.0 <= now_ms);
        }
        let due_count =
            holds.iter().filter(|h| h.status == HoldStatus::Held && h.release_at_ms.0 <= now_ms).count();
        assert_eq!(due.len(), due_count.min(100));

        self.seen = holds;
    }
}

fn transition_allowed(from: HoldStatus, to: HoldStatus) -> bool {
    use HoldStatus::*;
    from == to
        || matches!(
            (from, to),
            (Held, Releasing)
                | (Held, Released)
                | (Held, Cancelled)
                | (Releasing, Released)
                | (Releasing, Failed)
        )
}

#[test]
fn random_operations_keep_every_invariant() {
    for seed in 1..=12 {
        let mut world = World::new(seed);
        world.check(None);
        for _ in 0..400 {
            let extended = world.step();
            world.check(extended);
        }
        assert!(world.seen.len() > 20, "seed {seed} created only {} holds", world.seen.len());
        let statuses: BTreeSet<String> = world.seen.iter().map(|hold| format!("{:?}", hold.status)).collect();
        assert!(statuses.len() >= 4, "seed {seed} reached only {statuses:?}");
    }
}
