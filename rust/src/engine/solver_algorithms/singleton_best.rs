//! Greedy per-request search used by online and offline singleton strategies.

use crate::engine::solver::{
    CommitReport, ProposedPlacement, SolveInput, SolverAlgorithm, SolverError, SolverProposal,
    SolverScope,
};
use crate::types::{Flavour, get_capacity_multiplier};

use super::shared::{
    cheapest_feasible_flavour, get_fallback_flav, request_slot_bounds, validate_solve_input,
};

/// Chooses which profile flavours the singleton search may consider.
#[derive(Debug, Clone, Copy)]
pub enum SingletonBestFlavourPolicy {
    /// Search every flavour supplied by the request's QoS profile.
    AllProfileFlavours,
    /// Restrict the search to the profile's lowest-error flavour.
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

        let flavours = match self.flavour_policy {
            SingletonBestFlavourPolicy::AllProfileFlavours => input.qos.flavours.clone(),
            SingletonBestFlavourPolicy::MostAccurateOnly => {
                vec![get_fallback_flav(&input.qos.flavours)?.clone()]
            }
        };

        let mut placements = Vec::with_capacity(input.requests.len());
        let mut slot_count = input.baseline_slot_counts.clone();
        let mut solved_error_by_slot = vec![(0.0_f64, 0.0_f64); scope.total_slots];
        let mut cumulative_count = input.qos.cumulative_err.count as i64;
        let mut cumulative_sum = input.qos.cumulative_err.sum;
        let mut window_count = input.qos.error_window.baseline_err.count;
        let mut window_sum = input.qos.error_window.baseline_err.sum;

        let mut sorted_flavours: Vec<&Flavour> = flavours.iter().collect();
        sorted_flavours.sort_by(|left, right| left.duration.total_cmp(&right.duration));

        for request in &input.requests {
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
                let (slot_sum, slot_count) = &mut solved_error_by_slot[input.current_slot as usize];
                *slot_sum += flavour.error;
                *slot_count += 1.0;
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

            let (start_slot, deadline) = request_slot_bounds(input, request, scope)?;
            let window_center_slot = request.arrival_slot.max(input.current_slot);
            let win_start = (window_center_slot - input.qos.error_window.past_slots).max(0);
            let win_end = (window_center_slot + input.qos.error_window.future_slots)
                .min(scope.total_slots as i32 - 1);
            let (local_win_sum, local_win_count) = solved_error_by_slot
                [win_start as usize..=win_end as usize]
                .iter()
                .fold((0.0, 0.0), |(sum, count), (slot_sum, slot_count)| {
                    (sum + slot_sum, count + slot_count)
                });
            let win_sum_before_candidate = input.qos.error_window.baseline_err.sum + local_win_sum;
            let win_count_before_candidate =
                input.qos.error_window.baseline_err.count + local_win_count;

            let mut best: Option<(f64, i32, &Flavour)> = None;
            for slot in start_slot..=deadline {
                let carbon_intensity = input.carbon_forecast[slot as usize];
                let position = slot_count.get(&slot).copied().unwrap_or(0) + 1;
                let multiplier = get_capacity_multiplier(&input.capacity_tiers, position);

                for flavour in &sorted_flavours {
                    let cumulative_average = if cumulative_count > 0 {
                        cumulative_sum / cumulative_count as f64
                    } else {
                        0.0
                    };
                    if input.qos.cumulative_error_constraint_active
                        && cumulative_average > input.qos.max_average_error
                        && flavour.error > input.qos.max_average_error
                    {
                        continue;
                    }

                    let win_sum = win_sum_before_candidate + flavour.error;
                    let win_count = win_count_before_candidate + 1.0;
                    if win_count > 0.0 && win_sum / win_count > input.qos.max_average_error {
                        continue;
                    }

                    let cost = compute_cost(
                        carbon_intensity,
                        multiplier,
                        flavour.duration,
                        input.carbon_cost_duration_scale,
                    );
                    if best
                        .map(|(best_cost, _, _)| cost < best_cost)
                        .unwrap_or(true)
                    {
                        best = Some((cost, slot, flavour));
                    }
                }
            }

            let (chosen_slot, chosen_flavour, used_infeasibility_fallback) =
                if let Some((_, slot, flavour)) = best {
                    (slot, flavour, false)
                } else {
                    (start_slot, get_fallback_flav(&flavours)?, true)
                };

            *slot_count.entry(chosen_slot).or_insert(0) += 1;
            let (slot_sum, slot_count) = &mut solved_error_by_slot[chosen_slot as usize];
            *slot_sum += chosen_flavour.error;
            *slot_count += 1.0;
            cumulative_count = cumulative_count.checked_add(1).ok_or_else(|| {
                SolverError::Algorithm("cumulative error count overflowed i64".to_owned())
            })?;
            cumulative_sum += chosen_flavour.error;
            window_count += 1.0;
            window_sum += chosen_flavour.error;
            placements.push(if used_infeasibility_fallback {
                ProposedPlacement::infeasibility_fallback(
                    request.request_id,
                    chosen_slot,
                    chosen_flavour.name.clone(),
                )
            } else {
                ProposedPlacement::new(request.request_id, chosen_slot, chosen_flavour.name.clone())
            });
        }
        Ok((SolverProposal { placements }, ()))
    }

    fn merge(&self, _state: &mut (), _delta: (), _committed: &CommitReport) {}
}

fn compute_cost(ci: f64, multiplier: f64, duration: f64, scale: f64) -> f64 {
    ci * multiplier * duration * scale
}
