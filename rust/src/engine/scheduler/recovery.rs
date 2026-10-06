//! Error-budget recovery and its persistent mock-pool state.

use std::collections::{HashMap, HashSet};
use std::sync::Mutex;

use rand::SeedableRng;
use rand_distr::{Distribution, Normal};

use crate::config::{AssignmentPolicy, InfeasibilityConfig, SimulationConfig};
use crate::dp_solver::MockPool;
use crate::engine::qos::QosProfileId;
use crate::shared_state::SolverSnapshot;

/// Persistent mock-pool for infeasibility recovery.
#[derive(Debug, Default)]
struct PersistentMockPool {
    slot: Option<i32>,
    mode: Option<String>,
    remaining: i32,
    error: f64,
}

#[derive(Debug, Clone)]
struct MockInfluenceState {
    base: f64,
    effective: f64,
    above_threshold_streak: i32,
    last_eval_slot: Option<i32>,
}

#[derive(Debug)]
struct RecoveryData {
    mock_pool: PersistentMockPool,
    mock_influence: MockInfluenceState,
}

impl RecoveryData {
    fn new(base_influence: f64) -> Self {
        Self {
            mock_pool: PersistentMockPool::default(),
            mock_influence: MockInfluenceState {
                base: base_influence,
                effective: base_influence,
                above_threshold_streak: 0,
                last_eval_slot: None,
            },
        }
    }
}

/// Stateful data owned by infeasibility recovery, separate from worker counters and swarm state.
pub(super) struct RecoveryState {
    profiles: Mutex<HashMap<QosProfileId, RecoveryData>>,
}

impl RecoveryState {
    pub(super) fn new() -> Self {
        Self {
            profiles: Mutex::new(HashMap::new()),
        }
    }

    fn profile_state<'a>(
        profiles: &'a mut HashMap<QosProfileId, RecoveryData>,
        profile_id: &QosProfileId,
        base_influence: f64,
    ) -> &'a mut RecoveryData {
        profiles
            .entry(profile_id.clone())
            .or_insert_with(|| RecoveryData::new(base_influence))
    }
}

// ─── Simple error baseline (internal) ────────────────────────────────────────

#[derive(Debug, Clone, Default)]
pub(super) struct ErrorBaseline {
    pub(super) error_sum: f64,
    pub(super) request_count: f64,
    pub(super) average_error: f64,
}

impl ErrorBaseline {
    pub(super) fn new(error_sum: f64, request_count: f64) -> Self {
        let avg = if request_count > 0.0 {
            error_sum / request_count
        } else {
            0.0
        };
        Self {
            error_sum,
            request_count,
            average_error: avg,
        }
    }
}

// ─── error baseline augmentation helpers ─────────────────────────────────────

/// Augment the error baseline with linearly-decayed contributions from the
/// slots just outside the past window boundary.  No-op when
/// `error_window_past_decay_slots == 0` (the default).
pub(super) fn augment_with_decayed_past(
    current_slot: i32,
    baseline: ErrorBaseline,
    profile_id: &QosProfileId,
    assignment: &AssignmentPolicy<'_>,
    snapshot: &SolverSnapshot,
    exclude: &HashSet<u64>,
) -> ErrorBaseline {
    let decay_slots = assignment.error_window_past_decay_slots.max(0) as usize;
    if decay_slots == 0 {
        return baseline;
    }

    let mut weighted_count = 0.0f64;
    let mut weighted_error_sum = 0.0f64;
    let denominator = (decay_slots + 1) as f64;

    for idx in 1..=decay_slots {
        let slot = current_slot - assignment.error_window_past - idx as i32;
        let slot_assignments: Vec<_> = snapshot
            .get_profile_requests_in_slot(profile_id, slot)
            .into_iter()
            .filter(|a| !exclude.contains(&a.request_id))
            .collect();
        let n = slot_assignments.len();
        if n == 0 {
            continue;
        }
        let slot_avg_err: f64 = slot_assignments.iter().map(|a| a.error).sum::<f64>() / n as f64;
        let weight = (decay_slots - idx + 1) as f64 / denominator;
        weighted_count += n as f64 * weight;
        weighted_error_sum += slot_avg_err * n as f64 * weight;
    }

    if weighted_count <= 0.0 {
        return baseline;
    }
    ErrorBaseline::new(
        baseline.error_sum + weighted_error_sum,
        baseline.request_count + weighted_count,
    )
}

