//! Stateless scheduling algorithms adapted to the common solver contract.
pub mod aco;
pub mod bandit;
mod shared;

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicU64, Ordering};

use rand::Rng;
use rand::SeedableRng;
use rand::rngs::SmallRng;

use crate::engine::config::SwarmConfig;
use crate::engine::dp_solver::{DpSolver, ErrorWindowBaseline, MockPool, SolveBatchInput};
use crate::engine::random_selection::{
    RandomSelectionMode, closest_to_uniform_flavour_probabilities, select_random_assignment,
};
use crate::engine::solver::{
    CommitReport, ProposedPlacement, SolveInput, SolveRequest, SolverAlgorithm, SolverError,
    SolverProposal, SolverRegistry, SolverScope,
};
use crate::types::{CapacityTier, Flavour, get_capacity_multiplier};

/// DP-specific search settings, kept separate from the application config.
#[derive(Debug, Clone)]
pub struct DpAlgorithmConfig {
    /// DP pruning method (`"none"`, `"beam"`, or `"kbest"`).
    pub pruning_method: String,
    /// Minimum batch size at which the configured pruning method is enabled.
    pub pruning_min_batch_size: usize,
    /// Maximum retained DP states when pruning is enabled.
    pub pruning_k: usize,
    /// Search timeout in seconds.
    pub timeout_secs: f64,
}

/// Stateless adapter that runs one joint DP solve for the complete input batch.
pub struct DpAlgorithm {
    pub config: DpAlgorithmConfig,
}

impl DpAlgorithm {
    fn effective_pruning(&self, batch_size: usize) -> String {
        if self.config.pruning_min_batch_size == 0
            || batch_size >= self.config.pruning_min_batch_size
        {
            self.config.pruning_method.clone()
        } else {
            "none".to_owned()
        }
    }
}

impl SolverAlgorithm for DpAlgorithm {
    type State = ();
    type Delta = ();

    fn initial_state(&self, _scope: &SolverScope) {}

    fn solve(
        &self,
        _snapshot: &(),
        scope: &SolverScope,
        input: &SolveInput,
    ) -> Result<(SolverProposal, ()), SolverError> {
        if input.requests.is_empty() {
            return Ok((SolverProposal::default(), ()));
        }
        validate_solve_input(input, scope)?;

        // Initialize placement vector to be returned
        let mut placements = Vec::with_capacity(input.requests.len());
        // Initialize per-slot counts with already assigned requests
        let mut solver_baseline_counts = input.baseline_slot_counts.clone();
        let mut cumulative_count = input.qos.cumulative_err.count as i64;
        let mut cumulative_sum = input.qos.cumulative_err.sum;
        let mut window_count = input.qos.error_window.baseline_err.count;
        let mut window_sum = input.qos.error_window.baseline_err.sum;
        // Mark late requests
        // i.e. requests such that current_slot > deadline_slot
        // These should be a few and the result of late arrivals mostly
        let late_request_ids: HashSet<u64> = input
            .requests
            .iter()
            .filter(|request| input.current_slot > request.deadline_slot)
            .map(|request| request.request_id)
            .collect();

        // BIZ_RULE: Assign late requests first to current slot
        // with the cheapest feasible flavour
        for request in input
            .requests
            .iter()
            .filter(|request| late_request_ids.contains(&request.request_id))
        {
            let flavour = cheapest_feasible_flavour(
                &input.qos.flavours,
                input.qos.max_average_error,
                input.qos.cumulative_error_constraint_active,
                cumulative_count,
                cumulative_sum,
                window_count,
                window_sum,
            )?;
            let count = solver_baseline_counts
                .entry(input.current_slot)
                .or_insert(0);
            *count = count.checked_add(1).ok_or_else(|| {
                SolverError::Algorithm("current-slot occupancy overflowed i32".to_owned())
            })?;
            cumulative_count = cumulative_count.checked_add(1).ok_or_else(|| {
                SolverError::Algorithm("cumulative error count overflowed i64".to_owned())
            })?;
            cumulative_sum += flavour.error;
            window_count += 1.0;
            window_sum += flavour.error;
            placements.push(ProposedPlacement::late(
                request.request_id,
                input.current_slot,
                flavour.name.clone(),
            ));
        }

        // Filter out late requests for DP
        let on_time_requests: Vec<_> = input
            .requests
            .iter()
            .filter(|request| !late_request_ids.contains(&request.request_id))
            .map(|request| {
                let (_, deadline_slot) = request_slot_bounds(input, request, scope)?;
                Ok(SolveRequest {
                    deadline_slot,
                    ..*request
                })
            })
            .collect::<Result<_, SolverError>>()?;

        // Return early if no on-time requests
        if on_time_requests.is_empty() {
            return Ok((SolverProposal { placements }, ()));
        }

        let cumulative_average = if cumulative_count > 0 {
            cumulative_sum / cumulative_count as f64
        } else {
            0.0
        };
        let mut flavours = input.qos.flavours.clone();
        if input.qos.cumulative_error_constraint_active
            && cumulative_average > input.qos.max_average_error
        {
            let allowed: Vec<Flavour> = flavours
                .iter()
                .filter(|flavour| flavour.error <= input.qos.max_average_error)
                .cloned()
                .collect();
            if !allowed.is_empty() {
                flavours = allowed;
            }
        }

        let solver = DpSolver {
            flavours,
            carbon_forecast: input.carbon_forecast.clone(),
            window_size: scope.total_slots as i32,
            pruning: self.effective_pruning(input.requests.len()),
            pruning_k: self.config.pruning_k,
            timeout: self.config.timeout_secs,
            carbon_cost_scale: input.carbon_cost_duration_scale,
        };
        // Keep the DP-specific solve input here; the common input supplies one
        // flavour set for this homogeneous QoS-profile batch.
        let mut assignments = solver.solve_batch(SolveBatchInput {
            requests: &on_time_requests,
            current_slot: input.current_slot,
            capacity_multiplier: 1.0,
            capacity_tiers: &input.capacity_tiers,
            baseline_slot_counts: &solver_baseline_counts,
            error_window_baseline: ErrorWindowBaseline {
                error_sum: window_sum,
                request_count: window_count,
            },
            max_error_threshold: Some(input.qos.max_average_error),
            error_window_past: input.qos.error_window.past_slots,
            error_window_future: input.qos.error_window.future_slots,
            assignment_max_slot: Some(latest_forecast_slot(input, scope)),
            dynamic_mock_pool: MockPool::default(),
        });

        let scheduled_ids: HashSet<u64> = assignments
            .iter()
            .map(|assignment| assignment.request_id)
            .collect();
        let unscheduled: Vec<_> = on_time_requests
            .iter()
            .filter(|request| !scheduled_ids.contains(&request.request_id))
            .copied()
            .collect();

        if !unscheduled.is_empty() {
            let mut fallback_counts: Vec<i64> = (0..scope.total_slots)
                .map(|slot| {
                    solver_baseline_counts
                        .get(&(slot as i32))
                        .copied()
                        .unwrap_or(0)
                })
                .collect();
            for assignment in &assignments {
                fallback_counts[assignment.slot as usize] += 1;
            }

            assignments.extend(solver.greedy_fallback(
                &unscheduled,
                input.current_slot,
                &input.capacity_tiers,
                &fallback_counts,
            ));
        }

        placements.extend(assignments.into_iter().map(|assignment| {
            ProposedPlacement::new(
                assignment.request_id,
                assignment.slot,
                assignment.flavour_name,
            )
        }));
        Ok((SolverProposal { placements }, ()))
    }

