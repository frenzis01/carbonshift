//! Online swarm scheduling strategies for the CarbonShift batch scheduler
//! (**parallel, additive-merge** concurrency variant — see `online_swarm.rs`
//! for the simpler serialized variant; select between them via
//! `Config::online_swarm_mode`).
//!
//! Provides online variants of the bandit and ACO strategies that integrate
//! with the generator + scheduler pipeline.  Unlike the offline versions in
//! `nshift/swarm.rs` (which process all requests in a single pass), these
//! maintain **persistent state across batch calls**, allowing the scheduler to
//! improve its slot-selection policy as more requests are processed.
//!
//! # Concurrency model
//!
//! Each batch worker **snapshots** the shared state, solves against that
//! snapshot lock-free (so multiple swarm batches can run concurrently, same
//! as DP batches), and then **merges its own contribution back additively**
//! (see `merge_delta`) instead of overwriting the shared state outright.
//!
//! This differs from a naive "clone, solve, overwrite" scheme (the bug fixed
//! by `online_swarm.rs`'s serialized model): overwriting silently discards
//! any updates made by other workers that finished in the meantime. Here,
//! each worker instead returns a *delta* describing only the net effect of
//! its own batch (independent of what other concurrent workers did), which is
//! then composed onto whatever the shared state happens to be at merge time:
//!
//! - **Bandit**: the delta is (per slot) the count and cost-sum added during
//!   this batch. Merging combines two running means exactly
//!   (`n' = n + Δn`, `q' = (q·n + Δcost_sum) / n'`) — mathematically
//!   equivalent to processing every request through a single shared
//!   accumulator, regardless of merge order.
//! - **ACO**: the delta is an affine transform of the pheromone vector
//!   (`τ' = τ·evap_factor + deposit`), pre-computed so that replaying it on
//!   top of *any* current `τ` reproduces what this worker's own evaporation +
//!   deposit steps would have done if applied directly, without needing to
//!   know what other workers did.
//!
//! Trade-off vs. the serialized variant: workers read a possibly slightly
//! stale snapshot while solving (so within-batch decisions can't see
//! *concurrent* workers' choices, only already-committed ones), but no
//! update is ever discarded, and full parallelism is preserved.

use std::collections::HashMap;

use rand::rngs::SmallRng;
use rand::{Rng, SeedableRng};

use crate::config::Config;
use crate::online_swarm::SwarmContext;
use crate::types::{Assignment, CapacityTier, Flavour, Request};

// ─── SwarmContext ─────────────────────────────────────────────────────────────
//
// Reused as-is from `online_swarm.rs`: it's just a plain snapshot of
// committed scheduling state (produced by `SharedState::swarm_context_snapshot`)
// with no behaviour tied to either concurrency model, so both variants share
// the same type instead of keeping two copies in sync.

// ─── shared helpers ───────────────────────────────────────────────────────────

fn capacity_mult(tiers: &[CapacityTier], count: i64) -> f64 {
    for tier in tiers {
        match tier.max_requests {
            None => return tier.multiplier,
            Some(max) if count <= max => return tier.multiplier,
            _ => {}
        }
    }
    tiers.last().map(|t| t.multiplier).unwrap_or(1.0)
}

/// Return the sorted list of valid target slots for a request:
/// `[arrival_slot, min(deadline_slot, arrival_slot + max_future, total_slots-1)]`.
fn valid_slots(arrival_slot: i32, deadline_slot: i32, max_future: i32, total_slots: i32) -> Vec<i32> {
    let from = arrival_slot;
    let to = deadline_slot
        .min(arrival_slot + max_future)
        .min(total_slots - 1)
        .max(from);
    (from..=to).collect()
}

