// Helper functions

use crate::engine::{
    solver::{
        QoSProfileInput, SolveInput, SolveRequest, SolverError, SolverScope,
        cap_assignment_deadline,
    },
    types::{CapacityTier, Flavour, get_capacity_multiplier},
};
use std::collections::HashMap;

/// Price the next request at a slot's current global occupancy position.
pub(super) fn get_slot_cost(
    slot: i32,
    flavour: &Flavour,
    carbon_forecast: &[f64],
    tiers: &[CapacityTier],
    slot_counts: &HashMap<i32, i64>,
    scale: f64,
) -> f64 {
    let ci = carbon_forecast.get(slot as usize).copied().unwrap_or(1.0);
    let position = *slot_counts.get(&slot).unwrap_or(&0) + 1;
    let multiplier = get_capacity_multiplier(tiers, position);
    ci * multiplier * flavour.duration * scale
}

/// Return the profile flavour with minimum error for best-effort fallback.
pub(super) fn get_fallback_flav(flavours: &[Flavour]) -> Result<&Flavour, SolverError> {
    flavours
        .iter()
        .min_by(|left, right| left.error.total_cmp(&right.error))
        .ok_or_else(|| SolverError::Algorithm("no fallback flavour is available".to_owned()))
}

/// Choose the cheapest flavour that keeps the supplied cumulative and window
/// error totals within the profile threshold.
pub(super) fn cheapest_feasible_flavour(
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

/// Validate the solver input shared by the stateless, random, and stateful
/// strategies before they inspect forecasts, deadlines, or QoS values.
pub(super) fn validate_solve_input(
    input: &SolveInput,
    scope: &SolverScope,
) -> Result<(), SolverError> {
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
    if input
        .forecast_max_slot
        .is_some_and(|max_slot| max_slot < input.current_slot)
    {
        return Err(SolverError::Algorithm(
            "forecast coverage ends before the current slot".to_owned(),
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

/// Cap a request's deadline to its arrival-based QoS look-ahead and to slots
/// for which this solve has forecast data. Expired work remains at the current
/// slot so algorithms can mark it late and use their explicit late-work policy.
fn cap_deadline_inside_forecast(
    input: &SolveInput,
    request: &SolveRequest,
    scope: &SolverScope,
) -> Result<i32, SolverError> {
    if input.current_slot > request.deadline_slot {
        return cap_assignment_deadline(
            request.request_id,
            request.arrival_slot,
            request.deadline_slot,
            input.current_slot,
            input.qos.error_window.future_slots,
            latest_forecast_slot(input, scope),
            scope.total_slots as i32,
        );
    }

    let derived_cap = cap_assignment_deadline(
        request.request_id,
        request.arrival_slot,
        request.deadline_slot,
        input.current_slot,
        input.qos.error_window.future_slots,
        latest_forecast_slot(input, scope),
        scope.total_slots as i32,
    )?;
    Ok(derived_cap.min(request.assignment_deadline_slot))
}

/// Return the request's earliest legal and latest forecast/QoS-capped slots.
pub(super) fn request_slot_bounds(
    input: &SolveInput,
    request: &SolveRequest,
    scope: &SolverScope,
) -> Result<(i32, i32), SolverError> {
    Ok((
        request.arrival_slot.max(input.current_slot),
        cap_deadline_inside_forecast(input, request, scope)?,
    ))
}

/// Latest slot available in both the planning horizon and this solve's forecast.
pub(super) fn latest_forecast_slot(input: &SolveInput, scope: &SolverScope) -> i32 {
    let total_slots = scope.total_slots as i32;
    (total_slots - 1).min(input.forecast_max_slot.unwrap_or(total_slots - 1))
}

/// Return the sorted candidate slots after request bounds have already been
/// capped by the deadline, QoS future window, and forecast availability.
pub(super) fn valid_slots(earliest_slot: i32, deadline_slot: i32, total_slots: usize) -> Vec<i32> {
    let from = earliest_slot;
    let to = deadline_slot.min(total_slots as i32 - 1).max(from);
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