    fn merge(&self, _state: &mut (), _delta: (), _committed: &CommitReport) {}
}

/// Chooses which profile flavours the singleton search may consider.
#[derive(Debug, Clone, Copy)]
pub enum SingletonBestFlavourPolicy {
    /// Search every flavour supplied by the request's QoS profile.
    AllProfileFlavours,
    /// Restrict the search to that profile's lowest-error flavour.
    MostAccurateOnly,
}

/// Exhaustive per-request strategy that minimizes carbon cost within the QoS policy.
pub struct SingletonBestAlgorithm {
    flavour_policy: SingletonBestFlavourPolicy,
}

impl SingletonBestAlgorithm {
    /// Create a singleton search with an explicit flavour-selection policy.
    pub fn new(flavour_policy: SingletonBestFlavourPolicy) -> Self {
        Self { flavour_policy }
    }
}

impl SolverAlgorithm for SingletonBestAlgorithm {
    type State = ();
    type Delta = ();

    fn initial_state(&self, _scope: &SolverScope) {}

    fn solve(
        &self,
        _snapshot: &(),
        scope: &SolverScope,
        input: &SolveInput,
    ) -> Result<(SolverProposal, ()), SolverError> {
        if input.requests.is_empty() {
            return Ok((SolverProposal::default(), ()));
        }
        validate_solve_input(input, scope)?;

        // Both singleton variants share the same search and QoS behavior.
        // This policy changes only the candidate flavour set, so offline
        // accurate-only runs do not need a duplicate scheduling algorithm.
        let flavours = match self.flavour_policy {
            SingletonBestFlavourPolicy::AllProfileFlavours => input.qos.flavours.clone(),
            SingletonBestFlavourPolicy::MostAccurateOnly => {
                vec![get_fallback_flav(&input.qos.flavours)?.clone()]
            }
        };

        // Initialize placements and counts
        let mut placements = Vec::with_capacity(input.requests.len());
        let mut slot_count = input.baseline_slot_counts.clone();
        let mut solved_errors = Vec::with_capacity(input.requests.len());
        let mut cumulative_count = input.qos.cumulative_err.count as i64;
        let mut cumulative_sum = input.qos.cumulative_err.sum;
        let mut window_count = input.qos.error_window.baseline_err.count;
        let mut window_sum = input.qos.error_window.baseline_err.sum;

        for request in &input.requests {
            // If request is late, mark it as such and go on
            if input.current_slot > request.deadline_slot {
                let flavour = cheapest_feasible_flavour(
                    &flavours,
                    input.qos.max_average_error,
                    input.qos.cumulative_error_constraint_active,
                    cumulative_count,
                    cumulative_sum,
                    window_count,
                    window_sum,
                )?;
                *slot_count.entry(input.current_slot).or_insert(0) += 1;
                solved_errors.push((input.current_slot, flavour.error));
                cumulative_count = cumulative_count.checked_add(1).ok_or_else(|| {
                    SolverError::Algorithm("cumulative error count overflowed i64".to_owned())
                })?;
                cumulative_sum += flavour.error;
                window_count += 1.0;
                window_sum += flavour.error;
                placements.push(ProposedPlacement::late(
                    request.request_id,
                    input.current_slot,
                    flavour.name.clone(),
                ));
                continue;
            }

            // Find best slot and feasible flavour
            let (start_slot, deadline) = request_slot_bounds(input, request, scope)?;
            let mut sorted_flavours: Vec<&Flavour> = flavours.iter().collect();
            sorted_flavours.sort_by(|left, right| left.duration.total_cmp(&right.duration));

            let mut best: Option<(f64, i32, &Flavour)> = None;

            for slot in start_slot..=deadline {
                // Input validation and deadline capping guarantee this index
                // is inside the forecast; the current value is the observed CI.
                let carbon_intensity = input.carbon_forecast[slot as usize];
                let position = slot_count.get(&slot).copied().unwrap_or(0) + 1;
                let multiplier = get_capacity_multiplier(&input.capacity_tiers, position);

                for flav in &sorted_flavours {
                    let cumulative_average = if cumulative_count > 0 {
                        cumulative_sum / cumulative_count as f64
                    } else {
                        0.0
                    };
                    if input.qos.cumulative_error_constraint_active
                        && cumulative_average > input.qos.max_average_error
                        && flav.error > input.qos.max_average_error
                    {
                        continue;
                    }

                    // Match the existing policy: the local window is anchored
                    // to this request's arrival slot.
                    let win_start =
                        (request.arrival_slot - input.qos.error_window.past_slots).max(0);
                    let win_end = (request.arrival_slot + input.qos.error_window.future_slots)
                        .min(scope.total_slots as i32 - 1);
                    let mut win_sum = input.qos.error_window.baseline_err.sum + flav.error;
                    let mut win_count = input.qos.error_window.baseline_err.count + 1.0;
                    for &(placed_slot, placed_error) in &solved_errors {
                        if (win_start..=win_end).contains(&placed_slot) {
                            win_sum += placed_error;
                            win_count += 1.0;
                        }
                    }

                    // Reject this candidate if it would violate the window
                    // budget; another flavour or slot may still be feasible.
                    if win_count > 0.0 && win_sum / win_count > input.qos.max_average_error {
                        continue;
                    }

                    let cost = compute_cost(
                        carbon_intensity,
                        multiplier,
                        flav.duration,
                        input.carbon_cost_duration_scale,
                    );
                    if best.map(|(c, _, _)| cost < c).unwrap_or(true) {
                        best = Some((cost, slot, flav));
                    }
                }
            }

            let (chosen_slot, chosen_flavour) = if let Some((_, slot, flavour)) = best {
                (slot, flavour)
            } else {
                // BIZ_RULE: most accurate as fallback
                // Use the most accurate flavour at the earliest legal slot. This fallback
                // can exceed the error threshold when no feasible candidate
                // exists; it does not change the threshold used above.
                (start_slot, get_fallback_flav(&flavours)?)
            };

            *slot_count.entry(chosen_slot).or_insert(0) += 1;
            solved_errors.push((chosen_slot, chosen_flavour.error));
            cumulative_count = cumulative_count.checked_add(1).ok_or_else(|| {
                SolverError::Algorithm("cumulative error count overflowed i64".to_owned())
            })?;
            cumulative_sum += chosen_flavour.error;
            window_count += 1.0;
            window_sum += chosen_flavour.error;
            placements.push(ProposedPlacement::new(
                request.request_id,
                chosen_slot,
                chosen_flavour.name.clone(),
            ));
        }
        Ok((SolverProposal { placements }, ()))
    }

