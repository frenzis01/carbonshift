// Helper functions

use crate::engine::{
    solver::{ErrorTotals, QoSProfileInput},
    types::{CapacityTier, Flavour, get_capacity_multiplier},
};

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
    // slot_count: &HashMap<i32, i64>,
    // slot_errors: &HashMap<i32, Vec<f64>>,
    // global_error_sum: f64,
    // global_count: usize,
    scale: f64,
    // sorted_flavours: &[&'a Flavour],
    // max_err: f64,
    // win_past: i32,
    // win_future: i32,
    // global_constraint_enabled: bool,
    qos: &'a QoSProfileInput,
) -> Option<(&'a Flavour, f64)> {
    // `'a` links the returned flavour reference to `qos`: the caller must keep
    // the profile borrowed for as long as it uses the selected flavour.
    let ci = carbon_forecast.get(slot as usize).copied().unwrap_or(1.0);
    // TODO: unwrap can err_by_slot.get(&slot) ever return None?
    // TODO: count should always be integer number (e.g. 46.0) for future slots,
    // as there is no decayed history there, should ceil() stay there for safety?
    let position = qos
        .err_by_slot
        .get(&slot)
        .unwrap_or(&ErrorTotals {
            sum: 0.0,
            count: 0.0,
        })
        .count
        .ceil() as i64
        + 1;

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
        let mut win_cnt = 1usize;
        for ws in (slot - qos.error_window.past_slots)..=(slot + qos.error_window.future_slots) {
            if let Some(errs) = qos.err_by_slot.get(&ws) {
                // Add past errors to window sum
                win_sum += errs.sum;
                win_cnt += errs.count as usize;
            }
        }
        if qos.max_average_error < win_sum / win_cnt as f64 {
            continue;
        }
        let cost = ci * mult * flav.duration * scale;
        return Some((flav, cost));
    }
    None
}