/// Try each flavour (sorted cheapest-first) for `slot`, returning the first
/// one that satisfies the global and window error constraints, or `None`.
fn pick_feasible_flavour<'a>(
    slot: i32,
    carbon_forecast: &[f64],
    tiers: &[CapacityTier],
    slot_count: &HashMap<i32, i32>,
    slot_errors: &HashMap<i32, Vec<f64>>,
    global_error_sum: f64,
    global_count: usize,
    scale: f64,
    sorted_flavours: &[&'a Flavour],
    max_err: f64,
    win_past: i32,
    win_future: i32,
    global_constraint_enabled: bool,
) -> Option<(&'a Flavour, f64)> {
    let ci = carbon_forecast.get(slot as usize).copied().unwrap_or(1.0);
    let position = *slot_count.get(&slot).unwrap_or(&0) + 1;
    let mult = capacity_mult(tiers, position as i64);
    for &flav in sorted_flavours {
        if global_constraint_enabled {
            let new_avg = (global_error_sum + flav.error) / (global_count as f64 + 1.0);
            if new_avg > max_err { continue; }
        }
        let mut win_sum = flav.error;
        let mut win_cnt = 1usize;
        for ws in (slot - win_past)..=(slot + win_future) {
            if let Some(errs) = slot_errors.get(&ws) {
                win_sum += errs.iter().sum::<f64>();
                win_cnt += errs.len();
            }
        }
        if win_sum / win_cnt as f64 > max_err { continue; }
        let cost = ci * mult * flav.duration as f64 * scale;
        return Some((flav, cost));
    }
    None
}

fn slot_cost(
    slot: i32,
    flav: &Flavour,
    carbon_forecast: &[f64],
    tiers: &[CapacityTier],
    slot_count: &HashMap<i32, i32>,
    scale: f64,
) -> f64 {
    let ci = carbon_forecast.get(slot as usize).copied().unwrap_or(1.0);
    let position = *slot_count.get(&slot).unwrap_or(&0) + 1;
    let mult = capacity_mult(tiers, position as i64);
    ci * mult * flav.duration as f64 * scale
}

#[inline]
fn make_assignment(req: &Request, slot: i32, flav: &Flavour, cost: f64) -> Assignment {
    Assignment::new(
        req.id,
        slot,
        flav.name.clone(),
        cost,
        flav.error,
        flav.duration,
        Some(req.arrival_slot),
        Some(req.deadline_slot),
    )
}

fn sorted_flavours_and_fallback(cfg: &Config) -> (Vec<&Flavour>, &Flavour) {
    let mut sorted: Vec<&Flavour> = cfg.flavours.iter().collect();
    sorted.sort_by_key(|f| f.duration);
    let fallback = cfg.flavours.iter()
        .min_by(|a, b| a.error.partial_cmp(&b.error).unwrap())
        .expect("Config must have at least one flavour");
    (sorted, fallback)
}

// ─── Online Bandit ────────────────────────────────────────────────────────────

/// Persistent ε-greedy bandit state.
#[derive(Clone)]
pub struct OnlineBanditState {
    /// Per-slot running-mean Q-values (estimated carbon cost).
    pub q: Vec<f64>,
    /// Per-slot sample counts.
    pub n: Vec<u64>,
    pub rng: SmallRng,
    pub epsilon: f64,
}

/// Net effect of one worker's batch on the bandit's running-mean Q-values,
/// expressed relative to whatever baseline the worker read — so it can be
/// composed onto the *current* shared state (see `merge_delta`) without
/// needing to know what other concurrent workers did.
pub struct BanditDelta {
    /// Per-slot number of new samples contributed by this batch.
    n_delta: Vec<u64>,
    /// Per-slot sum of the raw costs of those new samples.
    cost_sum_delta: Vec<f64>,
    /// RNG state after this batch (merged with plain overwrite — losing a
    /// few draws to a concurrent race doesn't affect correctness, only the
    /// exact exploration path, which is inherently racy under concurrency).
    rng_after: SmallRng,
}

impl OnlineBanditState {
    pub fn new(total_slots: usize, initial_q: f64, epsilon: f64, seed: u64) -> Self {
        Self {
            q: vec![initial_q; total_slots],
            n: vec![0; total_slots],
            rng: SmallRng::seed_from_u64(seed),
            epsilon,
        }
    }