    fn merge(&self, _state: &mut (), _delta: (), _committed: &CommitReport) {}
}

// BIZ_RULE: computing cost, applying carbon_cost_duration_scale
fn compute_cost(ci: f64, mult: f64, dur: f64, scale: f64) -> f64 {
    ci * mult * dur * scale
}

fn get_slot_cost(
    slot: i32,
    flav: &Flavour,
    carbon_forecast: &[f64],
    tiers: &[CapacityTier],
    slot_count: &HashMap<i32, i64>,
    scale: f64,
) -> f64 {
    let ci = carbon_forecast.get(slot as usize).copied().unwrap_or(1.0);
    let position = *slot_count.get(&slot).unwrap_or(&0) + 1;
    let mult = get_capacity_multiplier(tiers, position);
    ci * mult * flav.duration * scale
}

/// Yields the flavour with minimum error
fn get_fallback_flav(flavours: &[Flavour]) -> Result<&Flavour, SolverError> {
    flavours
        .iter()
        // BIZ_RULE: fallback flavour is the one minimizing error
        .min_by(|left, right| left.error.total_cmp(&right.error))
        .ok_or_else(|| SolverError::Algorithm("no fallback flavour is available".to_owned()))
}

fn cheapest_feasible_flavour(
    flavours: &[Flavour],
    threshold: f64,
    cumulative_constraint_active: bool,
    cumulative_count: i64,
    cumulative_sum: f64,
    window_count: f64,
    window_sum: f64,
) -> Result<&Flavour, SolverError> {
    flavours
        .iter()
        .filter(|flavour| {
            let cumulative_ok = !cumulative_constraint_active
                || (cumulative_sum + flavour.error) / (cumulative_count as f64 + 1.0) <= threshold;
            let window_ok = (window_sum + flavour.error) / (window_count + 1.0) <= threshold;
            cumulative_ok && window_ok
        })
        .min_by(|left, right| left.duration.total_cmp(&right.duration))
        .ok_or_else(|| {
            SolverError::Algorithm(
                "no flavour satisfies the cumulative and window error limits for late work"
                    .to_owned(),
            )
        })
}

/// Randomized strategy that favours low-carbon, low-capacity-cost slots.
///
/// The flavour distribution still targets the configured expected error,
/// while slot probabilities are inversely proportional to forecast carbon
/// intensity multiplied by the capacity tier at that slot's next position.
/// The algorithm keeps only an atomic stream counter, so concurrent solves
/// can use reproducible independent random streams without a shared RNG lock.
pub struct HazardAlgorithm {
    seed: u64,
    next_stream: AtomicU64,
}

impl HazardAlgorithm {
    /// Create a hazard strategy with a reproducible base seed.
    pub fn new(seed: u64) -> Self {
        Self {
            seed,
            next_stream: AtomicU64::new(0),
        }
    }
}

impl SolverAlgorithm for HazardAlgorithm {
    type State = ();
    type Delta = ();

    fn initial_state(&self, _scope: &SolverScope) {}

    fn solve(
        &self,
        _snapshot: &(),
        scope: &SolverScope,
        input: &SolveInput,
    ) -> Result<(SolverProposal, ()), SolverError> {
        if input.requests.is_empty() {
            return Ok((SolverProposal::default(), ()));
        }
        validate_solve_input(input, scope)?;

        // Give concurrent solve calls disjoint deterministic streams instead
        // of serializing on a shared random-number-generator mutex.
        let stream = self
            .next_stream
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |value| {
                value.checked_add(1)
            })
            .map_err(|_| {
                SolverError::Algorithm("hazard solver stream counter exhausted".to_owned())
            })?;
        let mut rng = SmallRng::seed_from_u64(self.seed ^ stream);
        let mut placements = Vec::with_capacity(input.requests.len());

        // Capacity tiers price the next request at a slot's next occupancy
        // position. Updating this local map after every selection makes the
        // following request's hazard distribution account for earlier picks.
        let mut slot_counts = input.baseline_slot_counts.clone();
        let mut cumulative_count = input.qos.cumulative_err.count as i64;
        let mut cumulative_sum = input.qos.cumulative_err.sum;
        let mut window_count = input.qos.error_window.baseline_err.count;
        let mut window_sum = input.qos.error_window.baseline_err.sum;

        // Preserve the former flavour policy: its expected error equals the
        // configured threshold, though an individual random run can differ.
        let flavour_errors: Vec<f64> = input
            .qos
            .flavours
            .iter()
            .map(|flavour| flavour.error)
            .collect();
        let flavour_probabilities = if input
            .requests
            .iter()
            .any(|request| input.current_slot <= request.deadline_slot)
        {
            Some(
                closest_to_uniform_flavour_probabilities(
                    &flavour_errors,
                    input.qos.max_average_error,
                )
                .map_err(SolverError::Algorithm)?,
            )
        } else {
            None
        };