/// Synthesise missing pre-history slots for startup iterations
/// (`current_slot < error_window_past`).  Disabled by default.
pub(super) fn augment_with_virtual_prehistory(
    current_slot: i32,
    baseline: ErrorBaseline,
    assignment: &AssignmentPolicy<'_>,
    simulation: &SimulationConfig,
    recovery: &InfeasibilityConfig,
) -> ErrorBaseline {
    if !recovery.prehistory_use_virtual_past {
        return baseline;
    }
    let missing = (assignment.error_window_past - current_slot).max(0);
    if missing == 0 {
        return baseline;
    }

    let rate = simulation.predicted_requests_per_slot;
    let sigma = (rate * simulation.request_rate_std_factor).max(1.0);
    let virtual_avg_err =
        assignment.max_error_threshold * recovery.prehistory_error_ratio_of_threshold;

    let mut virtual_requests = 0i32;
    for offset in 0..missing {
        let seed = recovery
            .prehistory_random_seed
            .wrapping_add((current_slot as u64).wrapping_sub(missing as u64 + offset as u64));
        let count = if recovery.prehistory_stochastic_counts {
            let dist = Normal::new(rate, sigma).unwrap();
            let mut rng = rand::rngs::StdRng::seed_from_u64(seed);
            (dist.sample(&mut rng) as i32).max(1)
        } else {
            rate.round().max(1.0) as i32
        };
        virtual_requests += count;
    }

    if virtual_requests <= 0 {
        return baseline;
    }
    ErrorBaseline::new(
        baseline.error_sum + virtual_requests as f64 * virtual_avg_err,
        baseline.request_count + virtual_requests as f64,
    )
}

/// Apply the configured infeasibility-recovery policy.
///
/// Returns (augmented_baseline, mock_pool_for_dp).
pub(super) fn apply_infeasibility_recovery(
    current_slot: i32,
    baseline: ErrorBaseline,
    profile_id: &QosProfileId,
    assignment: &AssignmentPolicy<'_>,
    simulation: &SimulationConfig,
    recovery: &InfeasibilityConfig,
    snapshot: &SolverSnapshot,
    recovery_state: &RecoveryState,
) -> (ErrorBaseline, MockPool) {
    let mode = recovery.recovery_mode.trim().to_lowercase();

    // Update mock influence once per slot (needs the current baseline avg).
    update_mock_influence(
        current_slot,
        baseline.average_error,
        assignment.max_error_threshold,
        profile_id,
        recovery,
        recovery_state,
    );

    if mode == "min_error_greedy" {
        // No mock injection; reset any persistent pool.
        reset_mock_pool(profile_id, recovery_state);
        return (baseline, MockPool::default());
    }

    // Retrieve (or seed) the persistent mock pool for this slot/mode.
    let (mock_count, mock_error, _source) = get_or_seed_mock_pool(
        current_slot,
        profile_id,
        &mode,
        assignment,
        simulation,
        recovery,
        snapshot,
        recovery_state,
    );

    if mock_count <= 0 || mock_error <= 0.0 {
        return (baseline, MockPool::default());
    }

    let augmented = ErrorBaseline::new(
        baseline.error_sum + mock_count as f64 * mock_error,
        baseline.request_count + mock_count as f64,
    );
    let pool = MockPool {
        initial_count: mock_count,
        error_per_request: mock_error,
    };
    (augmented, pool)
}

// ─── mock pool helpers ────────────────────────────────────────────────────────