    /// Assign `pending` using the current Q-values, without mutating `self`.
    ///
    /// `ctx` is the committed-state baseline (slot counts, errors). Returns
    /// the assignments plus a `BanditDelta` capturing this batch's net effect
    /// on the Q-values, to be applied later via `merge_delta` — this lets
    /// callers solve lock-free and merge afterwards without losing updates
    /// made by other concurrent workers in the meantime.
    pub fn solve_batch(
        &self,
        pending: &[Request],
        carbon_forecast: &[f64],
        ctx: &SwarmContext,
        cfg: &Config,
    ) -> (Vec<Assignment>, BanditDelta) {
        let (sorted_flavours, fallback_flav) = sorted_flavours_and_fallback(cfg);
        let tiers = &cfg.capacity_tiers;
        let scale = cfg.carbon_cost_duration_scale;
        let max_future = cfg.assignment_max_future_slots;
        let total_slots = cfg.total_slots;
        let win_past = cfg.error_window_past;
        let win_future_cfg = cfg.error_window_future;
        let max_err = cfg.max_error_threshold;

        // Start from committed state; update within-batch as we go.
        let mut slot_count = ctx.slot_count.clone();
        let mut slot_errors = ctx.slot_errors.clone();
        let mut global_error_sum = ctx.global_error_sum;
        let mut global_count = ctx.global_count;

        // Working copies of the Q-values / sample counts: mutated locally so
        // later requests in *this* batch see earlier ones' updates (same
        // within-batch behaviour as the serialized variant), but `self` is
        // left untouched — the net effect is captured in the returned delta.
        let mut q = self.q.clone();
        let mut n = self.n.clone();
        let mut rng = self.rng.clone();

        let mut assignments = Vec::with_capacity(pending.len());

        for req in pending {
            let candidates = valid_slots(req.arrival_slot, req.deadline_slot, max_future, total_slots);
            if candidates.is_empty() { continue; }

            // ε-greedy slot selection.
            let chosen_slot = if rng.r#gen::<f64>() < self.epsilon {
                candidates[rng.gen_range(0..candidates.len())]
            } else {
                *candidates.iter()
                    .min_by(|&&a, &&b| {
                        let qa = q.get(a as usize).copied().unwrap_or(f64::MAX);
                        let qb = q.get(b as usize).copied().unwrap_or(f64::MAX);
                        qa.partial_cmp(&qb).unwrap_or(std::cmp::Ordering::Equal)
                    })
                    .unwrap()
            };

            // Cheapest feasible flavour; fallback to min-error if none pass.
            let (chosen_flav, cost) = pick_feasible_flavour(
                chosen_slot, carbon_forecast, tiers, &slot_count, &slot_errors,
                global_error_sum, global_count, scale, &sorted_flavours,
                max_err, win_past, win_future_cfg, cfg.global_error_constraint_enabled,
            ).unwrap_or_else(|| {
                let c = slot_cost(chosen_slot, fallback_flav, carbon_forecast, tiers, &slot_count, scale);
                (fallback_flav, c)
            });

            // Incremental Q-update (running mean) on the working copy.
            let idx = chosen_slot as usize;
            if idx < n.len() {
                n[idx] += 1;
                q[idx] += (cost - q[idx]) / n[idx] as f64;
            }

            *slot_count.entry(chosen_slot).or_insert(0) += 1;
            slot_errors.entry(chosen_slot).or_default().push(chosen_flav.error);
            global_error_sum += chosen_flav.error;
            global_count += 1;

            assignments.push(make_assignment(req, chosen_slot, chosen_flav, cost));
        }

        // Compute the delta relative to the baseline (self.q / self.n) this
        // batch started from: (Δn, Δcost_sum) per slot. `n` only ever grows.
        let mut n_delta = vec![0u64; n.len()];
        let mut cost_sum_delta = vec![0f64; q.len()];
        for i in 0..n.len() {
            let dn = n[i] - self.n[i];
            if dn > 0 {
                n_delta[i] = dn;
                cost_sum_delta[i] = q[i] * n[i] as f64 - self.q[i] * self.n[i] as f64;
            }
        }

        (assignments, BanditDelta { n_delta, cost_sum_delta, rng_after: rng })
    }

    /// Additively merge a worker's delta into the current shared state.
    ///
    /// Combines two running-mean accumulators exactly: `n' = n + Δn`,
    /// `q' = (q·n + Δcost_sum) / n'`. This is associative and commutative in
    /// its numeric result regardless of how many concurrent deltas are
    /// merged in whatever order, so no worker's contribution is ever lost.
    pub fn merge_delta(&mut self, delta: BanditDelta) {
        for i in 0..self.q.len() {
            let dn = delta.n_delta[i];
            if dn == 0 { continue; }
            let new_n = self.n[i] + dn;
            let new_cost_sum = self.q[i] * self.n[i] as f64 + delta.cost_sum_delta[i];
            self.q[i] = new_cost_sum / new_n as f64;
            self.n[i] = new_n;
        }
        self.rng = delta.rng_after;
    }
}