        for request in &input.requests {
            if input.current_slot > request.deadline_slot {
                let flavour = cheapest_feasible_flavour(
                    &input.qos.flavours,
                    input.qos.max_average_error,
                    input.qos.cumulative_error_constraint_active,
                    cumulative_count,
                    cumulative_sum,
                    window_count,
                    window_sum,
                )?;

                // Expired work has no on-time feasible slot. Keep the existing
                // explicit late-work behavior, and include it in occupancy so
                // later requests see the correct capacity tier.
                increment_slot_count(&mut slot_counts, input.current_slot)?;
                placements.push(ProposedPlacement::late(
                    request.request_id,
                    input.current_slot,
                    flavour.name.clone(),
                ));
                cumulative_count = cumulative_count.checked_add(1).ok_or_else(|| {
                    SolverError::Algorithm("cumulative error count overflowed i64".to_owned())
                })?;
                cumulative_sum += flavour.error;
                window_count += 1.0;
                window_sum += flavour.error;
                continue;
            }

            let (earliest, forecast_deadline) = request_slot_bounds(input, request, scope)?;
            let latest_shift_slot = request
                .arrival_slot
                .checked_add(input.assignment_max_future_slots)
                .ok_or_else(|| {
                    SolverError::Algorithm(format!(
                        "request {} assignment window overflowed",
                        request.request_id
                    ))
                })?;

            // Both bounds matter: the forecast/QoS deadline was capped by
            // request_slot_bounds, then this policy applies the independent
            // maximum-shift limit measured from the original arrival slot.
            let latest = forecast_deadline.min(latest_shift_slot);
            if earliest > latest {
                return Err(SolverError::Algorithm(format!(
                    "request {} has no feasible slot within its deadline and assignment window",
                    request.request_id
                )));
            }

            let candidate_slots: Vec<i32> = (earliest..=latest).collect();
            let slot_probabilities = inverse_carbon_slot_probabilities(
                &candidate_slots,
                &input.carbon_forecast,
                &input.capacity_tiers,
                &slot_counts,
            )
            .map_err(SolverError::Algorithm)?;
            let selected_slot_index = sample_probability_index(&slot_probabilities, &mut rng)
                .map_err(SolverError::Algorithm)?;
            let selected_slot = candidate_slots[selected_slot_index];

            // The slot lottery and flavour lottery are intentionally separate:
            // capacity-adjusted carbon chooses *when* to run, while the
            // threshold-calibrated distribution chooses *how* to run.
            let probabilities = flavour_probabilities.as_deref().ok_or_else(|| {
                SolverError::Algorithm(
                    "on-time hazard placement has no flavour distribution".to_owned(),
                )
            })?;
            let (_, flavour_index) = select_random_assignment(
                RandomSelectionMode::Flavour,
                selected_slot,
                selected_slot,
                &input.qos.flavours,
                probabilities,
                &mut rng,
            )
            .map_err(SolverError::Algorithm)?;
            let flavour = &input.qos.flavours[flavour_index];

            placements.push(ProposedPlacement::new(
                request.request_id,
                selected_slot,
                flavour.name.clone(),
            ));
            increment_slot_count(&mut slot_counts, selected_slot)?;
            cumulative_count = cumulative_count.checked_add(1).ok_or_else(|| {
                SolverError::Algorithm("cumulative error count overflowed i64".to_owned())
            })?;
            cumulative_sum += flavour.error;
            window_count += 1.0;
            window_sum += flavour.error;
        }

        Ok((SolverProposal { placements }, ()))
    }

    fn merge(&self, _state: &mut (), _delta: (), _committed: &CommitReport) {}
}

/// Convert each feasible slot's capacity-adjusted forecast cost into a
/// normalized inverse-cost probability.
///
/// Scaling every reciprocal by the minimum cost preserves the distribution
/// while keeping each weight at or below one. When a candidate has zero cost,
/// zero-cost slots share all probability; positive-cost slots get none.
fn inverse_carbon_slot_probabilities(
    candidate_slots: &[i32],
    carbon_forecast: &[f64],
    capacity_tiers: &[CapacityTier],
    slot_counts: &HashMap<i32, i64>,
) -> Result<Vec<f64>, String> {
    if candidate_slots.is_empty() {
        return Err("at least one candidate slot is required".to_owned());
    }

    let mut effective_costs = Vec::with_capacity(candidate_slots.len());
    for &slot in candidate_slots {
        let slot_index = usize::try_from(slot)
            .map_err(|_| format!("candidate slot {slot} cannot index the forecast"))?;
        let intensity = carbon_forecast
            .get(slot_index)
            .copied()
            .ok_or_else(|| format!("candidate slot {slot} is outside the carbon forecast"))?;
        let current_count = slot_counts.get(&slot).copied().unwrap_or(0);
        let next_position = current_count
            .checked_add(1)
            .ok_or_else(|| format!("slot {slot} occupancy overflowed i64"))?;
        let capacity_multiplier = get_capacity_multiplier(capacity_tiers, next_position);
        let effective_cost = intensity * capacity_multiplier;

        if !intensity.is_finite()
            || intensity < 0.0
            || !capacity_multiplier.is_finite()
            || capacity_multiplier < 0.0
            || !effective_cost.is_finite()
            || effective_cost < 0.0
        {
            return Err(format!(
                "slot {slot} has an invalid forecast intensity or capacity-adjusted cost"
            ));
        }
        effective_costs.push(effective_cost);
    }

    let minimum_cost = effective_costs
        .iter()
        .copied()
        .fold(f64::INFINITY, f64::min);
    let mut weights = if minimum_cost == 0.0 {
        effective_costs
            .iter()
            .map(|cost| if *cost == 0.0 { 1.0 } else { 0.0 })
            .collect::<Vec<_>>()
    } else {
        effective_costs
            .iter()
            .map(|cost| minimum_cost / cost)
            .collect::<Vec<_>>()
    };

    let total_weight: f64 = weights.iter().sum();
    if !total_weight.is_finite() || total_weight <= 0.0 {
        return Err("could not normalize inverse carbon-cost probabilities".to_owned());
    }
    for weight in &mut weights {
        *weight /= total_weight;
    }

    Ok(weights)
}

/// Sample one index from a non-empty, finite, non-negative distribution.
fn sample_probability_index<R: Rng + ?Sized>(
    probabilities: &[f64],
    rng: &mut R,
) -> Result<usize, String> {
    if probabilities.is_empty()
        || probabilities
            .iter()
            .any(|probability| !probability.is_finite() || *probability < 0.0)
    {
        return Err(
            "selection probabilities must be non-empty, finite, and non-negative".to_owned(),
        );
    }

    let total_probability: f64 = probabilities.iter().sum();
    if !total_probability.is_finite() || total_probability <= 0.0 {
        return Err("selection probabilities must have a positive finite sum".to_owned());
    }

    let selected_value = rng.gen_range(0.0..total_probability);
    let mut cumulative_probability = 0.0;
    for (index, probability) in probabilities.iter().enumerate() {
        cumulative_probability += probability;
        if selected_value < cumulative_probability {
            return Ok(index);
        }
    }

    // The independently summed total and cumulative sum can differ by a few
    // floating-point ulps. Choose the last positive bucket at that boundary.
    probabilities
        .iter()
        .rposition(|probability| *probability > 0.0)
        .ok_or_else(|| "selection probabilities contain no positive entry".to_owned())
}

