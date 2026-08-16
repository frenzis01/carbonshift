//! Swarm-based alternative scheduling strategies for CarbonShift.
//!
//! Provides two comparison strategies that can be run alongside the DP solver:
//!
//! - **Bandit** (`run_bandit`): epsilon-greedy multi-armed bandit.  Each arm
//!   is a candidate slot.  Q-values (running mean of carbon costs achieved at a
//!   slot) guide exploitation; ε-random choices drive exploration.
//!
//! - **Ant Colony Optimisation** (`run_ant_colony`): offline ACO that processes
//!   all requests jointly.  A pheromone vector over slots is updated by the
//!   best-cost ant in each iteration.
//!
//! Both strategies:
//! - Respect per-request deadlines and the `assignment_max_future_slots` window.
//! - Try all flavours, picking the cheapest one that satisfies the global and
//!   window error constraints; fall back to the minimum-error flavour if no
//!   feasible flavour exists for any candidate slot.

use std::collections::HashMap;

use rand::rngs::SmallRng;
use rand::{Rng, SeedableRng};

use carbonshift_rs::config::Config;
use carbonshift_rs::scenario::ScenarioRequest;
use carbonshift_rs::types::{CapacityTier, Flavour};

// ─── shared types ─────────────────────────────────────────────────────────────

/// A completed assignment produced by a swarm strategy.
pub struct SwarmAssignment {
    pub request_id: u64,
    pub arrival_slot: i32,
    pub deadline_slot: i32,
    pub scheduled_slot: i32,
    pub flavour_name: String,
    // TODO verify if this is needed later on in the development or not
    #[allow(dead_code)]
    pub flavour_duration: i32,
    pub error: f64,
    pub carbon_cost: f64,
}

// ─── shared utilities ────────────────────────────────────────────────────────

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

/// Returns the valid target slot range for a request: [arrival, min(deadline, arrival+window)].
fn valid_slots(req: &ScenarioRequest, max_future: i32, total_slots: i32) -> Vec<i32> {
    let from = req.arrival_slot;
    let to = req.deadline_slot
        .min(req.arrival_slot + max_future)
        .min(total_slots - 1);
    (from..=to.max(from)).collect()
}

/// Try flavours (sorted cheapest-first by duration) for the given `slot`.
/// Returns `Some((flavour, cost))` for the first one that passes error constraints,
/// or `None` if every flavour violates them.
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
    let ci       = carbon_forecast.get(slot as usize).copied().unwrap_or(1.0);
    let position = *slot_count.get(&slot).unwrap_or(&0) + 1;
    let mult     = capacity_mult(tiers, position as i64);

    for &flav in sorted_flavours {
        if global_constraint_enabled {
            let new_avg = (global_error_sum + flav.error) / (global_count as f64 + 1.0);
            if new_avg > max_err { continue; }
        }
        {
            let mut win_sum = flav.error;
            let mut win_cnt = 1usize;
            for ws in (slot - win_past)..=(slot + win_future) {
                if let Some(errs) = slot_errors.get(&ws) {
                    win_sum += errs.iter().sum::<f64>();
                    win_cnt += errs.len();
                }
            }
            if win_sum / win_cnt as f64 > max_err { continue; }
        }
        let cost = ci * mult * flav.duration as f64 * scale;
        return Some((flav, cost));
    }
    None
}

/// Compute the carbon cost for a slot using the given flavour (ignores error constraints).
fn slot_cost(
    slot: i32,
    flav: &Flavour,
    carbon_forecast: &[f64],
    tiers: &[CapacityTier],
    slot_count: &HashMap<i32, i32>,
    scale: f64,
) -> f64 {
    let ci       = carbon_forecast.get(slot as usize).copied().unwrap_or(1.0);
    let position = *slot_count.get(&slot).unwrap_or(&0) + 1;
    let mult     = capacity_mult(tiers, position as i64);
    ci * mult * flav.duration as f64 * scale
}