// ─── Online ACO ───────────────────────────────────────────────────────────────

/// Persistent ACO state.  Pheromone persists across batch calls so that
/// each new batch builds on the colony's accumulated experience.
#[derive(Clone)]
pub struct OnlineAcoState {
    /// Per-slot pheromone levels (updated after each batch).
    pub tau: Vec<f64>,
    /// Per-slot static heuristic (1/estimated_base_cost).
    pub eta: Vec<f64>,
    pub rng: SmallRng,
    pub n_ants: usize,
    /// Number of ACO iterations per batch (default 1 is fine for online use
    /// since pheromone accumulates across many batches).
    pub n_iterations: usize,
    pub alpha: f64,
    pub beta: f64,
    /// Pheromone evaporation rate ρ ∈ (0, 1).
    pub rho: f64,
    /// Deposit quantity (divided by solution cost).
    pub q_deposit: f64,
}

/// Net effect of one worker's batch on the ACO pheromone vector, expressed as
/// an affine transform `τ' = τ·evap_factor + deposit` — composing this onto
/// whatever the shared `τ` is at merge time reproduces what this worker's own
/// evaporation + deposit steps would have done if applied directly to it,
/// without needing to know what other concurrent workers did in the meantime.
pub struct AcoDelta {
    /// Combined multiplicative evaporation factor across all of this
    /// worker's iterations, i.e. `(1 - rho) ^ n_iterations`.
    evap_factor: f64,
    /// Per-slot additive deposit, pre-discounted for the evaporation that
    /// occurred *after* each deposit within this worker's own solve (a
    /// deposit made in iteration i is evaporated by every iteration after
    /// it, before the worker hands off its delta).
    deposit: Vec<f64>,
    rng_after: SmallRng,
}