fn increment_slot_count(slot_counts: &mut HashMap<i32, i64>, slot: i32) -> Result<(), SolverError> {
    let count = slot_counts.entry(slot).or_insert(0);
    *count = count
        .checked_add(1)
        .ok_or_else(|| SolverError::Algorithm(format!("slot {slot} occupancy overflowed i64")))?;
    Ok(())
}

/// Register deterministic strategies without adding solver-specific branches
/// to either runner.
pub fn register_stateless_solvers(
    registry: &mut SolverRegistry,
    dp_config: DpAlgorithmConfig,
    hazard_seed: u64,
) -> Result<(), SolverError> {
    registry.register("dp", move || DpAlgorithm {
        config: dp_config.clone(),
    })?;
    registry.register("singleton_best", || {
        SingletonBestAlgorithm::new(SingletonBestFlavourPolicy::AllProfileFlavours)
    })?;
    registry.register("singleton_best_accurate", || {
        SingletonBestAlgorithm::new(SingletonBestFlavourPolicy::MostAccurateOnly)
    })?;
    registry.register("hazard", move || HazardAlgorithm::new(hazard_seed))?;

    Ok(())
}

/// Register the persistent online solvers without exposing their concrete
/// state or delta types to either runner.
pub fn register_stateful_solvers(
    registry: &mut SolverRegistry,
    config: &SwarmConfig,
) -> Result<(), SolverError> {
    let bandit_config = bandit::BanditConfig {
        epsilon: config.bandit_epsilon,
        initial_q: config.bandit_initial_q,
        seed: config.bandit_seed,
    };
    registry.register("bandit", move || {
        bandit::BanditAlgorithm::new(bandit_config.clone())
    })?;

    let aco_config = aco::AcoConfig {
        n_ants: config.aco_n_ants,
        n_iterations: config.aco_n_iterations,
        alpha: config.aco_alpha,
        beta: config.aco_beta,
        rho: config.aco_rho,
        q_deposit: config.aco_q,
        initial_pheromone: config.aco_tau0,
        seed: config.aco_seed,
    };
    registry.register("ant_colony", move || {
        aco::AcoAlgorithm::new(aco_config.clone())
    })?;

    Ok(())
}

/// Compose the full engine registry from the shared solver configuration.
///
/// The function only registers implementations. Each runner remains
/// responsible for choosing an engine name, creating a scoped runtime, and
/// settling proposed placements through its own commit adapter.
pub fn register_all_solvers(
    registry: &mut SolverRegistry,
    dp_config: DpAlgorithmConfig,
    swarm_config: &SwarmConfig,
    hazard_seed: u64,
) -> Result<(), SolverError> {
    register_stateless_solvers(registry, dp_config, hazard_seed)?;
    register_stateful_solvers(registry, swarm_config)?;
    Ok(())
}

// Helper functions

fn validate_solve_input(input: &SolveInput, scope: &SolverScope) -> Result<(), SolverError> {
    let horizon = usize::try_from(scope.total_slots)
        .map_err(|_| SolverError::Algorithm("planning horizon must be positive".to_owned()))?;
    if horizon == 0 {
        return Err(SolverError::Algorithm(
            "planning horizon must be positive".to_owned(),
        ));
    }
    if input.carbon_forecast.len() != horizon {
        return Err(SolverError::Algorithm(format!(
            "forecast length {} does not match planning horizon {horizon}",
            input.carbon_forecast.len()
        )));
    }

    // The forecast vector is indexed by absolute solver slots, so it must
    // cover the configured local horizon. A rolling provider window is
    // represented separately by `assignment_max_slot`; request deadlines are
    // capped to that slot and to the QoS future-window boundary below.

    if input.current_slot < 0 || input.current_slot >= scope.total_slots as i32 {
        return Err(SolverError::Algorithm(format!(
            "current slot {} is outside the planning horizon",
            input.current_slot
        )));
    }
    if input.qos.error_window.past_slots < 0 || input.qos.error_window.future_slots < 0 {
        return Err(SolverError::Algorithm(
            "error-window slot counts cannot be negative".to_owned(),
        ));
    }
    if input.assignment_max_future_slots < 0 {
        return Err(SolverError::Algorithm(
            "maximum assignment shift cannot be negative".to_owned(),
        ));
    }
    if input
        .assignment_max_slot
        .is_some_and(|max_slot| max_slot < input.current_slot)
    {
        return Err(SolverError::Algorithm(
            "assignment cap is earlier than the current slot".to_owned(),
        ));
    }
    if !input.carbon_cost_duration_scale.is_finite()
        || input.carbon_cost_duration_scale < 0.0
        || input.carbon_forecast.iter().any(|value| !value.is_finite())
    {
        return Err(SolverError::Algorithm(
            "carbon forecast and duration scale must be finite and non-negative".to_owned(),
        ));
    }
    if !input.qos.max_average_error.is_finite() {
        return Err(SolverError::Algorithm(
            "sliding-window error threshold must be finite".to_owned(),
        ));
    }
    if !input.qos.cumulative_err.count.is_finite()
        || input.qos.cumulative_err.count < 0.0
        || input.qos.cumulative_err.count.fract() != 0.0
        || input.qos.cumulative_err.count > (i64::MAX as f64)
        || !input.qos.cumulative_err.sum.is_finite()
        || !input.qos.error_window.baseline_err.sum.is_finite()
        || !input.qos.error_window.baseline_err.count.is_finite()
        || input.qos.error_window.baseline_err.count < 0.0
    {
        return Err(SolverError::Algorithm(
            "QoS error sums must be finite and error counts non-negative".to_owned(),
        ));
    }
    if input.baseline_slot_counts.values().any(|count| *count < 0) {
        return Err(SolverError::Algorithm(
            "baseline slot counts cannot be negative".to_owned(),
        ));
    }

    if !input.requests.is_empty() && input.qos.flavours.is_empty() {
        return Err(SolverError::Algorithm(
            "the QoS profile has no available flavours".to_owned(),
        ));
    }

    if input.qos.flavours.is_empty() {
        return Err(SolverError::EmptyFlavours);
    }

    if input.qos.flavours.iter().any(|flavour| {
        !flavour.error.is_finite() || !flavour.duration.is_finite() || flavour.duration < 0.0
    }) {
        return Err(SolverError::Algorithm(
            "QoS profile contains a flavour with invalid error or duration".to_owned(),
        ));
    }

    for request in &input.requests {
        request_slot_bounds(input, request, scope)?;
    }
    Ok(())
}

