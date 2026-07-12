//! Online swarm scheduling strategies for the CarbonShift batch scheduler
//! (**serialized** concurrency variant — see `online_swarmerge.rs` for the
//! parallel, additive-merge variant; select between them via
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
//! `OnlineSwarmState` is stored in `SchedulerMutableState` (behind the
//! scheduler mutex).  Each batch worker solves **while holding the mutex**
//! (see `scheduler.rs::batch_worker_entry_swarm_serialized`), so state updates
//! are fully sequential regardless of how many worker threads are spawned
//! concurrently — this trades away swarm/swarm concurrency (DP batches are
//! unaffected) for correctness and reproducibility.
//!
//! An earlier version of this module used a "clone the state, solve
//! lock-free, then overwrite (`*self = updated`)" pattern to avoid holding the
//! lock during the solve. That is unsound: when two workers overlap, the
//! second write-back **silently discards** the first worker's Q-value /
//! pheromone updates rather than merging them — not just reordering, but
//! genuine loss of learning signal, whose severity depends on `batch_size`
//! (it changes how much workers overlap). Since a single swarm solve is much
//! cheaper than a DP solve, serializing it has negligible performance cost.

use std::collections::HashMap;

use rand::rngs::SmallRng;
use rand::{Rng, SeedableRng};

use crate::config::Config;
use crate::types::{Assignment, CapacityTier, Flavour, Request};

// ─── SwarmContext ─────────────────────────────────────────────────────────────

/// Snapshot of committed scheduling state, used as the baseline for the
/// swarm solver's capacity and error computations.
///
/// Built once per batch worker from `SharedState::swarm_context_snapshot`.
pub struct SwarmContext {
    /// Number of already-committed assignments per slot.
    pub slot_count: HashMap<i32, i32>,
    /// Error values of already-committed assignments, grouped by slot.
    pub slot_errors: HashMap<i32, Vec<f64>>,
    /// Cumulative error sum across all committed assignments.
    pub global_error_sum: f64,
    /// Total number of committed assignments.
    pub global_count: usize,
}

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

/// Persistent ε-greedy bandit state. With the serialized concurrency model,
/// each batch worker mutates this state directly while holding the scheduler
/// mutex — no cloning or write-back merge is needed.
#[derive(Clone)]
pub struct OnlineBanditState {
    /// Per-slot running-mean Q-values (estimated carbon cost).
    pub q: Vec<f64>,
    /// Per-slot sample counts.
    pub n: Vec<u64>,
    pub rng: SmallRng,
    pub epsilon: f64,
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

    /// Assign `pending` using the current Q-values.
    ///
    /// `ctx` is the committed-state baseline (slot counts, errors).
    /// Q-values are updated incrementally in place; the caller must hold the
    /// scheduler mutex for the duration of this call (serialized model).
    pub fn solve_batch(
        &mut self,
        pending: &[Request],
        carbon_forecast: &[f64],
        ctx: &SwarmContext,
        cfg: &Config,
    ) -> Vec<Assignment> {
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

        let mut assignments = Vec::with_capacity(pending.len());

        for req in pending {
            let candidates = valid_slots(req.arrival_slot, req.deadline_slot, max_future, total_slots);
            if candidates.is_empty() { continue; }

            // ε-greedy slot selection.
            let chosen_slot = if self.rng.r#gen::<f64>() < self.epsilon {
                candidates[self.rng.gen_range(0..candidates.len())]
            } else {
                *candidates.iter()
                    .min_by(|&&a, &&b| {
                        let qa = self.q.get(a as usize).copied().unwrap_or(f64::MAX);
                        let qb = self.q.get(b as usize).copied().unwrap_or(f64::MAX);
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

            // Incremental Q-update (running mean).
            let idx = chosen_slot as usize;
            if idx < self.n.len() {
                self.n[idx] += 1;
                self.q[idx] += (cost - self.q[idx]) / self.n[idx] as f64;
            }

            *slot_count.entry(chosen_slot).or_insert(0) += 1;
            slot_errors.entry(chosen_slot).or_default().push(chosen_flav.error);
            global_error_sum += chosen_flav.error;
            global_count += 1;

            assignments.push(make_assignment(req, chosen_slot, chosen_flav, cost));
        }

        assignments
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

    /// Assign `pending` using the current pheromone state.
    ///
    /// Runs `n_iterations` × `n_ants` mini-iterations on the batch, updates
    /// pheromone, and returns the best-ant solution. The caller must hold the
    /// scheduler mutex for the duration of this call (serialized model).
    pub fn solve_batch(
        &mut self,
        pending: &[Request],
        carbon_forecast: &[f64],
        ctx: &SwarmContext,
        cfg: &Config,
    ) -> Vec<Assignment> {
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
                        let t = self.tau.get(s as usize).copied().unwrap_or(1e-12).max(1e-12);
                        let e = self.eta.get(s as usize).copied().unwrap_or(1e-12);
                        t.powf(self.alpha) * e.powf(self.beta)
                    }).collect();

                    let total_weight: f64 = weights.iter().sum();
                    let chosen_slot = if total_weight <= 0.0 {
                        candidates[self.rng.gen_range(0..candidates.len())]
                    } else {
                        let mut r = self.rng.r#gen::<f64>() * total_weight;
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

            // Evaporate.
            for t in self.tau.iter_mut() {
                *t *= 1.0 - self.rho;
                *t = t.max(1e-12);
            }

            // Deposit on best-ant slots.
            if iter_best_cost > 0.0 && iter_best_cost.is_finite() {
                let deposit = self.q_deposit / iter_best_cost;
                for a in &iter_best_solution {
                    if let Some(t) = self.tau.get_mut(a.scheduled_slot as usize) {
                        *t += deposit;
                    }
                }
            }

            if iter_best_cost < best_cost {
                best_cost = iter_best_cost;
                best_solution = iter_best_solution;
            }
        }

        best_solution
    }
}

// ─── OnlineSwarmState enum ────────────────────────────────────────────────────

/// Unified online swarm state — stored in `SchedulerMutableState` and
/// mutated directly by whichever worker holds the scheduler mutex.
#[derive(Clone)]
pub enum OnlineSwarmState {
    /// The scheduler is using the DP solver; no swarm state.
    None,
    Bandit(OnlineBanditState),
    Aco(OnlineAcoState),
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

    /// Solve a batch of requests using the active swarm strategy.
    ///
    /// Mutates `self` in place (Q-values or pheromone). The caller must hold
    /// the scheduler mutex for the duration of this call so concurrent
    /// workers never race on the same state (serialized model).
    pub fn solve_batch(
        &mut self,
        pending: &[Request],
        _slot: i32,
        carbon_forecast: &[f64],
        ctx: &SwarmContext,
        cfg: &Config,
    ) -> Vec<Assignment> {
        match self {
            Self::None => vec![],
            Self::Bandit(b) => b.solve_batch(pending, carbon_forecast, ctx, cfg),
            Self::Aco(a) => a.solve_batch(pending, carbon_forecast, ctx, cfg),
        }
    }
}