// ─── Bandit ───────────────────────────────────────────────────────────────────

/// Hyper-parameters for the epsilon-greedy bandit strategy.
pub struct BanditParams {
    /// Probability of choosing a random valid slot instead of the best-known.
    pub epsilon: f64,
    /// Optimistic initial Q-value (encourages exploration of all slots early on).
    pub initial_q: f64,
    /// RNG seed for reproducibility.
    pub seed: u64,
}

impl Default for BanditParams {
    fn default() -> Self {
        Self { epsilon: 0.15, initial_q: 10.0, seed: 42 }
    }
}

/// Run the epsilon-greedy bandit strategy over `requests` (must be sorted by
/// `(arrival_slot, request_id)` before calling).
///
/// Q-values are maintained per slot and updated with the running mean of the
/// actual carbon costs achieved at that slot.  On each request the strategy
/// either exploits (pick valid slot with lowest Q) or explores (random valid
/// slot) with probability `params.epsilon`.  Within the chosen slot the
/// cheapest flavour that satisfies the global and window error constraints is
/// used; if none is feasible the minimum-error flavour is used as a fallback.
pub fn run_bandit(
    requests: &[ScenarioRequest],
    carbon_forecast: &[f64],
    cfg: &Config,
    params: &BanditParams,
) -> Vec<SwarmAssignment> {
    let tiers    = &cfg.capacity_tiers;
    let scale    = cfg.carbon_cost_duration_scale;
    let max_future   = cfg.assignment_max_future_slots;
    let total_slots  = cfg.total_slots;
    let win_past     = cfg.error_window_past;
    let win_future   = cfg.error_window_future;
    let max_err      = cfg.max_error_threshold;

    // Flavours sorted cheapest-first; fallback = minimum-error flavour.
    let mut sorted_flavours: Vec<&Flavour> = cfg.flavours.iter().collect();
    sorted_flavours.sort_by_key(|f| f.duration);
    let fallback_flav = cfg.flavours
        .iter()
        .min_by(|a, b| a.error.partial_cmp(&b.error).unwrap())
        .expect("Config must have at least one flavour");

    // Q[s] = running-mean carbon cost achieved at slot s; n[s] = sample count.
    let mut q: Vec<f64> = vec![params.initial_q; total_slots as usize];
    let mut n: Vec<u64> = vec![0; total_slots as usize];
    let mut slot_count:  HashMap<i32, i32>       = HashMap::new();
    let mut slot_errors: HashMap<i32, Vec<f64>>  = HashMap::new();
    let mut global_error_sum: f64 = 0.0;
    let mut global_count:  usize  = 0;
    let mut rng = SmallRng::seed_from_u64(params.seed);
    let mut assignments = Vec::with_capacity(requests.len());

    for req in requests {
        let candidates = valid_slots(req, max_future, total_slots);
        if candidates.is_empty() {
            continue;
        }

        // ε-greedy slot selection.
        let chosen_slot = if rng.r#gen::<f64>() < params.epsilon {
            candidates[rng.gen_range(0..candidates.len())]
        } else {
            *candidates
                .iter()
                .min_by(|&&a, &&b| q[a as usize].partial_cmp(&q[b as usize]).unwrap_or(std::cmp::Ordering::Equal))
                .unwrap()
        };

        // Within the chosen slot, pick cheapest feasible flavour (error-aware).
        let (chosen_flav, cost) = pick_feasible_flavour(
            chosen_slot, carbon_forecast, tiers, &slot_count, &slot_errors,
            global_error_sum, global_count, scale, &sorted_flavours,
            max_err, win_past, win_future, cfg.global_error_constraint_enabled,
        ).unwrap_or_else(|| {
            let c = slot_cost(chosen_slot, fallback_flav, carbon_forecast, tiers, &slot_count, scale);
            (fallback_flav, c)
        });

        // Update running-mean Q-value.
        let idx = chosen_slot as usize;
        n[idx] += 1;
        q[idx] += (cost - q[idx]) / n[idx] as f64;

        *slot_count.entry(chosen_slot).or_insert(0) += 1;
        slot_errors.entry(chosen_slot).or_default().push(chosen_flav.error);
        global_error_sum += chosen_flav.error;
        global_count     += 1;

        assignments.push(SwarmAssignment {
            request_id:       req.request_id,
            arrival_slot:     req.arrival_slot,
            deadline_slot:    req.deadline_slot,
            scheduled_slot:   chosen_slot,
            flavour_name:     chosen_flav.name.clone(),
            flavour_duration: chosen_flav.duration,
            error:            chosen_flav.error,
            carbon_cost:      cost,
        });
    }

    assignments
}