/// Cap a request's deadline to slots for which this solve has forecast data
/// or for whose future assignment horizon is valid.
/// Already-expired work is retained at the current slot so the algorithm can
/// mark it late and choose its explicit late-work fallback.
fn cap_deadline_inside_forecast(
    input: &SolveInput,
    request: &SolveRequest,
    scope: &SolverScope,
) -> Result<i32, SolverError> {
    if input.current_slot > request.deadline_slot {
        return Ok(input.current_slot);
    }

    let cap = request
        .deadline_slot
        .min(latest_forecast_slot(input, scope));
    let earliest = request.arrival_slot.max(input.current_slot);
    if cap < earliest {
        return Err(SolverError::Algorithm(format!(
            "request {} starts at slot {earliest}, beyond forecast-covered deadline {cap}",
            request.request_id
        )));
    }
    Ok(cap)
}

fn request_slot_bounds(
    input: &SolveInput,
    request: &SolveRequest,
    scope: &SolverScope,
) -> Result<(i32, i32), SolverError> {
    Ok((
        request.arrival_slot.max(input.current_slot),
        cap_deadline_inside_forecast(input, request, scope)?,
    ))
}

fn latest_forecast_slot(input: &SolveInput, scope: &SolverScope) -> i32 {
    let total_slots: i32 = scope.total_slots as i32;
    (total_slots - 1)
        .min(input.current_slot + input.qos.error_window.future_slots)
        .min(input.assignment_max_slot.unwrap_or(total_slots - 1))
}

// ----------------------------------------------------------------------------
// ----------------------------------------------------------------------------
// ---------------------------------TESTS--------------------------------------

#[cfg(test)]
mod tests {
    use std::collections::{HashMap, HashSet};

    use crate::engine::qos::QosProfileId;
    use crate::engine::solver::{
        ErrorTotals, ErrorWindowInput, PendingProposal, QoSProfileInput, SolveInput, SolveRequest,
        SolverRegistry, SolverRuntime, SolverScope,
    };
    use crate::types::{CapacityTier, Flavour};

    use super::{
        DpAlgorithmConfig, inverse_carbon_slot_probabilities, register_all_solvers,
        register_stateless_solvers,
    };

    fn config() -> DpAlgorithmConfig {
        DpAlgorithmConfig {
            pruning_method: "none".to_owned(),
            pruning_min_batch_size: 0,
            pruning_k: 100,
            timeout_secs: 5.0,
        }
    }

    fn input() -> SolveInput {
        SolveInput {
            current_slot: 1,
            requests: vec![
                SolveRequest {
                    request_id: 10,
                    arrival_slot: 0,
                    deadline_slot: 3,
                },
                SolveRequest {
                    request_id: 11,
                    arrival_slot: 1,
                    deadline_slot: 3,
                },
            ],
            carbon_forecast: vec![20.0, 10.0, 5.0, 1.0],
            capacity_tiers: vec![CapacityTier {
                max_requests: None,
                multiplier: 1.0,
            }],
            baseline_slot_counts: HashMap::new(),
            carbon_cost_duration_scale: 1.0,
            assignment_max_future_slots: 2,
            assignment_max_slot: Some(3),
            qos: QoSProfileInput {
                profile_id: QosProfileId::default_profile(),
                flavours: vec![Flavour {
                    name: "Default".to_owned(),
                    error: 0.0,
                    duration: 10.0,
                }],
                cumulative_error_constraint_active: false,
                cumulative_err: ErrorTotals {
                    count: 0.0,
                    sum: 0.0,
                },
                err_by_slot: HashMap::new(),
                max_average_error: 4.0,
                error_window: ErrorWindowInput {
                    baseline_err: ErrorTotals {
                        count: 0.0,
                        sum: 0.0,
                    },
                    past_slots: 0,
                    future_slots: 3,
                },
            },
        }
    }

    fn runtime_for(name: &str) -> Box<dyn SolverRuntime> {
        runtime_for_scope(name, scope())
    }

    fn runtime_for_scope(name: &str, solver_scope: SolverScope) -> Box<dyn SolverRuntime> {
        let mut registry = SolverRegistry::default();
        register_stateless_solvers(&mut registry, config(), 42).unwrap();
        registry.create(name, solver_scope).unwrap()
    }

    fn scope() -> SolverScope {
        scope_with_total_slots(4)
    }

    fn scope_with_total_slots(total_slots: usize) -> SolverScope {
        SolverScope {
            profile_id: QosProfileId::default_profile(),
            total_slots,
        }
    }

    fn runtime() -> Box<dyn SolverRuntime> {
        runtime_for("dp")
    }

    fn propose(runtime: &dyn SolverRuntime, input: &SolveInput) -> PendingProposal {
        runtime.propose(input).unwrap()
    }

    fn placements_for(name: &str, solve_input: &SolveInput) -> Vec<super::ProposedPlacement> {
        placements_for_scope(name, solve_input, scope())
    }

    fn placements_for_scope(
        name: &str,
        solve_input: &SolveInput,
        solver_scope: SolverScope,
    ) -> Vec<super::ProposedPlacement> {
        let runtime = runtime_for_scope(name, solver_scope);
        let pending = propose(&*runtime, solve_input);
        let placements = pending.proposal.placements.clone();
        runtime.discard(pending.id).unwrap();
        placements
    }

    #[test]
    fn dp_proposes_all_requests_from_one_joint_batch() {
        let runtime = runtime();
        let pending = propose(&*runtime, &input());
        let ids: HashSet<u64> = pending
            .proposal
            .placements
            .iter()
            .map(|placement| placement.req_id)
            .collect();

        assert_eq!(pending.proposal.placements.len(), 2);
        assert_eq!(ids, HashSet::from([10, 11]));
        assert!(pending.proposal.placements.iter().all(|placement| {
            (1..=3).contains(&placement.scheduled_slot) && placement.flavour_name == "Default"
        }));

        runtime.discard(pending.id).unwrap();
    }

    #[test]
    fn dp_uses_the_profile_flavour_set_for_the_whole_batch() {
        let runtime = runtime();
        let mut solve_input = input();
        solve_input.qos.flavours = vec![Flavour {
            name: "ProfileFlavour".to_owned(),
            error: 0.0,
            duration: 1.0,
        }];

        let pending = propose(&*runtime, &solve_input);

        assert!(
            pending
                .proposal
                .placements
                .iter()
                .all(|placement| placement.flavour_name == "ProfileFlavour")
        );
        runtime.discard(pending.id).unwrap();
    }