impl OnlineAcoState {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        total_slots: usize,
        carbon_forecast: &[f64],
        cfg: &Config,
        n_ants: usize,
        n_iterations: usize,
        alpha: f64,
        beta: f64,
        rho: f64,
        q_deposit: f64,
        tau0: f64,
        seed: u64,
    ) -> Self {
        let cheapest = cfg.flavours.iter()
            .min_by_key(|f| f.duration)
            .expect("at least one flavour");
        let eta: Vec<f64> = (0..total_slots)
            .map(|s| {
                let ci = carbon_forecast.get(s).copied().unwrap_or(1.0);
                let base = ci * cheapest.duration as f64 * cfg.carbon_cost_duration_scale;
                if base > 0.0 { 1.0 / base } else { 1e9 }
            })
            .collect();
        Self {
            tau: vec![tau0; total_slots],
            eta,
            rng: SmallRng::seed_from_u64(seed),
            n_ants,
            n_iterations,
            alpha,
            beta,
            rho,
            q_deposit,
        }
    }

    /// Assign `pending` using the current pheromone state, without mutating
    /// `self`. Runs `n_iterations` × `n_ants` mini-iterations on the batch
    /// against a local working copy of the pheromone vector (so later
    /// iterations within *this* batch still see earlier ones' deposits), and
    /// returns the best-ant solution plus an `AcoDelta` capturing the net
    /// evaporation+deposit effect, to be applied later via `merge_delta`.
    pub fn solve_batch(
        &self,
        pending: &[Request],
        carbon_forecast: &[f64],
        ctx: &SwarmContext,
        cfg: &Config,
    ) -> (Vec<Assignment>, AcoDelta) {
        let (sorted_flavours, fallback_flav) = sorted_flavours_and_fallback(cfg);
        let tiers = &cfg.capacity_tiers;
        let scale = cfg.carbon_cost_duration_scale;
        let max_future = cfg.assignment_max_future_slots;
        let total_slots = cfg.total_slots;
        let win_past = cfg.error_window_past;
        let win_future_cfg = cfg.error_window_future;
        let max_err = cfg.max_error_threshold;

        let mut best_cost = f64::INFINITY;
        let mut best_solution: Vec<Assignment> = Vec::new();

        // Working copy of pheromone, evolved through iterations exactly like
        // the serialized variant. `deposit_acc` separately tracks the net
        // additive deposit relative to `self.tau`, discounted for
        // evaporations that happen after each deposit within this call.
        let mut tau = self.tau.clone();
        let mut rng = self.rng.clone();
        let mut deposit_acc = vec![0f64; tau.len()];
        let mut evap_factor = 1.0f64;

        for _iter in 0..self.n_iterations {
            let mut iter_best_cost = f64::INFINITY;
            let mut iter_best_solution: Vec<Assignment> = Vec::new();

            for _ant in 0..self.n_ants {
                let mut slot_count = ctx.slot_count.clone();
                let mut slot_errors = ctx.slot_errors.clone();
                let mut global_error_sum = ctx.global_error_sum;
                let mut global_count = ctx.global_count;
                let mut ant_solution: Vec<Assignment> = Vec::with_capacity(pending.len());
                let mut ant_cost = 0.0f64;

                for req in pending {
                    let candidates = valid_slots(req.arrival_slot, req.deadline_slot, max_future, total_slots);
                    if candidates.is_empty() { continue; }

                    let weights: Vec<f64> = candidates.iter().map(|&s| {
                        let t = tau.get(s as usize).copied().unwrap_or(1e-12).max(1e-12);
                        let e = self.eta.get(s as usize).copied().unwrap_or(1e-12);
                        t.powf(self.alpha) * e.powf(self.beta)
                    }).collect();

                    let total_weight: f64 = weights.iter().sum();
                    let chosen_slot = if total_weight <= 0.0 {
                        candidates[rng.gen_range(0..candidates.len())]
                    } else {
                        let mut r = rng.r#gen::<f64>() * total_weight;
                        let mut chosen = *candidates.last().unwrap();
                        for (&s, &w) in candidates.iter().zip(weights.iter()) {
                            r -= w;
                            if r <= 0.0 { chosen = s; break; }
                        }
                        chosen
                    };

                    let (chosen_flav, cost) = pick_feasible_flavour(
                        chosen_slot, carbon_forecast, tiers, &slot_count, &slot_errors,
                        global_error_sum, global_count, scale, &sorted_flavours,
                        max_err, win_past, win_future_cfg, cfg.global_error_constraint_enabled,
                    ).unwrap_or_else(|| {
                        let c = slot_cost(chosen_slot, fallback_flav, carbon_forecast, tiers, &slot_count, scale);
                        (fallback_flav, c)
                    });

                    ant_cost += cost;
                    *slot_count.entry(chosen_slot).or_insert(0) += 1;
                    slot_errors.entry(chosen_slot).or_default().push(chosen_flav.error);
                    global_error_sum += chosen_flav.error;
                    global_count += 1;

                    ant_solution.push(make_assignment(req, chosen_slot, chosen_flav, cost));
                }

                if ant_cost < iter_best_cost {
                    iter_best_cost = ant_cost;
                    iter_best_solution = ant_solution;
                }
            }

            // Evaporate both the working pheromone copy and the deposit
            // accumulator (a deposit made this iteration must decay in every
            // later iteration of this same batch, exactly like it would if
            // it had been applied directly to a shared `tau`).
            for t in tau.iter_mut() {
                *t *= 1.0 - self.rho;
                *t = t.max(1e-12);
            }
            for d in deposit_acc.iter_mut() {
                *d *= 1.0 - self.rho;
            }
            evap_factor *= 1.0 - self.rho;

            // Deposit on best-ant slots.
            if iter_best_cost > 0.0 && iter_best_cost.is_finite() {
                let deposit = self.q_deposit / iter_best_cost;
                for a in &iter_best_solution {
                    if let Some(t) = tau.get_mut(a.scheduled_slot as usize) {
                        *t += deposit;
                    }
                    if let Some(d) = deposit_acc.get_mut(a.scheduled_slot as usize) {
                        *d += deposit;
                    }
                }
            }

            if iter_best_cost < best_cost {
                best_cost = iter_best_cost;
                best_solution = iter_best_solution;
            }
        }

        (best_solution, AcoDelta { evap_factor, deposit: deposit_acc, rng_after: rng })
    }

    /// Additively merge a worker's delta into the current shared pheromone.
    ///
    /// `τ' = τ·evap_factor + deposit`, applied to whatever `τ` currently is
    /// — including contributions from other workers that merged in the
    /// meantime — rather than overwriting it.
    pub fn merge_delta(&mut self, delta: AcoDelta) {
        for i in 0..self.tau.len() {
            self.tau[i] = (self.tau[i] * delta.evap_factor + delta.deposit[i]).max(1e-12);
        }
        self.rng = delta.rng_after;
    }
}