// ─── Ant Colony Optimisation ──────────────────────────────────────────────────

/// Hyper-parameters for the Ant Colony Optimisation strategy.
pub struct AcoParams {
    /// Number of ants per iteration.
    pub n_ants: usize,
    /// Number of ACO iterations.
    pub n_iterations: usize,
    /// Pheromone influence exponent (α).
    pub alpha: f64,
    /// Heuristic influence exponent (β).
    pub beta: f64,
    /// Pheromone evaporation rate ρ ∈ (0, 1).
    pub rho: f64,
    /// Pheromone deposit quantity (divided by solution cost).
    pub q: f64,
    /// Initial pheromone level τ₀.
    pub tau0: f64,
    /// RNG seed for reproducibility.
    pub seed: u64,
}

impl Default for AcoParams {
    fn default() -> Self {
        Self {
            n_ants: 10,
            n_iterations: 20,
            alpha: 1.0,
            beta: 2.0,
            rho: 0.5,
            q: 1.0,
            tau0: 1.0,
            seed: 42,
        }
    }
}

/// Run the Ant Colony Optimisation strategy over `requests` (must be sorted by
/// `(arrival_slot, request_id)` before calling).
///
/// Pheromone is maintained per slot (shared across requests).  Each ant builds
/// a complete assignment by sampling slots for every request with probability
/// proportional to τ[s]^α × η(req,s)^β where η = 1/estimated_cost.  Within
/// the chosen slot the cheapest feasible flavour (satisfying global and window
/// error constraints) is selected; if no flavour is feasible the minimum-error
/// flavour is used.  At the end of each iteration the pheromone is evaporated
/// and reinforced by the best-cost ant of that iteration.
pub fn run_ant_colony(
    requests: &[ScenarioRequest],
    carbon_forecast: &[f64],
    cfg: &Config,
    params: &AcoParams,
) -> Vec<SwarmAssignment> {
    let tiers        = &cfg.capacity_tiers;
    let scale        = cfg.carbon_cost_duration_scale;
    let max_future   = cfg.assignment_max_future_slots;
    let total_slots  = cfg.total_slots;
    let win_past     = cfg.error_window_past;
    let win_future   = cfg.error_window_future;
    let max_err      = cfg.max_error_threshold;

    // Flavours sorted cheapest-first; fallback = minimum-error flavour.
    let mut sorted_flavours: Vec<&Flavour> = cfg.flavours.iter().collect();
    sorted_flavours.sort_by_key(|f| f.duration);
    let fallback_flav = cfg.flavours
        .iter()
        .min_by(|a, b| a.error.partial_cmp(&b.error).unwrap())
        .expect("Config must have at least one flavour");

    // Heuristic: rough per-slot cost using cheapest flavour, position=1, mult=1.
    let cheapest = sorted_flavours[0];
    let eta: Vec<f64> = (0..total_slots as usize)
        .map(|s| {
            let ci   = carbon_forecast.get(s).copied().unwrap_or(1.0);
            let base = ci * cheapest.duration as f64 * scale;
            if base > 0.0 { 1.0 / base } else { 1e9 }
        })
        .collect();

    // Pheromone per slot.
    let mut tau: Vec<f64> = vec![params.tau0; total_slots as usize];

    let mut best_cost = f64::INFINITY;
    let mut best_solution: Vec<SwarmAssignment> = Vec::new();
    let mut rng = SmallRng::seed_from_u64(params.seed);

    for _iter in 0..params.n_iterations {
        let mut iter_best_cost = f64::INFINITY;
        let mut iter_best_solution: Vec<SwarmAssignment> = Vec::new();

        for _ant in 0..params.n_ants {
            // Each ant tracks its own error state.
            let mut slot_count:  HashMap<i32, i32>      = HashMap::new();
            let mut slot_errors: HashMap<i32, Vec<f64>> = HashMap::new();
            let mut global_error_sum: f64 = 0.0;
            let mut global_count:  usize  = 0;
            let mut ant_solution: Vec<SwarmAssignment> = Vec::with_capacity(requests.len());
            let mut ant_cost = 0.0_f64;

            for req in requests {
                let candidates = valid_slots(req, max_future, total_slots);
                if candidates.is_empty() {
                    continue;
                }

                // Compute unnormalised probabilities.
                let weights: Vec<f64> = candidates
                    .iter()
                    .map(|&s| {
                        let t = tau[s as usize].max(1e-12);
                        let e = eta[s as usize];
                        t.powf(params.alpha) * e.powf(params.beta)
                    })
                    .collect();

                let total_weight: f64 = weights.iter().sum();
                let chosen_slot = if total_weight <= 0.0 {
                    candidates[rng.gen_range(0..candidates.len())]
                } else {
                    let mut r = rng.r#gen::<f64>() * total_weight;
                    let mut chosen = *candidates.last().unwrap();
                    for (&s, &w) in candidates.iter().zip(weights.iter()) {
                        r -= w;
                        if r <= 0.0 {
                            chosen = s;
                            break;
                        }
                    }
                    chosen
                };

                // Pick cheapest feasible flavour for chosen slot.
                let (chosen_flav, cost) = pick_feasible_flavour(
                    chosen_slot, carbon_forecast, tiers, &slot_count, &slot_errors,
                    global_error_sum, global_count, scale, &sorted_flavours,
                    max_err, win_past, win_future, cfg.global_error_constraint_enabled,
                ).unwrap_or_else(|| {
                    let c = slot_cost(chosen_slot, fallback_flav, carbon_forecast, tiers, &slot_count, scale);
                    (fallback_flav, c)
                });

                ant_cost += cost;
                *slot_count.entry(chosen_slot).or_insert(0) += 1;
                slot_errors.entry(chosen_slot).or_default().push(chosen_flav.error);
                global_error_sum += chosen_flav.error;
                global_count     += 1;

                ant_solution.push(SwarmAssignment {
                    request_id:       req.request_id,
                    arrival_slot:     req.arrival_slot,
                    deadline_slot:    req.deadline_slot,
                    scheduled_slot:   chosen_slot,
                    flavour_name:     chosen_flav.name.clone(),
                    flavour_duration: chosen_flav.duration,
                    error:            chosen_flav.error,
                    carbon_cost:      cost,
                });
            }

            if ant_cost < iter_best_cost {
                iter_best_cost = ant_cost;
                iter_best_solution = ant_solution;
            }
        }

        // Evaporate.
        for t in tau.iter_mut() {
            *t *= 1.0 - params.rho;
            *t = t.max(1e-12);
        }

        // Deposit on best-ant slots.
        if iter_best_cost > 0.0 && iter_best_cost.is_finite() {
            let deposit = params.q / iter_best_cost;
            for a in &iter_best_solution {
                tau[a.scheduled_slot as usize] += deposit;
            }
        }

        // Track global best.
        if iter_best_cost < best_cost {
            best_cost = iter_best_cost;
            best_solution = iter_best_solution;
        }
    }

    best_solution
}