    #[test]
    fn singleton_best_uses_the_profile_flavour_set_for_every_request() {
        let mut solve_input = input();
        solve_input.qos.flavours = vec![Flavour {
            name: "ProfileFlavour".to_owned(),
            error: 0.0,
            duration: 1.0,
        }];

        let placements = placements_for("singleton_best", &solve_input);

        assert_eq!(placements.len(), solve_input.requests.len());
        assert!(
            placements
                .iter()
                .all(|placement| placement.flavour_name == "ProfileFlavour")
        );
    }

    #[test]
    fn singleton_best_updates_slot_occupancy_after_each_request() {
        let mut solve_input = input();
        solve_input.current_slot = 0;
        solve_input.requests = vec![
            SolveRequest {
                request_id: 1,
                arrival_slot: 0,
                deadline_slot: 1,
            },
            SolveRequest {
                request_id: 2,
                arrival_slot: 0,
                deadline_slot: 1,
            },
        ];
        solve_input.qos.flavours = vec![Flavour {
            name: "One".to_owned(),
            error: 0.0,
            duration: 1.0,
        }];
        solve_input.carbon_forecast = vec![1.0, 1.0];
        solve_input.capacity_tiers = vec![
            CapacityTier {
                max_requests: Some(1),
                multiplier: 1.0,
            },
            CapacityTier {
                max_requests: None,
                multiplier: 10.0,
            },
        ];
        solve_input.assignment_max_future_slots = 1;
        solve_input.assignment_max_slot = Some(1);

        let placements =
            placements_for_scope("singleton_best", &solve_input, scope_with_total_slots(2));

        assert_eq!(placements[0].scheduled_slot, 0);
        assert_eq!(placements[1].scheduled_slot, 1);
    }

    #[test]
    fn singleton_best_handles_occupancy_above_i32_max() {
        let mut solve_input = input();
        solve_input.current_slot = 0;
        solve_input.requests.truncate(1);
        solve_input.requests[0].arrival_slot = 0;
        solve_input.requests[0].deadline_slot = 1;
        solve_input.qos.flavours = vec![Flavour {
            name: "One".to_owned(),
            error: 0.0,
            duration: 1.0,
        }];
        solve_input.carbon_forecast = vec![1.0, 2.0];
        solve_input.capacity_tiers = vec![
            CapacityTier {
                max_requests: Some(i64::from(i32::MAX)),
                multiplier: 1.0,
            },
            CapacityTier {
                max_requests: None,
                multiplier: 10.0,
            },
        ];
        solve_input
            .baseline_slot_counts
            .insert(0, i64::from(i32::MAX) + 1);
        solve_input.assignment_max_future_slots = 1;
        solve_input.assignment_max_slot = Some(1);

        let placements =
            placements_for_scope("singleton_best", &solve_input, scope_with_total_slots(2));

        assert_eq!(placements[0].scheduled_slot, 1);
    }

    #[test]
    fn singleton_best_counts_the_candidate_and_prior_placements_in_the_window() {
        let mut solve_input = input();
        solve_input.current_slot = 0;
        solve_input.requests = vec![
            SolveRequest {
                request_id: 1,
                arrival_slot: 0,
                deadline_slot: 1,
            },
            SolveRequest {
                request_id: 2,
                arrival_slot: 0,
                deadline_slot: 1,
            },
        ];
        solve_input.qos.flavours = vec![
            Flavour {
                name: "Accurate".to_owned(),
                error: 0.0,
                duration: 10.0,
            },
            Flavour {
                name: "Fast".to_owned(),
                error: 4.0,
                duration: 1.0,
            },
        ];
        solve_input.carbon_forecast = vec![1.0; 4];
        solve_input.qos.max_average_error = 2.0;
        solve_input.qos.error_window.future_slots = 1;

        let placements = placements_for("singleton_best", &solve_input);

        assert_eq!(placements[0].flavour_name, "Accurate");
        assert_eq!(placements[1].flavour_name, "Fast");
    }

    #[test]
    fn singleton_best_preserves_fractional_weighted_error_counts() {
        let mut solve_input = input();
        solve_input.requests.truncate(1);
        solve_input.qos.flavours = vec![
            Flavour {
                name: "Accurate".to_owned(),
                error: 0.0,
                duration: 10.0,
            },
            Flavour {
                name: "Fast".to_owned(),
                error: 4.0,
                duration: 1.0,
            },
        ];
        solve_input.qos.max_average_error = 2.0;
        solve_input.qos.error_window.baseline_err = ErrorTotals {
            count: 1.5,
            sum: 1.0,
        };

        let placements = placements_for("singleton_best", &solve_input);

        assert_eq!(placements[0].flavour_name, "Fast");
    }

    #[test]
    fn expired_requests_use_the_cheapest_flavour_feasible_for_profile_budgets() {
        let mut solve_input = input();
        solve_input.current_slot = 2;
        solve_input.requests.truncate(1);
        solve_input.requests[0].arrival_slot = 0;
        solve_input.requests[0].deadline_slot = 1;
        solve_input.qos.flavours = vec![
            Flavour {
                name: "Cheap".to_owned(),
                error: 8.0,
                duration: 1.0,
            },
            Flavour {
                name: "Safe".to_owned(),
                error: 2.0,
                duration: 5.0,
            },
        ];
        solve_input.qos.cumulative_error_constraint_active = true;
        solve_input.qos.cumulative_err.count = 1.0;
        solve_input.qos.cumulative_err.sum = 5.0;
        solve_input.qos.error_window.baseline_err.count = 1.0;
        solve_input.qos.error_window.baseline_err.sum = 1.0;

        for name in ["dp", "singleton_best", "hazard"] {
            let placements = placements_for(name, &solve_input);
            assert_eq!(placements.len(), 1);
            assert_eq!(placements[0].scheduled_slot, solve_input.current_slot);
            assert_eq!(placements[0].flavour_name, "Safe");
            assert!(placements[0].late);
        }
    }

    #[test]
    fn all_algorithms_cap_on_time_deadlines_to_forecast_availability() {
        let mut solve_input = input();
        solve_input.current_slot = 0;
        solve_input.qos.error_window.future_slots = 1;
        solve_input.assignment_max_future_slots = 3;
        solve_input.qos.flavours = vec![
            Flavour {
                name: "Accurate".to_owned(),
                error: 0.0,
                duration: 10.0,
            },
            Flavour {
                name: "Fast".to_owned(),
                error: 8.0,
                duration: 1.0,
            },
        ];
        solve_input.requests[0].arrival_slot = 0;
        solve_input.requests[0].deadline_slot = 3;
        solve_input.requests.truncate(1);

        for name in ["dp", "singleton_best", "hazard"] {
            let placements = placements_for(name, &solve_input);
            assert_eq!(placements.len(), 1);
            assert!(placements[0].scheduled_slot <= 1);
            assert!(!placements[0].late);
        }
    }

