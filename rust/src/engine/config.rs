/// Configuration for the CarbonShift batch scheduler.
///
/// Mirrors `config.py`.  All fields are plain values (no lazy evaluation).
/// The `Config::default()` implementation reproduces the Python module
/// defaults exactly so unit tests and production code share one source of
/// truth.
use crate::engine::qos::QosProfile;
use crate::types::{CapacityTier, Flavour};

// ─── Concern-specific configuration ─────────────────────────────────────────

/// Settings for batching, solver selection, and solver execution.
#[derive(Debug, Clone)]
pub struct SolverConfig {
    /// Number of requests to accumulate before running a solver batch.
    pub batch_size: usize,
    /// Which solver to use: "dp", "bandit", "ant_colony", or "greedy_singleton".
    pub solver_strategy: String,
    /// DP pruning method: "beam", "kbest", or "none".
    pub dp_pruning_method: String,
    /// Apply pruning only for batches at least this large (0 = disabled).
    pub dp_pruning_min_batch_size: usize,
    /// Number of states to keep during pruning.
    pub dp_pruning_k: usize,
    /// Maximum seconds for the DP solver per batch before timeout fallback.
    pub dp_timeout: f64,
    /// Whether future assignments are pinned as baseline load.
    pub dp_lock_future_assignments: bool,
    /// Maximum consecutive capacity-tier rollbacks for one batch (0 = disabled).
    pub rollback_max_consecutive: usize,
    /// Maximum number of batch solver workers.
    pub max_batch_solver_parallelism: usize,
    /// Maximum wait for work on the solver queue, in seconds.
    pub queue_timeout: f64,
    /// Flush a partial batch after this many virtual seconds (0 = disabled).
    pub batch_timeout_secs: f64,
}

/// Settings used to generate and pace synthetic or replayed workloads.
#[derive(Debug, Clone)]
pub struct SimulationConfig {
    /// Expected request arrivals per slot.
    pub predicted_requests_per_slot: f64,
    /// Standard-deviation factor for generated request rates.
    pub request_rate_std_factor: f64,
    /// Minimum request deadline slack in slots.
    pub deadline_min_slack: i32,
    /// Maximum request deadline slack in slots.
    pub deadline_max_slack: i32,
    /// Skip empty slots rather than waiting for their wall-clock boundary.
    pub skip_empty_slots: bool,
    /// Wall-clock pacing multiplier for each slot (1.0 = real time).
    pub slot_speed_scale: f64,
    /// Freeze the virtual clock until an explicit slot-advance operation.
    pub manual_clock: bool,
    /// Maximum generated requests per real-time tick.
    pub generator_realtime_chunk_size: usize,
    /// Expected request count for progress reporting (0 = unknown).
    pub total_requests: usize,
}

/// Settings for synthetic prehistory and recovery from infeasible batches.
#[derive(Debug, Clone)]
pub struct InfeasibilityConfig {
    /// Whether to use virtual past slots when building the error baseline.
    pub prehistory_use_virtual_past: bool,
    /// Prehistory error as a ratio of the configured error threshold.
    pub prehistory_error_ratio_of_threshold: f64,
    /// Forecast error as a ratio of the configured error threshold.
    pub forecast_error_ratio_of_threshold: f64,
    /// Generate stochastic request counts for virtual prehistory.
    pub prehistory_stochastic_counts: bool,
    /// Random seed used for virtual prehistory generation.
    pub prehistory_random_seed: u64,
    /// Influence of prehistory mock requests on the error baseline.
    pub prehistory_mock_influence: f64,
    /// Recovery policy: "min_error_greedy", "carryover", or "forecast".
    pub recovery_mode: String,
    /// Influence of infeasibility mock requests.
    pub mock_influence: f64,
    /// Optional fixed error for each infeasibility mock request.
    pub mock_error_per_request: Option<f64>,
    /// Influence decay applied to infeasibility mocks at each step.
    pub mock_influence_decay_step: f64,
}

