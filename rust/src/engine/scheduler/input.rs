//! Convert a claim-time scheduler snapshot into the common solver contract.

use std::collections::{HashMap, HashSet};

use crate::config::AssignmentPolicy;
use crate::engine::solver::{
    ErrorTotals, ErrorWindowInput, QoSProfileInput, SolveInput, SolveRequest, SolverError,
    cap_assignment_deadline,
};
use crate::shared_state::SolverSnapshot;
use crate::types::Request;

use super::error_history::{ErrorBaseline, augment_with_decayed_past};

/// Build common solver input from one atomic claim-time snapshot.
///
/// Existing future assignments remain fixed global capacity occupancy.
/// Error totals and historical augmentation are isolated to the batch's
/// QoS profile.
pub(super) fn build_solve_input_from_snapshot(
    current_slot: i32,
    pending: &[Request],
    snapshot: &SolverSnapshot,
    assignment: &AssignmentPolicy<'_>,
    carbon_forecast: &[f64],
) -> Result<SolveInput, SolverError> {
    let first_request = pending.first().ok_or_else(|| {
        SolverError::Algorithm("cannot build solver input for an empty batch".to_owned())
    })?;
    let profile_id = first_request.qos_profile_id();
    if pending
        .iter()
        .any(|request| request.qos_profile_id() != profile_id)
    {
        return Err(SolverError::Algorithm(
            "solver input must contain requests from exactly one QoS profile".to_owned(),
        ));
    }

    let total_slots = usize::try_from(assignment.total_slots).map_err(|_| {
        SolverError::Algorithm("planning horizon must be a positive slot count".to_owned())
    })?;
    if total_slots == 0 {
        return Err(SolverError::Algorithm(
            "planning horizon must contain at least one slot".to_owned(),
        ));
    }
    if carbon_forecast.len() != total_slots {
        return Err(SolverError::Algorithm(format!(
            "carbon forecast has {} slots but the planning horizon has {total_slots}",
            carbon_forecast.len()
        )));
    }
    let forecast_max_slot = i32::try_from(carbon_forecast.len() - 1).map_err(|_| {
        SolverError::Algorithm("carbon forecast exceeds the supported slot range".to_owned())
    })?;
    let requests = pending
        .iter()
        .map(|request| {
            let assignment_deadline_slot = cap_assignment_deadline(
                request.id,
                request.arrival_slot,
                request.deadline_slot,
                current_slot,
                assignment.error_window_future,
                forecast_max_slot,
                assignment.total_slots,
            )?;
            Ok(SolveRequest {
                request_id: request.id,
                arrival_slot: request.arrival_slot,
                deadline_slot: request.deadline_slot,
                assignment_deadline_slot,
            })
        })
        .collect::<Result<Vec<_>, SolverError>>()?;

    let mut baseline_slot_counts: HashMap<i32, i64> = HashMap::new();
    for existing in snapshot.get_future_assignments(current_slot) {
        let count = baseline_slot_counts
            .entry(existing.scheduled_slot)
            .or_insert(0);
        *count = count.checked_add(1).ok_or_else(|| {
            SolverError::Algorithm(format!(
                "slot {} baseline occupancy overflowed i64",
                existing.scheduled_slot
            ))
        })?;
    }

    let mut err_by_slot: HashMap<i32, ErrorTotals> = HashMap::new();
    for existing in snapshot
        .assignments
        .values()
        .filter(|existing| existing.qos_profile_id == profile_id)
    {
        let totals = err_by_slot.entry(existing.scheduled_slot).or_default();
        totals.sum += existing.error;
        totals.count += 1.0;
    }

    let profile_stats = snapshot.get_profile_error_stats(&profile_id);
    let window = snapshot.get_profile_window_error_stats(
        &profile_id,
        current_slot,
        assignment.error_window_past,
        assignment.error_window_future,
        &HashSet::new(),
    );
    let baseline = ErrorBaseline::new(window.error_sum, window.count as f64);
    let baseline =
        augment_with_decayed_past(current_slot, baseline, &profile_id, assignment, snapshot);

    let cumulative_error_constraint_active = assignment.global_error_constraint_enabled
        && assignment.global_error_constraint_hard
        && profile_stats.count > 0
        && profile_stats.avg > assignment.max_error_threshold;

    Ok(SolveInput {
        current_slot,
        requests,
        carbon_forecast: carbon_forecast.to_vec(),
        capacity_tiers: assignment.capacity_tiers.to_vec(),
        baseline_slot_counts,
        carbon_cost_duration_scale: assignment.carbon_cost_duration_scale,
        forecast_max_slot: Some(forecast_max_slot),
        qos: QoSProfileInput {
            profile_id,
            flavours: assignment.flavours.to_vec(),
            cumulative_error_constraint_active,
            cumulative_err: ErrorTotals {
                sum: profile_stats.error_sum,
                count: profile_stats.count as f64,
            },
            err_by_slot,
            max_average_error: assignment.max_error_threshold,
            error_window: ErrorWindowInput {
                baseline_err: ErrorTotals {
                    sum: baseline.error_sum,
                    count: baseline.request_count,
                },
                past_slots: assignment.error_window_past,
                future_slots: assignment.error_window_future,
            },
        },
    })
}