    #[test]
    fn singleton_best_falls_back_to_the_lowest_error_flavour() {
        let mut solve_input = input();
        solve_input.current_slot = 0;
        solve_input.requests.truncate(1);
        solve_input.qos.flavours = vec![
            Flavour {
                name: "Fast".to_owned(),
                error: 4.0,
                duration: 1.0,
            },
            Flavour {
                name: "Accurate".to_owned(),
                error: 2.0,
                duration: 3.0,
            },
        ];
        solve_input.qos.max_average_error = 1.0;

        let placements = placements_for("singleton_best", &solve_input);

        assert_eq!(placements[0].flavour_name, "Accurate");
        assert_eq!(placements[0].scheduled_slot, 0);
    }

    #[test]
    fn singleton_best_accurate_uses_only_the_profile_flavours_with_minimum_error() {
        let mut solve_input = input();
        solve_input.current_slot = 0;
        solve_input.requests.truncate(1);
        solve_input.qos.flavours = vec![
            Flavour {
                name: "Fast".to_owned(),
                error: 4.0,
                duration: 1.0,
            },
            Flavour {
                name: "Accurate".to_owned(),
                error: 0.0,
                duration: 10.0,
            },
        ];
        solve_input.carbon_forecast = vec![1.0; 4];

        let placements = placements_for("singleton_best_accurate", &solve_input);

        assert_eq!(placements.len(), 1);
        assert_eq!(placements[0].flavour_name, "Accurate");
    }

    #[test]
    fn registered_hazard_produces_valid_profile_flavour_placements() {
        let mut solve_input = input();
        solve_input.current_slot = 0;
        solve_input.qos.flavours = vec![
            Flavour {
                name: "Accurate".to_owned(),
                error: 0.0,
                duration: 10.0,
            },
            Flavour {
                name: "Fast".to_owned(),
                error: 8.0,
                duration: 1.0,
            },
        ];
        solve_input.qos.error_window.future_slots = 3;
        solve_input.qos.cumulative_error_constraint_active = true;

        let placements = placements_for("hazard", &solve_input);
        assert_eq!(placements.len(), solve_input.requests.len());
        for placement in placements {
            let request = solve_input
                .requests
                .iter()
                .find(|request| request.request_id == placement.req_id)
                .unwrap();
            let latest = request
                .deadline_slot
                .min(solve_input.assignment_max_slot.unwrap())
                .min(request.arrival_slot + solve_input.assignment_max_future_slots);
            assert!(
                (request.arrival_slot.max(solve_input.current_slot)..=latest)
                    .contains(&placement.scheduled_slot)
            );
            assert!(["Accurate", "Fast"].contains(&placement.flavour_name.as_str()));
            assert!(!placement.late);
        }
    }

    #[test]
    fn hazard_accepts_a_request_with_only_one_legal_slot() {
        let mut solve_input = input();
        solve_input.current_slot = 0;
        solve_input.requests.truncate(1);
        solve_input.requests[0].arrival_slot = 0;
        solve_input.requests[0].deadline_slot = 0;
        solve_input.qos.flavours = vec![
            Flavour {
                name: "Accurate".to_owned(),
                error: 0.0,
                duration: 10.0,
            },
            Flavour {
                name: "Fast".to_owned(),
                error: 8.0,
                duration: 1.0,
            },
        ];
        solve_input.carbon_forecast = vec![1.0];
        solve_input.assignment_max_future_slots = 0;
        solve_input.assignment_max_slot = Some(0);

        let placements = placements_for_scope("hazard", &solve_input, scope_with_total_slots(1));

        assert_eq!(placements.len(), 1);
        assert_eq!(placements[0].scheduled_slot, 0);
    }

    #[test]
    fn hazard_rejects_a_request_with_no_slot_inside_its_shift_window() {
        let mut solve_input = input();
        solve_input.current_slot = 2;
        solve_input.requests.truncate(1);
        solve_input.requests[0].arrival_slot = 0;
        solve_input.requests[0].deadline_slot = 3;
        solve_input.assignment_max_future_slots = 1;
        solve_input.qos.flavours = vec![
            Flavour {
                name: "Accurate".to_owned(),
                error: 0.0,
                duration: 10.0,
            },
            Flavour {
                name: "Fast".to_owned(),
                error: 4.0,
                duration: 1.0,
            },
        ];

        let runtime = runtime_for("hazard");
        assert!(runtime.propose(&solve_input).is_err());
    }

    #[test]
    fn hazard_returns_flavour_probability_errors_instead_of_panicking() {
        let mut solve_input = input();
        solve_input.requests.truncate(1);
        let runtime = runtime_for("hazard");

        assert!(runtime.propose(&solve_input).is_err());
    }

    #[test]
    fn hazard_slot_probabilities_use_capacity_adjusted_forecast_cost() {
        let tiers = vec![
            CapacityTier {
                max_requests: Some(1),
                multiplier: 1.0,
            },
            CapacityTier {
                max_requests: None,
                multiplier: 4.0,
            },
        ];
        let slot_counts = HashMap::from([(1, 1)]);

        // Slot 0's next position is in the first tier: 10 × 1 = 10.
        // Slot 1 already has one request, so its next position is in the
        // overflow tier: 5 × 4 = 20. Inverse costs therefore normalize 2:1.
        let probabilities =
            inverse_carbon_slot_probabilities(&[0, 1], &[10.0, 5.0], &tiers, &slot_counts).unwrap();

        assert!((probabilities[0] - 2.0 / 3.0).abs() < 1e-12);
        assert!((probabilities[1] - 1.0 / 3.0).abs() < 1e-12);
    }

    #[test]
    fn hazard_assigns_all_probability_to_zero_effective_cost_slots() {
        let probabilities =
            inverse_carbon_slot_probabilities(&[0, 1, 2], &[0.0, 10.0, 0.0], &[], &HashMap::new())
                .unwrap();

        assert_eq!(probabilities, vec![0.5, 0.0, 0.5]);
    }

    #[test]
    fn all_engines_are_available_from_the_common_registry_composition() {
        let application_config = crate::engine::config::Config::default();
        let mut registry = SolverRegistry::default();
        register_all_solvers(&mut registry, config(), &application_config.swarm, 42).unwrap();
        let scope = scope();

        for name in [
            "dp",
            "singleton_best",
            "singleton_best_accurate",
            "hazard",
            "bandit",
            "ant_colony",
        ] {
            assert!(
                registry.create(name, scope.clone()).is_ok(),
                "engine {name} should be registered"
            );
        }
    }
}
