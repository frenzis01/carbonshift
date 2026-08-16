/// Configuration for the CarbonShift batch scheduler.
///
/// Mirrors `config.py`.  All fields are plain values (no lazy evaluation).
/// The `Config::default()` implementation reproduces the Python module
/// defaults exactly so unit tests and production code share one source of
/// truth.

use crate::types::{CapacityTier, Flavour};

// ─── Config ──────────────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct Config {
    // ── batch processing ──────────────────────────────────────────────────
    /// Number of requests to accumulate before running a DP batch.
    pub batch_size: usize,

    // ── time slot ─────────────────────────────────────────────────────────
    /// Duration of each time slot in seconds.
    pub slot_duration_seconds: f64,
    /// Total number of time slots in the planning horizon.
    pub total_slots: i32,

    // ── flavours ──────────────────────────────────────────────────────────
    /// Available execution flavours (ordered from most accurate to fastest).
    pub flavours: Vec<Flavour>,
    /// Scale factor: carbon_intensity [gCO₂/kWh] × duration_seconds × scale → gCO₂.
    /// Derivation: 1/3600 converts seconds to hours at 1 kW load per request.
    pub carbon_cost_duration_scale: f64,

    // ── error budget ──────────────────────────────────────────────────────
    /// Maximum allowed average error (%) in the sliding window.
    pub max_error_threshold: f64,
    /// Number of past slots in the error window.
    pub error_window_past: i32,
    /// Number of future slots in the error window.
    pub error_window_future: i32,
    /// Additional past slots included with linearly decayed weight.
    pub error_window_past_decay_slots: i32,
    /// Requests cannot be placed beyond current_slot + this value.
    pub assignment_max_future_slots: i32,

    // global error constraint
    pub global_error_constraint_enabled: bool,
    pub global_error_constraint_hard: bool,

    // virtual prehistory
    pub prehistory_use_virtual_past: bool,
    pub prehistory_error_ratio_of_threshold: f64,
    pub forecast_error_ratio_of_threshold: f64,
    pub prehistory_stochastic_counts: bool,
    pub prehistory_random_seed: u64,
    pub prehistory_mock_influence: f64,

    // ── capacity tiers (rebound effect) ──────────────────────────────────
    pub capacity_tiers: Vec<CapacityTier>,

    // ── DP solver ─────────────────────────────────────────────────────────
    /// Pruning method: "beam", "kbest", or "none".
    pub dp_pruning_method: String,
    /// Apply pruning only for batches with size >= this threshold (0 = disabled).
    pub dp_pruning_min_batch_size: usize,
    /// Number of states to keep during pruning.
    pub dp_pruning_k: usize,
    /// Maximum seconds for DP solver per batch before timeout fallback.
    pub dp_timeout: f64,
    /// If true, future assignments are pinned as baseline load; if false, they
    /// are re-planned jointly with the current batch (time-shifting).
    pub dp_lock_future_assignments: bool,
    /// Governs both how the error-window baseline is diluted *and* what
    /// happens when the primary DP solve still can't cover all pending
    /// requests. The error constraint (`max_error_threshold`) is NEVER
    /// relaxed/removed — infeasibility always resolves via `greedy_fallback`
    /// (accurate flavour, cheapest feasible slot), never via a second DP
    /// pass with a loosened threshold.
    ///
    /// - "min_error_greedy": no synthetic/mock requests are injected; on
    ///   infeasibility, go straight to `greedy_fallback`.
    /// - "carryover": inject mock requests carried over from the slot that
    ///   just left the error window (with influence decay); on infeasibility
    ///   (even with mocks), go to `greedy_fallback`.
    /// - "forecast": inject mock requests sampled from the expected arrival
    ///   rate for the current slot (with influence decay); same fallback.
    pub infeasibility_recovery_mode: String,
    pub infeasibility_mock_influence: f64,
    /// None → policy-derived; Some(x) → fixed override.
    pub infeasibility_mock_error_per_request: Option<f64>,
    pub infeasibility_mock_influence_decay_step: f64,

    // ── request generation ────────────────────────────────────────────────
    pub predicted_requests_per_slot: f64,
    pub request_rate_std_factor: f64,
    pub deadline_min_slack: i32,
    pub deadline_max_slack: i32,

    // ── rollback (concurrent capacity-tier breach detection) ─────────────
    /// Maximum number of consecutive rollbacks allowed for a single batch
    /// before the assignment is forced regardless of tier breach.
    /// 0 = rollback disabled entirely.
    pub rollback_max_consecutive: usize,

    // ── threading & concurrency ───────────────────────────────────────────
    pub max_batch_solver_parallelism: usize,
    pub queue_timeout: f64,
    /// Flush a partial batch (< batch_size requests) if its oldest request has
    /// been waiting more than this many virtual seconds.  0.0 = disabled.
    pub batch_timeout_secs: f64,

    // ── simulation speed ──────────────────────────────────────────────────
    /// When true: once a slot has no pending requests and no active workers,
    /// the virtual clock jumps immediately to the next slot boundary instead
    /// of waiting in real time.  Useful for offline / batch-replay runs.
    pub skip_empty_slots: bool,
    /// Multiplier on `slot_duration_seconds` for wall-clock pacing.
    /// `1.0` = real-time; `0.1` = 10× faster; `0.0` = essentially instant
    /// (only meaningful when `skip_empty_slots` is false).
    pub slot_speed_scale: f64,
    /// Max requests the generator emits per tick while pacing a slot's
    /// arrivals in real time (only used when `skip_empty_slots` is false,
    /// i.e. true realtime simulation). Larger K = coarser pacing.
    pub generator_realtime_chunk_size: usize,

    // ── progress display ──────────────────────────────────────────────────
    /// Total requests expected for this run (known from scenario upfront).
    /// Used by the progress display to show `scheduled / total_requests` and
    /// a stable percentage even before all requests have been received.
    /// 0 means "unknown" — falls back to total_received.
    pub total_requests: usize,

    // ── solver strategy ───────────────────────────────────────────────────
    /// Which solver to use for batch assignment.
    /// `"dp"` (default): DP solver with error constraints and rollback.
    /// `"bandit"`: online ε-greedy bandit (state shared across batches).
    /// `"ant_colony"`: online ACO (pheromone shared across batches).
    /// `"greedy_singleton"`: online exhaustive greedy scan (no learning, no
    /// joint multi-request search) — only valid when `batch_size == 1`.
    pub solver_strategy: String,

    /// Concurrency strategy for online swarm strategies (bandit / ant_colony)
    /// when `max_batch_solver_parallelism > 1`. Irrelevant for `"dp"`.
    ///
    /// `"serialized"` (default, see `online_swarm.rs`): each batch worker
    /// solves while holding the scheduler mutex, so swarm state updates are
    /// fully sequential — correct and reproducible, at the cost of limiting
    /// swarm batches to one in flight at a time (DP batches are unaffected).
    ///
    /// `"merge"` (see `online_swarmerge.rs`): batch workers solve lock-free
    /// against a snapshot, then additively merge their contribution (running-
    /// mean deltas for bandit, discounted evaporation+deposit for ACO) back
    /// into the shared state. Preserves full parallelism without discarding
    /// concurrent updates (unlike the old last-writer-wins overwrite), at the
    /// cost of workers reading slightly stale state while they solve.
    pub online_swarm_mode: String,

    // ── swarm / bandit hyper-parameters ──────────────────────────────────
    /// Exploration probability for the online bandit.
    pub swarm_bandit_epsilon: f64,
    /// Optimistic initial Q-value (encourages early exploration).
    pub swarm_bandit_initial_q: f64,
    /// RNG seed for the online bandit.
    pub swarm_bandit_seed: u64,

    // ── swarm / ACO hyper-parameters ─────────────────────────────────────
    /// Number of ants per batch iteration.
    pub swarm_aco_n_ants: usize,
    /// Number of ACO iterations per batch (1 is fine for online use —
    /// pheromone accumulates across many batches).
    pub swarm_aco_n_iterations: usize,
    /// Pheromone influence exponent α.
    pub swarm_aco_alpha: f64,
    /// Heuristic influence exponent β.
    pub swarm_aco_beta: f64,
    /// Pheromone evaporation rate ρ ∈ (0, 1).
    pub swarm_aco_rho: f64,
    /// Pheromone deposit quantity (divided by solution cost).
    pub swarm_aco_q: f64,
    /// Initial pheromone level τ₀.
    pub swarm_aco_tau0: f64,
    /// RNG seed for the online ACO.
    pub swarm_aco_seed: u64,

    // ── logging & output ──────────────────────────────────────────────────
    pub verbose: bool,
    pub enable_solver_logging: bool,
    pub solver_runs_file: String,
    pub solver_assignments_file: String,
    pub solver_slot_metrics_file: String,
    pub enable_infeasibility_debug_logging: bool,
    pub solver_infeasible_debug_file: String,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            batch_size: 3,
            slot_duration_seconds: 10.0,
            total_slots: 24,
            flavours: vec![
                Flavour { name: "Accurate".to_string(), error: 0.0, duration: 60 },
                Flavour { name: "Balanced".to_string(), error: 2.5, duration: 30 },
                Flavour { name: "Fast".to_string(), error: 5.0, duration: 10 },
            ],
            carbon_cost_duration_scale: 1.0 / 3600.0,
            max_error_threshold: 4.0,
            error_window_past: 12,
            error_window_future: 14,
            error_window_past_decay_slots: 12,
            assignment_max_future_slots: 14,
            global_error_constraint_enabled: true,
            global_error_constraint_hard: true,
            prehistory_use_virtual_past: false,
            prehistory_error_ratio_of_threshold: 1.0,
            forecast_error_ratio_of_threshold: 1.0,
            prehistory_stochastic_counts: true,
            prehistory_random_seed: 4242,
            prehistory_mock_influence: 0.4,
            capacity_tiers: vec![
                CapacityTier { max_requests: Some(30),  multiplier: 1.0 },
                CapacityTier { max_requests: Some(50),  multiplier: 1.5 },
                CapacityTier { max_requests: Some(80),  multiplier: 2.0 },
                CapacityTier { max_requests: None,      multiplier: 5.0 }, // 81+: overload
            ],
            dp_pruning_method: "beam".to_string(),
            dp_pruning_min_batch_size: 8,
            dp_pruning_k: 1200,
            dp_timeout: 30.0,
            dp_lock_future_assignments: true,
            // infeasibility_recovery_mode: "forecast".to_string(),
            infeasibility_recovery_mode: "carryover".to_string(),
            infeasibility_mock_influence: 0.8,
            infeasibility_mock_error_per_request: None,
            infeasibility_mock_influence_decay_step: 0.15,
            predicted_requests_per_slot: 60.0,
            request_rate_std_factor: 0.5,
            deadline_min_slack: 0,
            deadline_max_slack: 14,
            max_batch_solver_parallelism: 20,
            queue_timeout: 1.0,
            batch_timeout_secs: 0.0,
            rollback_max_consecutive: 3,
            skip_empty_slots: true,
            slot_speed_scale: 1.0,
            generator_realtime_chunk_size: 10,
            total_requests: 0,
            solver_strategy: "dp".to_string(),
            online_swarm_mode: "serialized".to_string(),
            swarm_bandit_epsilon: 0.15,
            swarm_bandit_initial_q: 10.0,
            swarm_bandit_seed: 42,
            swarm_aco_n_ants: 10,
            swarm_aco_n_iterations: 3,
            swarm_aco_alpha: 1.0,
            swarm_aco_beta: 2.0,
            swarm_aco_rho: 0.3,
            swarm_aco_q: 1.0,
            swarm_aco_tau0: 1.0,
            swarm_aco_seed: 42,
            verbose: true,
            enable_solver_logging: true,
            solver_runs_file: "/tmp/online2_solver_runs.csv".to_string(),
            solver_assignments_file: "/tmp/online2_solver_assignments.csv".to_string(),
            solver_slot_metrics_file: "/tmp/online2_solver_slot_metrics.csv".to_string(),
            enable_infeasibility_debug_logging: true,
            solver_infeasible_debug_file: "/tmp/online2_solver_infeasible_debug.csv".to_string(),
        }
    }
}

