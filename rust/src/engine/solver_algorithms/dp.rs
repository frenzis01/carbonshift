//! Adapter that connects the shared solver contract to the joint DP engine.

use std::collections::HashSet;

use crate::engine::dp_solver::{DpSolver, ErrorWindowBaseline, SolveBatchInput};
use crate::engine::solver::{
    CommitReport, ProposedPlacement, SolveInput, SolveRequest, SolverAlgorithm, SolverError,
    SolverProposal, SolverScope,
};
use crate::types::Flavour;

use super::shared::{
    cheapest_feasible_flavour, latest_forecast_slot, request_slot_bounds, validate_solve_input,
};

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

        let mut placements = Vec::with_capacity(input.requests.len());
        let mut solver_baseline_counts = input.baseline_slot_counts.clone();
        let mut cumulative_count = input.qos.cumulative_err.count as i64;
        let mut cumulative_sum = input.qos.cumulative_err.sum;
        let mut window_count = input.qos.error_window.baseline_err.count;
        let mut window_sum = input.qos.error_window.baseline_err.sum;
        let late_request_ids: HashSet<u64> = input
            .requests
            .iter()
            .filter(|request| input.current_slot > request.deadline_slot)
            .map(|request| request.request_id)
            .collect();

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

        let on_time_requests: Vec<_> = input
            .requests
            .iter()
            .filter(|request| !late_request_ids.contains(&request.request_id))
            .map(|request| {
                let (_, deadline_slot) = request_slot_bounds(input, request, scope)?;
                Ok(SolveRequest {
                    assignment_deadline_slot: deadline_slot,
                    ..*request
                })
            })
            .collect::<Result<_, SolverError>>()?;

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
        let assignments = solver.solve_batch(SolveBatchInput {
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
            forecast_max_slot: Some(latest_forecast_slot(input, scope)),
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

            let fallback_assignments = solver.greedy_fallback(
                &unscheduled,
                input.current_slot,
                &input.capacity_tiers,
                &fallback_counts,
            );
            placements.extend(assignments.into_iter().map(|assignment| {
                ProposedPlacement::new(
                    assignment.request_id,
                    assignment.slot,
                    assignment.flavour_name,
                )
            }));
            placements.extend(fallback_assignments.into_iter().map(|assignment| {
                ProposedPlacement::greedy_fallback(
                    assignment.request_id,
                    assignment.slot,
                    assignment.flavour_name,
                )
            }));
        } else {
            placements.extend(assignments.into_iter().map(|assignment| {
                ProposedPlacement::new(
                    assignment.request_id,
                    assignment.slot,
                    assignment.flavour_name,
                )
            }));
        }
        Ok((SolverProposal { placements }, ()))
    }

    fn merge(&self, _state: &mut (), _delta: (), _committed: &CommitReport) {}
}