/// Hyperparameters and concurrency mode for online swarm solvers.
#[derive(Debug, Clone)]
pub struct SwarmConfig {
    /// Concurrency mode: "serialized" or "merge".
    pub mode: String,
    /// Exploration probability for the online bandit.
    pub bandit_epsilon: f64,
    /// Optimistic initial Q-value for the online bandit.
    pub bandit_initial_q: f64,
    /// RNG seed for the online bandit.
    pub bandit_seed: u64,
    /// Number of ants per ACO iteration.
    pub aco_n_ants: usize,
    /// Number of ACO iterations per batch.
    pub aco_n_iterations: usize,
    /// Pheromone influence exponent α.
    pub aco_alpha: f64,
    /// Heuristic influence exponent β.
    pub aco_beta: f64,
    /// Pheromone evaporation rate ρ.
    pub aco_rho: f64,
    /// Pheromone deposit quantity.
    pub aco_q: f64,
    /// Initial pheromone level τ₀.
    pub aco_tau0: f64,
    /// RNG seed for online ACO.
    pub aco_seed: u64,
}

/// Settings for diagnostic output and solver metrics files.
#[derive(Debug, Clone)]
pub struct LoggingConfig {
    /// Enable verbose scheduler diagnostics.
    pub verbose: bool,
    /// Print the scheduler progress line to stdout.
    pub enable_progress_display: bool,
    /// Write solver metrics to CSV.
    pub enable_solver_logging: bool,
    /// Destination for solver run metrics.
    pub solver_runs_file: String,
    /// Destination for per-assignment metrics.
    pub solver_assignments_file: String,
    /// Destination for per-slot metrics.
    pub solver_slot_metrics_file: String,
    /// Write diagnostics for infeasible solver batches.
    pub enable_infeasibility_debug_logging: bool,
    /// Destination for infeasibility diagnostics.
    pub solver_infeasible_debug_file: String,
}

// ─── Config ──────────────────────────────────────────────────────────────────

/// Core scheduling policy and the concern-specific settings used by its runtime.
#[derive(Debug, Clone)]
pub struct Config {
    /// Settings for batch formation and assignment solvers.
    pub solver: SolverConfig,
    /// Settings for synthetic and replayed workload execution.
    pub simulation: SimulationConfig,
    /// Settings for error-budget prehistory and infeasibility recovery.
    pub infeasibility: InfeasibilityConfig,
    /// Settings for online bandit and ant-colony solvers.
    pub swarm: SwarmConfig,
    /// Settings for progress and diagnostic output.
    pub logging: LoggingConfig,
    /// Duration of each time slot in seconds.
    pub slot_duration_seconds: f64,
    /// Total number of time slots in the planning horizon.
    pub total_slots: i32,
    /// Offset from the engine's uptime-based slot to the provider's global slot.
    pub slot_epoch_offset: i64,
    /// Available execution flavours, ordered from most accurate to fastest.
    pub flavours: Vec<Flavour>,
    /// Scale factor converting carbon intensity and duration to gCO₂.
    pub carbon_cost_duration_scale: f64,
    /// Maximum allowed average error (%) in the sliding window.
    pub max_error_threshold: f64,
    /// Number of past slots in the error window.
    pub error_window_past: i32,
    /// Number of future slots in the error window.
    pub error_window_future: i32,
    /// Additional past slots included with linearly decayed weight.
    pub error_window_past_decay_slots: i32,
    /// Maximum number of slots into the future for an assignment.
    pub assignment_max_future_slots: i32,
    /// Enforce the global error constraint.
    pub global_error_constraint_enabled: bool,
    /// Reject assignments that violate the global error constraint.
    pub global_error_constraint_hard: bool,
    /// Capacity/rebound tiers applied to slot carbon costs.
    pub capacity_tiers: Vec<CapacityTier>,
}