impl Config {
    /// Convenience: total error window size (past + 1 + future).
    pub fn error_window_size(&self) -> i32 {
        self.error_window_past + 1 + self.error_window_future
    }

    /// Alias for predicted_requests_per_slot (backward-compat with Python
    /// `REQUESTS_PER_SLOT`).
    pub fn requests_per_slot(&self) -> f64 {
        self.predicted_requests_per_slot
    }

    /// Wall-clock seconds per slot, accounting for `slot_speed_scale`.
    ///
    /// Used for virtual-clock slot boundaries.  Clamped to ≥ 1 ms so that
    /// `slot_ms()` is never zero.
    pub fn effective_slot_duration_secs(&self) -> f64 {
        (self.slot_duration_seconds * self.slot_speed_scale).max(0.001)
    }

    /// Override fields that are present in a scenario's metadata.
    ///
    /// Only parameters that were recorded at generation time are updated; all
    /// other config fields (batch size, DP settings, logging paths, …) keep
    /// their default values so they can still be tuned independently.
    pub fn apply_scenario_metadata(&mut self, meta: &crate::scenario::ScenarioMetadata) {
        self.total_slots = meta.total_slots;
        self.slot_duration_seconds = meta.slot_duration_seconds;
        self.predicted_requests_per_slot = meta.requests_per_slot;
        self.request_rate_std_factor = meta.request_rate_std_factor;
        self.deadline_min_slack = meta.deadline_min_slack;
        self.deadline_max_slack = meta.deadline_max_slack;
        self.max_error_threshold = meta.max_error_threshold;
        self.error_window_past = meta.error_window_past;
        self.error_window_future = meta.error_window_future;
        self.error_window_past_decay_slots = meta.error_window_past_decay_slots;
        self.prehistory_use_virtual_past = meta.prehistory_enabled;
        self.prehistory_error_ratio_of_threshold = meta.prehistory_error_ratio;
        self.forecast_error_ratio_of_threshold = meta.prehistory_error_ratio;
        self.prehistory_mock_influence = meta.prehistory_mock_influence;
        self.prehistory_random_seed = meta.seed;
        if let Some(tiers) = meta.capacity_tiers.as_ref() {
            self.capacity_tiers = tiers.clone();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn flavour_order_most_accurate_first() {
        let cfg = Config::default();
        // Most accurate = longest duration (smallest error)
        assert_eq!(cfg.flavours[0].error, 0.0);
        assert_eq!(cfg.flavours[2].error, 5.0);
        assert!(cfg.flavours[0].duration > cfg.flavours[2].duration);
    }
}
