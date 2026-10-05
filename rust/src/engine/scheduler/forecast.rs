//! Forecast generation and the scheduler's explicit virtual-clock operation.

use std::time::{Duration, Instant};

use rand::SeedableRng;
use rand_chacha::ChaCha8Rng;
use rand_distr::{Distribution, Normal};

use crate::shared_state::SharedState;

/// Generate a seeded carbon-intensity forecast shared by service, simulation,
/// and test callers so they exercise one forecast model.
pub fn generate_carbon_intensity_forecast(
    total_slots: usize,
    carbon_intensity_cycle_slots: usize,
    seed: u64,
    night_max: f64,
    day_min: f64,
    sunrise_fraction: f64,
    sunset_fraction: f64,
    transition_slope: f64,
    noise_std: f64,
    noise_persistence: f64,
    inverted: bool,
    phase_shifted: bool,
) -> Vec<f64> {
    let mut rng = ChaCha8Rng::seed_from_u64(seed);

    let cycle = std::cmp::max(1, carbon_intensity_cycle_slots as i64) as usize;
    let mut forecast: Vec<f64> = Vec::new();
    let mut noise_state = 0.0;

    let normal_dist = Normal::new(0.0, noise_std).unwrap();
    // .expect("Invalid normal distribution");

    for slot in 0..total_slots {
        let mut x = ((slot % cycle) as f64) / (cycle as f64);

        if phase_shifted {
            x = (x + 0.25) % 1.0;
        }

        // Sigmoid function: 1 / (1 + exp(-k * (x - x0)))
        let sigmoid = |k: f64, x0: f64, x: f64| -> f64 { 1.0 / (1.0 + (-k * (x - x0)).exp()) };

        let rise = sigmoid(transition_slope, sunrise_fraction, x);
        let fall = sigmoid(transition_slope, sunset_fraction, x);

        let mut daylight_factor = rise - fall;

        if inverted {
            daylight_factor = -daylight_factor;
        }

        let trend = night_max - (night_max - day_min) * daylight_factor;

        // Aggiorna lo stato del rumore con autocorrelazione
        let random_noise: f64 = Distribution::sample(&normal_dist, &mut rng);
        noise_state = noise_persistence * noise_state + random_noise;

        let value = trend + noise_state;

        // max(1.0, value) e arrotonda a 6 decimali
        let rounded = (value.max(1.0) * 1_000_000.0).round() / 1_000_000.0;
        forecast.push(rounded);
    }

    forecast
}

/// Manually advance the virtual clock to the start of the next slot boundary.
///
/// Only meaningful when `Config::simulation.manual_clock` is true (otherwise nothing
/// else keeps the clock from also drifting with real wall-clock time).
/// `main_loop` derives `current_slot` from `virtual_elapsed_ms` every tick,
/// so bumping the latter is all that's needed — `main_loop`'s own slot-end
/// flush (`current_slot > last_flush_slot`) then drains any request still
/// stranded in the slot we just left, within its next 1-10ms tick. This
/// function blocks briefly for that drain so the caller (the REST service's
/// `/v1/admin/advance-slot`) can rely on "call returned" meaning "this
/// slot's requests all got a DP assignment", not just "the clock moved".
pub fn advance_to_next_slot(
    shared_state: &SharedState,
    total_slots: i32,
    effective_slot_duration_secs: f64,
) -> i32 {
    let current_slot = shared_state.get_current_slot();
    let slot_ms = (effective_slot_duration_secs * 1000.0) as u64;
    let next_ms = (current_slot as u64 + 1) * slot_ms;
    shared_state.set_virtual_elapsed_ms(next_ms);

    let new_slot =
        (((next_ms as f64 / 1000.0) / effective_slot_duration_secs) as i32).min(total_slots - 1);
    // Set directly rather than relying on `main_loop`'s next tick to derive
    // it from `virtual_elapsed_ms`: makes this function self-consistent for
    // rapid back-to-back calls (and unit-testable without a live scheduler).
    shared_state.set_current_slot(new_slot);

    let deadline = Instant::now() + Duration::from_secs(5);
    while shared_state.get_pending_count() > 0 && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(5));
    }
    // Small settle grace period for a worker that already claimed the last
    // pending batch but hasn't finished committing its assignment yet.
    std::thread::sleep(Duration::from_millis(100));

    new_slot
}

#[cfg(test)]
mod tests {
    use super::*;

    fn forecast(
        total_slots: usize,
        cycle_slots: usize,
        seed: u64,
        noise_std: f64,
        noise_persistence: f64,
    ) -> Vec<f64> {
        generate_carbon_intensity_forecast(
            total_slots,
            cycle_slots,
            seed,
            420.0,
            80.0,
            0.25,
            0.75,
            18.0,
            noise_std,
            noise_persistence,
            false,
            false,
        )
    }

    #[test]
    fn forecast_is_reproducible_for_a_seed_and_changes_with_seed() {
        let first = forecast(48, 24, 37, 2.0, 0.95);
        let repeated = forecast(48, 24, 37, 2.0, 0.95);
        let other_seed = forecast(48, 24, 38, 2.0, 0.95);

        assert_eq!(first, repeated);
        assert_ne!(first, other_seed);
        assert!(
            first.iter().all(|value| value.is_finite() && *value >= 1.0),
            "forecast intensities must be finite and respect the 1.0 lower bound"
        );
    }

    #[test]
    fn low_noise_forecast_repeats_cycle_and_has_daytime_valley() {
        let cycle_slots = 24;
        let forecast = forecast(48, cycle_slots, 9, 1e-12, 0.95);

        for slot in 0..cycle_slots {
            assert!(
                (forecast[slot] - forecast[slot + cycle_slots]).abs() <= 1e-6,
                "forecast cycle differs at slot {slot}"
            );
        }
        assert!(
            forecast[0] > forecast[12],
            "daytime intensity should be lower than the pre-sunrise value"
        );
    }

    #[test]
    fn zero_length_cycle_is_clamped_and_negative_intensities_are_floored() {
        let forecast = generate_carbon_intensity_forecast(
            8, 0, 11, -100.0, -400.0, 0.25, 0.75, 18.0, 0.5, 0.0, false, false,
        );

        assert_eq!(forecast.len(), 8);
        assert!(
            forecast.iter().all(|value| *value == 1.0),
            "negative generated intensities must be clamped to 1.0"
        );
    }
}
