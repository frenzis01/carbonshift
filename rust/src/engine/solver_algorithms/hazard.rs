//! Randomized strategy that favours low-carbon, low-capacity-cost slots.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};

use rand::Rng;
use rand::SeedableRng;
use rand::rngs::SmallRng;

use crate::engine::random_selection::{
    RandomSelectionMode, closest_to_uniform_flavour_probabilities, select_random_assignment,
};
use crate::engine::solver::{
    CommitReport, ProposedPlacement, SolveInput, SolverAlgorithm, SolverError, SolverProposal,
    SolverScope,
};
use crate::types::{CapacityTier, get_capacity_multiplier};

use super::shared::{cheapest_feasible_flavour, request_slot_bounds, validate_solve_input};

/// Randomized strategy that favours low-carbon, low-capacity-cost slots.
///
/// The flavour distribution targets the configured expected error when it is
/// attainable, and otherwise the nearest attainable error. Slot probabilities
/// are inversely proportional to forecast carbon intensity multiplied by the
/// capacity tier at that slot's next position. The algorithm keeps only an
/// atomic stream counter, so concurrent solves can use reproducible independent
/// random streams without a shared RNG lock.
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
        let mut slot_counts = input.baseline_slot_counts.clone();
        let mut cumulative_count = input.qos.cumulative_err.count as i64;
        let mut cumulative_sum = input.qos.cumulative_err.sum;
        let mut window_count = input.qos.error_window.baseline_err.count;
        let mut window_sum = input.qos.error_window.baseline_err.sum;

        let flavour_errors: Vec<f64> = input
            .qos
            .flavours
            .iter()
            .map(|flavour| flavour.error)
            .collect();
        let min_flavour_error = flavour_errors.iter().copied().fold(f64::INFINITY, f64::min);
        let max_flavour_error = flavour_errors
            .iter()
            .copied()
            .fold(f64::NEG_INFINITY, f64::max);
        let attainable_target_error = input
            .qos
            .max_average_error
            .clamp(min_flavour_error, max_flavour_error);
        let flavour_probabilities = if input
            .requests
            .iter()
            .any(|request| input.current_slot <= request.deadline_slot)
        {
            Some(
                closest_to_uniform_flavour_probabilities(&flavour_errors, attainable_target_error)
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

            let (earliest, latest) = request_slot_bounds(input, request, scope)?;
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
pub(super) fn inverse_carbon_slot_probabilities(
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