fn update_mock_influence(
    slot: i32,
    baseline_avg: f64,
    max_error_threshold: f64,
    profile_id: &QosProfileId,
    recovery: &InfeasibilityConfig,
    recovery_state: &RecoveryState,
) {
    let mut profiles = recovery_state.profiles.lock().unwrap();
    let state = RecoveryState::profile_state(
        &mut profiles,
        profile_id,
        recovery.mock_influence.clamp(0.0, 1.0),
    );
    if state.mock_influence.last_eval_slot == Some(slot) {
        return;
    }
    let base = recovery.mock_influence.clamp(0.0, 1.0);
    let decay = recovery.mock_influence_decay_step.max(0.0);
    state.mock_influence.base = base;
    if baseline_avg > max_error_threshold {
        state.mock_influence.above_threshold_streak += 1;
        state.mock_influence.effective =
            (base - state.mock_influence.above_threshold_streak as f64 * decay).max(0.0);
    } else {
        state.mock_influence.above_threshold_streak = 0;
        state.mock_influence.effective = base;
    }
    state.mock_influence.last_eval_slot = Some(slot);
}

/// Retrieve the persistent mock pool for `(slot, mode)`, seeding it if needed.
///
/// The seed computation (which may call `shared_state`) is done outside the
/// lock to avoid holding it during I/O.
fn get_or_seed_mock_pool(
    slot: i32,
    profile_id: &QosProfileId,
    mode: &str,
    assignment: &AssignmentPolicy<'_>,
    simulation: &SimulationConfig,
    recovery: &InfeasibilityConfig,
    snapshot: &SolverSnapshot,
    recovery_state: &RecoveryState,
) -> (i32, f64, &'static str) {
    // First lock: check if we already have this slot/mode cached.
    let (has_pool, remaining, error) = {
        let mut profiles = recovery_state.profiles.lock().unwrap();
        let state = RecoveryState::profile_state(
            &mut profiles,
            profile_id,
            recovery.mock_influence.clamp(0.0, 1.0),
        );
        let same =
            state.mock_pool.slot == Some(slot) && state.mock_pool.mode.as_deref() == Some(mode);
        (same, state.mock_pool.remaining, state.mock_pool.error)
    };
    if has_pool {
        return (remaining, error, "persistent_remaining");
    }

    // Compute outside the lock (reads snapshot for carryover mode).
    let influence = {
        let mut profiles = recovery_state.profiles.lock().unwrap();
        RecoveryState::profile_state(
            &mut profiles,
            profile_id,
            recovery.mock_influence.clamp(0.0, 1.0),
        )
        .mock_influence
        .effective
    };
    let (new_count, new_error) = compute_mock_seed(
        slot, profile_id, mode, assignment, simulation, recovery, influence, snapshot,
    );

    // Second lock: store the new values.
    let mut profiles = recovery_state.profiles.lock().unwrap();
    let state = RecoveryState::profile_state(
        &mut profiles,
        profile_id,
        recovery.mock_influence.clamp(0.0, 1.0),
    );
    state.mock_pool.slot = Some(slot);
    state.mock_pool.mode = Some(mode.to_string());
    state.mock_pool.remaining = new_count.max(0);
    state.mock_pool.error = new_error.max(0.0);
    (
        state.mock_pool.remaining,
        state.mock_pool.error,
        "new_window_seed",
    )
}

fn compute_mock_seed(
    slot: i32,
    profile_id: &QosProfileId,
    mode: &str,
    assignment: &AssignmentPolicy<'_>,
    simulation: &SimulationConfig,
    recovery: &InfeasibilityConfig,
    influence: f64,
    snapshot: &SolverSnapshot,
) -> (i32, f64) {
    let (mut count, error) = match mode {
        "carryover" => {
            let window_start = (slot - assignment.error_window_past).max(0);
            let dropped_slot = window_start - 1;
            if dropped_slot < 0 {
                return (0, 0.0);
            }
            let dropped = snapshot.get_profile_requests_in_slot(profile_id, dropped_slot);
            let n = dropped.len() as i32;
            if n == 0 {
                return (0, 0.0);
            }
            let avg_err = dropped.iter().map(|a| a.error).sum::<f64>() / n as f64;
            let mock_err = resolve_mock_error(avg_err, recovery);
            (n, mock_err)
        }
        "forecast" => {
            let rate = simulation.predicted_requests_per_slot;
            let sigma = (rate * simulation.request_rate_std_factor).max(1.0);
            let seed = recovery.prehistory_random_seed.wrapping_add(slot as u64);
            let dist = Normal::new(rate, sigma).unwrap();
            let mut rng = rand::rngs::StdRng::seed_from_u64(seed);
            let n = (dist.sample(&mut rng) as i32).max(0);
            let default_err =
                assignment.max_error_threshold * recovery.forecast_error_ratio_of_threshold;
            (n, resolve_mock_error(default_err, recovery))
        }
        _ => return (0, 0.0),
    };

    if count > 0 {
        count = (count as f64 * influence).round() as i32;
    }
    (count.max(0), error.max(0.0))
}