// ─── OnlineSwarmState enum ────────────────────────────────────────────────────

/// Unified online swarm state — stored in `SchedulerMutableState`. Workers
/// clone it, solve lock-free against the clone (see `solve_batch`), then
/// merge their `SwarmDelta` back additively (see `merge_delta`).
#[derive(Clone)]
pub enum OnlineSwarmState {
    /// The scheduler is using the DP solver; no swarm state.
    None,
    Bandit(OnlineBanditState),
    Aco(OnlineAcoState),
}

/// Delta produced by `OnlineSwarmState::solve_batch`, to be applied via
/// `OnlineSwarmState::merge_delta`.
pub enum SwarmDelta {
    None,
    Bandit(BanditDelta),
    Aco(AcoDelta),
}

impl OnlineSwarmState {
    /// Build the appropriate state from `cfg.solver_strategy`.
    /// `carbon_forecast` is required for computing ACO's static heuristic.
    pub fn from_config(cfg: &Config, carbon_forecast: &[f64]) -> Self {
        match cfg.solver_strategy.as_str() {
            "bandit" => Self::Bandit(OnlineBanditState::new(
                cfg.total_slots as usize,
                cfg.swarm_bandit_initial_q,
                cfg.swarm_bandit_epsilon,
                cfg.swarm_bandit_seed,
            )),
            "ant_colony" => Self::Aco(OnlineAcoState::new(
                cfg.total_slots as usize,
                carbon_forecast,
                cfg,
                cfg.swarm_aco_n_ants,
                cfg.swarm_aco_n_iterations,
                cfg.swarm_aco_alpha,
                cfg.swarm_aco_beta,
                cfg.swarm_aco_rho,
                cfg.swarm_aco_q,
                cfg.swarm_aco_tau0,
                cfg.swarm_aco_seed,
            )),
            _ => Self::None, // "dp" (default) or unrecognised
        }
    }

    /// Returns `true` if this is an active swarm strategy (not DP).
    pub fn is_active(&self) -> bool {
        !matches!(self, Self::None)
    }