/// Borrowed view of only the core settings used to evaluate assignments.
#[derive(Debug, Clone, Copy)]
pub struct AssignmentPolicy<'a> {
    /// Default flavours used when a request has no task-specific override.
    pub flavours: &'a [Flavour],
    /// Capacity multipliers used to price assignments.
    pub capacity_tiers: &'a [CapacityTier],
    /// Planning horizon in slots.
    pub total_slots: i32,
    /// Scale converting flavour duration to carbon cost.
    pub carbon_cost_duration_scale: f64,
    /// Maximum allowed average error (%).
    pub max_error_threshold: f64,
    /// Number of past slots in the error window.
    pub error_window_past: i32,
    /// Number of future slots in the error window.
    pub error_window_future: i32,
    /// Additional past slots included with linearly decayed weight.
    pub error_window_past_decay_slots: i32,
    /// Maximum scheduling shift in slots.
    pub assignment_max_future_slots: i32,
    /// Whether to enforce this policy's cumulative error constraint.
    pub global_error_constraint_enabled: bool,
    /// Whether cumulative-constraint violations exclude flavours.
    pub global_error_constraint_hard: bool,
}

impl Default for SolverConfig {
    fn default() -> Self {
        Self {
            batch_size: 3,
            solver_strategy: "dp".to_string(),
            dp_pruning_method: "beam".to_string(),
            dp_pruning_min_batch_size: 8,
            dp_pruning_k: 1200,
            dp_timeout: 30.0,
            dp_lock_future_assignments: true,
            rollback_max_consecutive: 3,
            max_batch_solver_parallelism: 20,
            queue_timeout: 1.0,
            batch_timeout_secs: 0.0,
        }
    }
}

impl Default for SimulationConfig {
    fn default() -> Self {
        Self {
            predicted_requests_per_slot: 60.0,
            request_rate_std_factor: 0.5,
            deadline_min_slack: 0,
            deadline_max_slack: 14,
            skip_empty_slots: true,
            slot_speed_scale: 1.0,
            manual_clock: false,
            generator_realtime_chunk_size: 10,
            total_requests: 0,
        }
    }
}

impl Default for InfeasibilityConfig {
    fn default() -> Self {
        Self {
            prehistory_use_virtual_past: false,
            prehistory_error_ratio_of_threshold: 1.0,
            forecast_error_ratio_of_threshold: 1.0,
            prehistory_stochastic_counts: true,
            prehistory_random_seed: 4242,
            prehistory_mock_influence: 0.4,
            recovery_mode: "carryover".to_string(),
            mock_influence: 0.8,
            mock_error_per_request: None,
            mock_influence_decay_step: 0.15,
        }
    }
}

impl Default for SwarmConfig {
    fn default() -> Self {
        Self {
            mode: "serialized".to_string(),
            bandit_epsilon: 0.15,
            bandit_initial_q: 10.0,
            bandit_seed: 42,
            aco_n_ants: 10,
            aco_n_iterations: 3,
            aco_alpha: 1.0,
            aco_beta: 2.0,
            aco_rho: 0.3,
            aco_q: 1.0,
            aco_tau0: 1.0,
            aco_seed: 42,
        }
    }
}

impl Default for LoggingConfig {
    fn default() -> Self {
        Self {
            verbose: true,
            enable_progress_display: true,
            enable_solver_logging: true,
            solver_runs_file: "/tmp/online2_solver_runs.csv".to_string(),
            solver_assignments_file: "/tmp/online2_solver_assignments.csv".to_string(),
            solver_slot_metrics_file: "/tmp/online2_solver_slot_metrics.csv".to_string(),
            enable_infeasibility_debug_logging: true,
            solver_infeasible_debug_file: "/tmp/online2_solver_infeasible_debug.csv".to_string(),
        }
    }
}

