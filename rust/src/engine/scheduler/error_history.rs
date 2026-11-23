//! Profile-scoped decayed error history added to a solver's QoS baseline.

use crate::config::AssignmentPolicy;
use crate::engine::qos::QosProfileId;
use crate::engine::shared_state::linear_past_decay_weight;
use crate::shared_state::SolverSnapshot;

/// Weighted error history accumulated before the current batch is considered.
///
/// Counts may be fractional because decayed history is deliberately weighted.
#[derive(Debug, Clone, Default)]
pub(super) struct ErrorBaseline {
    pub(super) error_sum: f64,
    pub(super) request_count: f64,
}

impl ErrorBaseline {
    pub(super) fn new(error_sum: f64, request_count: f64) -> Self {
        Self {
            error_sum,
            request_count,
        }
    }
}

/// Add linearly decayed observations just outside the active past window.
pub(super) fn augment_with_decayed_past(
    current_slot: i32,
    baseline: ErrorBaseline,
    profile_id: &QosProfileId,
    assignment: &AssignmentPolicy<'_>,
    snapshot: &SolverSnapshot,
) -> ErrorBaseline {
    let decay_slots = assignment.error_window_past_decay_slots.max(0) as usize;
    if decay_slots == 0 {
        return baseline;
    }

    let mut weighted_count = 0.0;
    let mut weighted_error_sum = 0.0;

    for idx in 1..=decay_slots {
        let slot = current_slot - assignment.error_window_past - idx as i32;
        let stats = snapshot.get_profile_slot_error_stats(profile_id, slot);
        if stats.count == 0 {
            continue;
        }

        let count = stats.count as f64;
        let weight = linear_past_decay_weight(assignment.error_window_past_decay_slots, idx as i32);
        weighted_count += count * weight;
        weighted_error_sum += stats.error_sum * weight;
    }

    if weighted_count == 0.0 {
        return baseline;
    }

    ErrorBaseline::new(
        baseline.error_sum + weighted_error_sum,
        baseline.request_count + weighted_count,
    )
}