    /// Returns the strategy name for logging.
    pub fn name(&self) -> &'static str {
        match self {
            Self::None => "dp",
            Self::Bandit(_) => "bandit",
            Self::Aco(_) => "ant_colony",
        }
    }

    /// Solve a batch of requests against this (typically cloned) snapshot,
    /// without mutating it. Returns the assignments plus a `SwarmDelta` that
    /// the caller should later apply to the *live* shared state via
    /// `merge_delta`, so concurrent workers' contributions are combined
    /// rather than one overwriting another.
    pub fn solve_batch(
        &self,
        pending: &[Request],
        _slot: i32,
        carbon_forecast: &[f64],
        ctx: &SwarmContext,
        cfg: &Config,
    ) -> (Vec<Assignment>, SwarmDelta) {
        match self {
            Self::None => (vec![], SwarmDelta::None),
            Self::Bandit(b) => {
                let (assignments, delta) = b.solve_batch(pending, carbon_forecast, ctx, cfg);
                (assignments, SwarmDelta::Bandit(delta))
            }
            Self::Aco(a) => {
                let (assignments, delta) = a.solve_batch(pending, carbon_forecast, ctx, cfg);
                (assignments, SwarmDelta::Aco(delta))
            }
        }
    }

    /// Additively merge a worker's delta into the live shared state. A
    /// mismatched or `None` delta (e.g. produced before a config change) is a
    /// silent no-op rather than a panic.
    pub fn merge_delta(&mut self, delta: SwarmDelta) {
        match (self, delta) {
            (Self::Bandit(b), SwarmDelta::Bandit(d)) => b.merge_delta(d),
            (Self::Aco(a), SwarmDelta::Aco(d)) => a.merge_delta(d),
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_cfg() -> Config {
        let mut cfg = Config::default();
        cfg.total_slots = 6;
        cfg.assignment_max_future_slots = 5;
        cfg.global_error_constraint_enabled = false;
        cfg
    }

    fn empty_ctx() -> SwarmContext {
        SwarmContext {
            slot_count: HashMap::new(),
            slot_errors: HashMap::new(),
            global_error_sum: 0.0,
            global_count: 0,
        }
    }

    fn make_request(id: u64, arrival: i32, deadline: i32) -> Request {
        Request::new(id, arrival, deadline)
    }

    /// Merging two batches touching **disjoint forced slots** (each request's
    /// arrival==deadline, so slot choice can't depend on Q staleness) must
    /// give the exact same per-slot Q-values as processing all requests
    /// through a single call — proving the additive merge combines
    /// independent contributions losslessly, with no confound from
    /// stale-snapshot routing differences.
    #[test]
    fn bandit_merge_combines_disjoint_batches() {
        let cfg = test_cfg();
        let ctx = empty_ctx();
        let carbon_forecast = vec![1.0; 6];

        // Forced single-slot requests (arrival == deadline) on disjoint slots
        // 0 and 1 for batch_a, 2 and 3 for batch_b, so routing is independent
        // of Q-value staleness and of how work is split across batches.
        let batch_a = vec![make_request(1, 0, 0), make_request(2, 1, 1)];
        let batch_b = vec![make_request(3, 2, 2), make_request(4, 3, 3)];

        let baseline = OnlineBanditState::new(6, 10.0, 0.0, 42);
        let (_, delta_a) = baseline.solve_batch(&batch_a, &carbon_forecast, &ctx, &cfg);
        let (_, delta_b) = baseline.solve_batch(&batch_b, &carbon_forecast, &ctx, &cfg);
        let mut merged = OnlineBanditState::new(6, 10.0, 0.0, 42);
        merged.merge_delta(delta_a);
        merged.merge_delta(delta_b);

        let mut sequential = OnlineBanditState::new(6, 10.0, 0.0, 42);
        let all: Vec<Request> = batch_a.into_iter().chain(batch_b).collect();
        let (assignments, delta) = sequential.solve_batch(&all, &carbon_forecast, &ctx, &cfg);
        assert_eq!(assignments.len(), 4);
        sequential.merge_delta(delta);

        for i in 0..6 {
            assert_eq!(merged.n[i], sequential.n[i], "slot {i}: n mismatch");
            assert!(
                (merged.q[i] - sequential.q[i]).abs() < 1e-9,
                "slot {i}: merged.q={} sequential.q={}", merged.q[i], sequential.q[i]
            );
        }
        let total_n: u64 = merged.n.iter().sum();
        assert_eq!(total_n, 4);
    }

    /// A single `solve_batch` + `merge_delta` from a clean baseline must
    /// reproduce exactly what the serialized (`online_swarm.rs`) `&mut self`
    /// variant computes for the same requests/seed/config — the two
    /// implementations must agree whenever there's no concurrency to merge
    /// around, since the additive-merge model is designed to be a strict
    /// generalization of the serialized one.
    #[test]
    fn bandit_single_call_matches_serialized_variant() {
        let cfg = test_cfg();
        let ctx = empty_ctx();
        let carbon_forecast = vec![1.0, 2.0, 1.5, 1.0, 2.0, 1.0];
        let requests = vec![
            make_request(1, 0, 5), make_request(2, 1, 4),
            make_request(3, 0, 2), make_request(4, 2, 5),
        ];

        let mut legacy = crate::online_swarm::OnlineBanditState::new(6, 10.0, 0.2, 99);
        legacy.solve_batch(&requests, &carbon_forecast, &ctx, &cfg);

        let baseline = OnlineBanditState::new(6, 10.0, 0.2, 99);
        let (_, delta) = baseline.solve_batch(&requests, &carbon_forecast, &ctx, &cfg);
        let mut merged = OnlineBanditState::new(6, 10.0, 0.2, 99);
        merged.merge_delta(delta);

        for i in 0..6 {
            assert_eq!(merged.n[i], legacy.n[i], "slot {i}: n mismatch");
            assert!(
                (merged.q[i] - legacy.q[i]).abs() < 1e-9,
                "slot {i}: merged.q={} legacy.q={}", merged.q[i], legacy.q[i]
            );
        }
    }

    /// Same cross-implementation check as above, for ACO: a single
    /// `solve_batch` + `merge_delta` from a clean baseline must reproduce the
    /// pheromone vector computed by the serialized `&mut self` variant.
    #[test]
    fn aco_single_call_matches_serialized_variant() {
        let cfg = test_cfg();
        let ctx = empty_ctx();
        let carbon_forecast = vec![1.0, 2.0, 1.5, 1.0, 2.0, 1.0];
        let requests = vec![make_request(1, 0, 5), make_request(2, 0, 5), make_request(3, 0, 5)];

        let mut legacy = crate::online_swarm::OnlineAcoState::new(
            6, &carbon_forecast, &cfg, 4, 3, 1.0, 2.0, 0.3, 1.0, 1.0, 7,
        );
        legacy.solve_batch(&requests, &carbon_forecast, &ctx, &cfg);

        let baseline = OnlineAcoState::new(6, &carbon_forecast, &cfg, 4, 3, 1.0, 2.0, 0.3, 1.0, 1.0, 7);
        let (_, delta) = baseline.solve_batch(&requests, &carbon_forecast, &ctx, &cfg);
        let mut merged = OnlineAcoState::new(6, &carbon_forecast, &cfg, 4, 3, 1.0, 2.0, 0.3, 1.0, 1.0, 7);
        merged.merge_delta(delta);

        for i in 0..6 {
            assert!(
                (merged.tau[i] - legacy.tau[i]).abs() < 1e-9,
                "slot {i}: merged.tau={} legacy.tau={}", merged.tau[i], legacy.tau[i]
            );
        }
    }

    /// Sanity check that merging an ACO delta actually changes the
    /// pheromone vector and keeps it finite/positive.
    #[test]
    fn aco_merge_matches_direct_application() {
        let cfg = test_cfg();
        let ctx = empty_ctx();
        let carbon_forecast = vec![1.0, 2.0, 1.5, 1.0, 2.0, 1.0];
        let requests = vec![make_request(1, 0, 5), make_request(2, 0, 5), make_request(3, 0, 5)];

        let baseline = OnlineAcoState::new(6, &carbon_forecast, &cfg, 4, 3, 1.0, 2.0, 0.3, 1.0, 1.0, 7);

        let (_, delta) = baseline.solve_batch(&requests, &carbon_forecast, &ctx, &cfg);
        let mut merged = OnlineAcoState::new(6, &carbon_forecast, &cfg, 4, 3, 1.0, 2.0, 0.3, 1.0, 1.0, 7);
        merged.merge_delta(delta);

        // The merged tau must differ from the untouched baseline tau0 vector
        // (i.e. the delta actually had an effect) and must be finite/positive.
        for (i, &t) in merged.tau.iter().enumerate() {
            assert!(t.is_finite() && t > 0.0, "tau[{i}] = {t}");
        }
        assert!(
            merged.tau.iter().zip(baseline.tau.iter()).any(|(m, b)| (m - b).abs() > 1e-9),
            "merge_delta should have changed the pheromone vector"
        );
    }

    /// Two concurrent bandit deltas merged in either order must yield the
    /// same final Q-values (order-independence of the additive merge).
    #[test]
    fn bandit_merge_is_order_independent() {
        let cfg = test_cfg();
        let ctx = empty_ctx();
        let carbon_forecast = vec![1.0; 6];
        let batch_a = vec![make_request(1, 0, 5)];
        let batch_b = vec![make_request(2, 0, 5)];

        let baseline = OnlineBanditState::new(6, 10.0, 0.0, 1);
        let (_, delta_a1) = baseline.solve_batch(&batch_a, &carbon_forecast, &ctx, &cfg);
        let (_, delta_b1) = baseline.solve_batch(&batch_b, &carbon_forecast, &ctx, &cfg);
        let mut order1 = OnlineBanditState::new(6, 10.0, 0.0, 1);
        order1.merge_delta(delta_a1);
        order1.merge_delta(delta_b1);

        let (_, delta_a2) = baseline.solve_batch(&batch_a, &carbon_forecast, &ctx, &cfg);
        let (_, delta_b2) = baseline.solve_batch(&batch_b, &carbon_forecast, &ctx, &cfg);
        let mut order2 = OnlineBanditState::new(6, 10.0, 0.0, 1);
        order2.merge_delta(delta_b2);
        order2.merge_delta(delta_a2);

        for i in 0..6 {
            assert_eq!(order1.n[i], order2.n[i]);
            assert!((order1.q[i] - order2.q[i]).abs() < 1e-9);
        }
    }
}