impl Default for Config {
    fn default() -> Self {
        Self {
            solver: SolverConfig::default(),
            simulation: SimulationConfig::default(),
            infeasibility: InfeasibilityConfig::default(),
            swarm: SwarmConfig::default(),
            logging: LoggingConfig::default(),
            slot_duration_seconds: 10.0,
            total_slots: 24,
            slot_epoch_offset: 0,
            flavours: vec![
                Flavour {
                    name: "Accurate".to_string(),
                    error: 0.0,
                    duration: 60,
                },
                Flavour {
                    name: "Balanced".to_string(),
                    error: 2.5,
                    duration: 30,
                },
                Flavour {
                    name: "Fast".to_string(),
                    error: 5.0,
                    duration: 10,
                },
            ],
            carbon_cost_duration_scale: 1.0 / 3600.0,
            max_error_threshold: 4.0,
            error_window_past: 12,
            error_window_future: 14,
            error_window_past_decay_slots: 12,
            assignment_max_future_slots: 14,
            global_error_constraint_enabled: true,
            global_error_constraint_hard: true,
            capacity_tiers: vec![
                CapacityTier {
                    max_requests: Some(30),
                    multiplier: 1.0,
                },
                CapacityTier {
                    max_requests: Some(50),
                    multiplier: 1.5,
                },
                CapacityTier {
                    max_requests: Some(80),
                    multiplier: 2.0,
                },
                CapacityTier {
                    max_requests: None,
                    multiplier: 5.0,
                }, // 81+: overload
            ],
        }
    }
}