fn resolve_mock_error(fallback: f64, recovery: &InfeasibilityConfig) -> f64 {
    match recovery.mock_error_per_request {
        Some(v) => v.max(0.0),
        None => fallback.max(0.0),
    }
}

pub(super) fn consume_mock_pool(
    slot: i32,
    mode: &str,
    consumed: i32,
    profile_id: &QosProfileId,
    recovery_mode: &str,
    recovery_state: &RecoveryState,
) {
    if recovery_mode.trim().to_lowercase() == "min_error_greedy" {
        return;
    }
    let mut profiles = recovery_state.profiles.lock().unwrap();
    if let Some(state) = profiles.get_mut(profile_id) {
        if state.mock_pool.slot == Some(slot) && state.mock_pool.mode.as_deref() == Some(mode) {
            state.mock_pool.remaining = (state.mock_pool.remaining - consumed.max(0)).max(0);
        }
    }
}

fn reset_mock_pool(profile_id: &QosProfileId, recovery_state: &RecoveryState) {
    if let Some(state) = recovery_state.profiles.lock().unwrap().get_mut(profile_id) {
        state.mock_pool.slot = None;
        state.mock_pool.mode = None;
        state.mock_pool.remaining = 0;
        state.mock_pool.error = 0.0;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;

    #[test]
    fn mock_pool_is_persistent_but_isolated_by_profile() {
        let mut config = Config::default();
        config.simulation.predicted_requests_per_slot = 1000.0;
        config.simulation.request_rate_std_factor = 0.0;
        config.infeasibility.recovery_mode = "forecast".to_string();
        config.infeasibility.mock_influence = 1.0;
        config.infeasibility.mock_error_per_request = Some(1.0);

        let assignment = config.assignment_policy();
        let snapshot = crate::shared_state::SharedState::new().snapshot_for_solver();
        let recovery_state = RecoveryState::new();
        let profile_a = QosProfileId::parse("qa-standard-v1").unwrap();
        let profile_b = QosProfileId::parse("ner-standard-v1").unwrap();

        let (_, seeded_pool) = apply_infeasibility_recovery(
            0,
            ErrorBaseline::new(0.0, 1.0),
            &profile_a,
            &assignment,
            &config.simulation,
            &config.infeasibility,
            &snapshot,
            &recovery_state,
        );
        assert!(seeded_pool.initial_count > 1);

        consume_mock_pool(
            0,
            "forecast",
            1,
            &profile_a,
            &config.infeasibility.recovery_mode,
            &recovery_state,
        );
        let (_, remaining_pool) = apply_infeasibility_recovery(
            0,
            ErrorBaseline::new(0.0, 1.0),
            &profile_a,
            &assignment,
            &config.simulation,
            &config.infeasibility,
            &snapshot,
            &recovery_state,
        );
        let (_, independent_pool) = apply_infeasibility_recovery(
            0,
            ErrorBaseline::new(0.0, 1.0),
            &profile_b,
            &assignment,
            &config.simulation,
            &config.infeasibility,
            &snapshot,
            &recovery_state,
        );

        assert_eq!(remaining_pool.initial_count, seeded_pool.initial_count - 1);
        assert_eq!(independent_pool.initial_count, seeded_pool.initial_count);
    }
}
