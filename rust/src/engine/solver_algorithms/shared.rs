// Helper functions

use crate::engine::{
    solver::QoSProfileInput,
    types::{CapacityTier, Flavour, get_capacity_multiplier},
};
use std::collections::HashMap;

/// Return the sorted list of valid target slots for a request:
/// `[arrival_slot, min(deadline_slot, arrival_slot + max_future, total_slots-1)]`.
pub(super) fn valid_slots(
    arrival_slot: i32,
    deadline_slot: i32,
    max_future: i32,
    total_slots: usize,
) -> Vec<i32> {
    let from = arrival_slot;
    let to = deadline_slot
        .min(arrival_slot + max_future)
        .min(total_slots as i32 - 1)
        .max(from);
    (from..=to).collect()
}

/// Try each flavour (sorted cheapest-first) for `slot`, returning the first
/// one that satisfies the global and window error constraints, or `None`.
pub(super) fn pick_feasible_flavour<'a>(
    slot: i32,
    carbon_forecast: &[f64],
    tiers: &[CapacityTier],
    slot_counts: &HashMap<i32, i64>,
    scale: f64,
    qos: &'a QoSProfileInput,
) -> Option<(&'a Flavour, f64)> {
    // `'a` links the returned flavour reference to `qos`: the caller must keep
    // the profile borrowed for as long as it uses the selected flavour.
    let ci = carbon_forecast.get(slot as usize).copied().unwrap_or(1.0);
    // Capacity is global across profiles; profile error counts must not be
    // used to calculate the request's capacity-tier position.
    let position = slot_counts.get(&slot).copied().unwrap_or(0) + 1;
    let mult = get_capacity_multiplier(tiers, position);
    // This local vector owns references into `qos.flavours`, not the flavours;
    // sorting it changes their order without moving the profile's values.
    let mut sorted_flavours: Vec<&'a Flavour> = qos.flavours.iter().collect();
    sorted_flavours.sort_by(|left, right| left.duration.total_cmp(&right.duration));

    // Keep `flav` as a reference so it can be returned with lifetime `'a`.
    for flav in sorted_flavours {
        if qos.cumulative_error_constraint_active {
            let new_avg =
                (qos.cumulative_err.sum + flav.error) / (qos.cumulative_err.count as f64 + 1.0);
            if new_avg > qos.max_average_error {
                continue;
            }
        }
        let mut win_sum = flav.error;
        let mut win_cnt = 1.0f64;
        for ws in (slot - qos.error_window.past_slots)..=(slot + qos.error_window.future_slots) {
            if let Some(errs) = qos.err_by_slot.get(&ws) {
                win_sum += errs.sum;
                win_cnt += errs.count;
            }
        }
        if win_cnt > 0.0 && qos.max_average_error < win_sum / win_cnt {
            continue;
        }
        let cost = ci * mult * flav.duration * scale;
        return Some((flav, cost));
    }
    None
}

/// Add one proposal to the local QoS snapshot so later requests in this solve
/// see its cumulative and per-slot error contribution. The runner's baseline
/// remains an input snapshot and is not mutated.
pub(super) fn record_local_profile_error(
    qos: &mut QoSProfileInput,
    slot: i32,
    error: f64,
) -> Result<(), crate::engine::solver::SolverError> {
    qos.cumulative_err.count += 1.0;
    qos.cumulative_err.sum += error;
    let slot_error = qos.err_by_slot.entry(slot).or_default();
    slot_error.count += 1.0;
    slot_error.sum += error;
    if !qos.cumulative_err.count.is_finite()
        || !qos.cumulative_err.sum.is_finite()
        || !slot_error.count.is_finite()
        || !slot_error.sum.is_finite()
    {
        return Err(crate::engine::solver::SolverError::Algorithm(
            "local QoS error totals overflowed".to_owned(),
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::qos::QosProfileId;
    use crate::engine::solver::{ErrorTotals, ErrorWindowInput};

    #[test]
    fn capacity_position_uses_global_occupancy_not_profile_error_count() {
        let flavours = vec![Flavour {
            name: "Fast".into(),
            error: 0.0,
            duration: 1.0,
        }];
        let mut err_by_slot = HashMap::new();
        err_by_slot.insert(
            0,
            ErrorTotals {
                sum: 0.0,
                count: 100.0,
            },
        );
        let qos = QoSProfileInput {
            profile_id: QosProfileId::default_profile(),
            flavours,
            cumulative_error_constraint_active: false,
            cumulative_err: ErrorTotals::default(),
            err_by_slot,
            max_average_error: 10.0,
            error_window: ErrorWindowInput {
                baseline_err: ErrorTotals::default(),
                past_slots: 0,
                future_slots: 0,
            },
        };
        let tiers = vec![
            CapacityTier {
                max_requests: Some(1),
                multiplier: 1.0,
            },
            CapacityTier {
                max_requests: None,
                multiplier: 10.0,
            },
        ];

        let choice = pick_feasible_flavour(0, &[1.0], &tiers, &HashMap::new(), 1.0, &qos).unwrap();

        assert_eq!(choice.1, 1.0);
    }

    #[test]
    fn local_profile_error_updates_cumulative_and_slot_totals_once() {
        let mut qos = QoSProfileInput {
            profile_id: QosProfileId::default_profile(),
            flavours: vec![],
            cumulative_error_constraint_active: false,
            cumulative_err: ErrorTotals {
                sum: 2.0,
                count: 1.0,
            },
            err_by_slot: HashMap::new(),
            max_average_error: 10.0,
            error_window: ErrorWindowInput {
                baseline_err: ErrorTotals {
                    sum: 2.0,
                    count: 1.0,
                },
                past_slots: 0,
                future_slots: 0,
            },
        };

        record_local_profile_error(&mut qos, 4, 3.0).unwrap();

        assert_eq!(qos.cumulative_err.sum, 5.0);
        assert_eq!(qos.cumulative_err.count, 2.0);
        assert_eq!(qos.err_by_slot[&4].sum, 3.0);
        assert_eq!(qos.err_by_slot[&4].count, 1.0);
        assert_eq!(qos.error_window.baseline_err.sum, 2.0);
        assert_eq!(qos.error_window.baseline_err.count, 1.0);
    }
}