impl Config {
    /// Builds assignment policy from a profile while retaining global
    /// horizon, cost scale, capacity tiers, and assignment-future limits.
    pub fn assignment_policy_for_profile<'a>(
        &'a self,
        profile: &'a QosProfile,
    ) -> AssignmentPolicy<'a> {
        AssignmentPolicy {
            flavours: &profile.flavours,
            capacity_tiers: &self.capacity_tiers,
            total_slots: self.total_slots,
            carbon_cost_duration_scale: self.carbon_cost_duration_scale,
            max_error_threshold: profile.max_error_threshold,
            error_window_past: profile.error_window.past_slots,
            error_window_future: profile.error_window.future_slots,
            error_window_past_decay_slots: profile.error_window.past_decay_slots,
            assignment_max_future_slots: self.assignment_max_future_slots,
            global_error_constraint_enabled: profile.cumulative_error.enabled,
            global_error_constraint_hard: profile.cumulative_error.hard,
        }
    }

    /// Creates a borrowed assignment-policy view without exposing unrelated
    /// simulation, logging, recovery, or online-strategy settings.
    pub fn assignment_policy(&self) -> AssignmentPolicy<'_> {
        AssignmentPolicy {
            flavours: &self.flavours,
            capacity_tiers: &self.capacity_tiers,
            total_slots: self.total_slots,
            carbon_cost_duration_scale: self.carbon_cost_duration_scale,
            max_error_threshold: self.max_error_threshold,
            error_window_past: self.error_window_past,
            error_window_future: self.error_window_future,
            error_window_past_decay_slots: self.error_window_past_decay_slots,
            assignment_max_future_slots: self.assignment_max_future_slots,
            global_error_constraint_enabled: self.global_error_constraint_enabled,
            global_error_constraint_hard: self.global_error_constraint_hard,
        }
    }

    /// Convenience: total error window size (past + 1 + future).
    pub fn error_window_size(&self) -> i32 {
        self.error_window_past + 1 + self.error_window_future
    }

    /// Alias for predicted_requests_per_slot (backward-compat with Python
    /// `REQUESTS_PER_SLOT`).
    pub fn requests_per_slot(&self) -> f64 {
        self.simulation.predicted_requests_per_slot
    }

    /// Wall-clock seconds per slot, accounting for `slot_speed_scale`.
    ///
    /// Used for virtual-clock slot boundaries.  Clamped to ≥ 1 ms so that
    /// `slot_ms()` is never zero.
    pub fn effective_slot_duration_secs(&self) -> f64 {
        (self.slot_duration_seconds * self.simulation.slot_speed_scale).max(0.001)
    }

    /// Override fields that are present in a scenario's metadata.
    ///
    /// Only parameters that were recorded at generation time are updated; all
    /// other config fields (batch size, DP settings, logging paths, …) keep
    /// their default values so they can still be tuned independently.
    pub fn apply_scenario_metadata(&mut self, meta: &crate::scenario::ScenarioMetadata) {
        self.total_slots = meta.total_slots;
        self.slot_duration_seconds = meta.slot_duration_seconds;
        self.simulation.predicted_requests_per_slot = meta.requests_per_slot;
        self.simulation.request_rate_std_factor = meta.request_rate_std_factor;
        self.simulation.deadline_min_slack = meta.deadline_min_slack;
        self.simulation.deadline_max_slack = meta.deadline_max_slack;
        self.max_error_threshold = meta.max_error_threshold;
        self.error_window_past = meta.error_window_past;
        self.error_window_future = meta.error_window_future;
        self.error_window_past_decay_slots = meta.error_window_past_decay_slots;
        self.infeasibility.prehistory_use_virtual_past = meta.prehistory_enabled;
        self.infeasibility.prehistory_error_ratio_of_threshold = meta.prehistory_error_ratio;
        self.infeasibility.forecast_error_ratio_of_threshold = meta.prehistory_error_ratio;
        self.infeasibility.prehistory_mock_influence = meta.prehistory_mock_influence;
        self.infeasibility.prehistory_random_seed = meta.seed;
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

    #[test]
    fn concern_specific_defaults_preserve_scheduler_defaults() {
        let cfg = Config::default();

        assert_eq!(cfg.solver.batch_size, 3);
        assert_eq!(cfg.solver.solver_strategy, "dp");
        assert_eq!(cfg.solver.max_batch_solver_parallelism, 20);
        assert_eq!(cfg.simulation.predicted_requests_per_slot, 60.0);
        assert!(cfg.simulation.skip_empty_slots);
        assert!(!cfg.simulation.manual_clock);
        assert_eq!(cfg.infeasibility.recovery_mode, "carryover");
        assert_eq!(cfg.infeasibility.mock_influence, 0.8);
        assert_eq!(cfg.swarm.mode, "serialized");
        assert_eq!(cfg.swarm.aco_n_ants, 10);
        assert!(cfg.logging.enable_solver_logging);
        assert_eq!(cfg.total_slots, 24);
        assert_eq!(cfg.max_error_threshold, 4.0);
    }

    #[test]
    fn assignment_policy_projects_custom_core_settings() {
        let mut cfg = Config::default();
        cfg.flavours = vec![Flavour {
            name: "PolicySentinel".to_string(),
            error: 6.25,
            duration: 137,
        }];
        cfg.capacity_tiers = vec![
            CapacityTier {
                max_requests: Some(7),
                multiplier: 2.75,
            },
            CapacityTier {
                max_requests: None,
                multiplier: 8.5,
            },
        ];
        cfg.total_slots = 73;
        cfg.carbon_cost_duration_scale = 0.125;
        cfg.max_error_threshold = 6.75;
        cfg.error_window_past = 11;
        cfg.error_window_future = 13;
        cfg.error_window_past_decay_slots = 17;
        cfg.assignment_max_future_slots = 19;
        cfg.global_error_constraint_enabled = false;
        cfg.global_error_constraint_hard = false;

        let policy = cfg.assignment_policy();

        assert_eq!(policy.flavours.len(), 1);
        assert_eq!(policy.flavours[0].name, "PolicySentinel");
        assert_eq!(policy.flavours[0].error, 6.25);
        assert_eq!(policy.flavours[0].duration, 137);
        assert_eq!(policy.capacity_tiers.len(), 2);
        assert_eq!(policy.capacity_tiers[0].max_requests, Some(7));
        assert_eq!(policy.capacity_tiers[0].multiplier, 2.75);
        assert_eq!(policy.capacity_tiers[1].max_requests, None);
        assert_eq!(policy.capacity_tiers[1].multiplier, 8.5);
        assert_eq!(policy.total_slots, 73);
        assert_eq!(policy.carbon_cost_duration_scale, 0.125);
        assert_eq!(policy.max_error_threshold, 6.75);
        assert_eq!(policy.error_window_past, 11);
        assert_eq!(policy.error_window_future, 13);
        assert_eq!(policy.error_window_past_decay_slots, 17);
        assert_eq!(policy.assignment_max_future_slots, 19);
        assert!(!policy.global_error_constraint_enabled);
        assert!(!policy.global_error_constraint_hard);
    }
}
