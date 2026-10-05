//! Regression tests for scheduler batching, scheduling, and clock behavior.

use super::*;
use crate::shared_state::SharedState;
use crate::types::{Assignment, Flavour, Request};

// ── helpers ───────────────────────────────────────────────────────────────

fn make_config(overrides: impl FnOnce(&mut Config)) -> Config {
    let mut cfg = Config {
        solver: crate::config::SolverConfig {
            batch_size: 3,
            dp_lock_future_assignments: true,
            dp_pruning_min_batch_size: 0,
            dp_pruning_method: "none".to_string(),
            ..crate::config::SolverConfig::default()
        },
        total_slots: 24,
        error_window_past: 4,
        error_window_future: 4,
        max_error_threshold: 4.0,
        infeasibility: crate::config::InfeasibilityConfig {
            recovery_mode: "min_error_greedy".to_string(),
            mock_influence: 0.0,
            ..crate::config::InfeasibilityConfig::default()
        },
        logging: crate::config::LoggingConfig {
            verbose: false,
            enable_solver_logging: false,
            ..crate::config::LoggingConfig::default()
        },
        global_error_constraint_enabled: false,
        ..Config::default()
    };
    overrides(&mut cfg);
    cfg
}

fn make_recovery_state(cfg: &Config) -> RecoveryState {
    RecoveryState::new(cfg.infeasibility.mock_influence.clamp(0.0, 1.0))
}

fn flat_forecast(slots: i32, value: f64) -> Vec<f64> {
    vec![value; slots as usize]
}

fn req(id: u64, arrival: i32, deadline: i32) -> Request {
    Request {
        id,
        arrival_slot: arrival,
        arrival_time: 0.0,
        deadline_slot: deadline,
        task_id: "default".to_string(),
        flavours: vec![],
        max_error_threshold: None,
        capacity_tiers: None,
    }
}

fn run_batch_strategy(
    strategy_name: &str,
    current_slot: i32,
    pending: &[Request],
    cfg: &Config,
    shared_state: &SharedState,
    carbon_forecast: &[f64],
) -> (Vec<Assignment>, SolveContext) {
    shared_state.set_current_slot(current_slot);
    let flavour_duration_by_name = cfg
        .flavours
        .iter()
        .map(|f| (f.name.clone(), f.duration))
        .collect();
    let assignment = cfg.assignment_policy();
    let recovery_state = make_recovery_state(cfg);
    let strategy = select_batch_solver(strategy_name);
    let result = strategy.solve(BatchSolveContext {
        current_slot,
        pending,
        shared_state,
        assignment: &assignment,
        solver_config: &cfg.solver,
        simulation_config: &cfg.simulation,
        recovery_config: &cfg.infeasibility,
        recovery_state: &recovery_state,
        verbose: cfg.logging.verbose,
        carbon_forecast,
        flavour_duration_by_name: &flavour_duration_by_name,
    });
    (result.assignments, result.context)
}

fn call_solve_dp(
    current_slot: i32,
    pending: &[Request],
    cfg: &Config,
) -> (Vec<Assignment>, SolveContext) {
    let ss = SharedState::new();
    let forecast = flat_forecast(cfg.total_slots, 100.0);
    run_batch_strategy("dp", current_slot, pending, cfg, &ss, &forecast)
}

fn call_solve_dp_with_state(
    current_slot: i32,
    pending: &[Request],
    cfg: &Config,
    ss: &SharedState,
) -> (Vec<Assignment>, SolveContext) {
    let forecast = flat_forecast(cfg.total_slots, 100.0);
    run_batch_strategy("dp", current_slot, pending, cfg, ss, &forecast)
}

fn call_solve_dp_with_forecast(
    current_slot: i32,
    pending: &[Request],
    cfg: &Config,
    ss: &SharedState,
    forecast: &[f64],
) -> (Vec<Assignment>, SolveContext) {
    run_batch_strategy("dp", current_slot, pending, cfg, ss, forecast)
}

// ── tests ─────────────────────────────────────────────────────────────────

/// All assigned slots must be ≥ current_slot.
#[test]
fn test_scheduled_slots_never_before_current_slot() {
    let cfg = make_config(|_| {});
    let current_slot = 5;
    let pending = vec![
        req(0, current_slot, current_slot + 3),
        req(1, current_slot, current_slot + 3),
        req(2, current_slot, current_slot + 4),
    ];

    let (assignments, _ctx) = call_solve_dp(current_slot, &pending, &cfg);

    assert!(!assignments.is_empty(), "all requests should be scheduled");
    for a in &assignments {
        assert!(
            a.scheduled_slot >= current_slot,
            "scheduled_slot {} < current_slot {}",
            a.scheduled_slot,
            current_slot
        );
    }
}

/// With `dp_lock_future_assignments=true`, pre-existing future assignments
/// are pinned as baseline load and must NOT appear in the DP result.
#[test]
fn test_lock_future_pins_and_excludes_future_assignments() {
    let cfg = make_config(|c| c.solver.dp_lock_future_assignments = true);
    let ss = SharedState::new();
    let current_slot = 2i32;

    // Pre-place a future assignment (slot 6 > current_slot 2).
    let future_id = 99u64;
    ss.add_assignments(vec![Assignment::new(
        future_id,
        6,
        "Fast".to_string(),
        1.0,
        5.0,
        10,
        Some(0),
        Some(8),
    )]);

    let pending = vec![
        req(0, current_slot, current_slot + 3),
        req(1, current_slot, current_slot + 3),
        req(2, current_slot, current_slot + 4),
    ];
    let (assignments, _ctx) = call_solve_dp_with_state(current_slot, &pending, &cfg, &ss);

    let result_ids: HashSet<u64> = assignments.iter().map(|a| a.request_id).collect();
    assert!(
        !result_ids.contains(&future_id),
        "future assignment should not be re-planned when dp_lock_future_assignments=true"
    );
    for i in 0u64..3 {
        assert!(result_ids.contains(&i), "pending request {} missing", i);
    }
}

/// With `dp_lock_future_assignments=false`, future assignments ARE included
/// in the DP pool for joint re-planning.
#[test]
fn test_unlock_future_includes_future_in_dp() {
    let cfg = make_config(|c| c.solver.dp_lock_future_assignments = false);
    let ss = SharedState::new();
    let current_slot = 2i32;

    let future_id = 99u64;
    ss.add_assignments(vec![Assignment::new(
        future_id,
        6,
        "Fast".to_string(),
        1.0,
        5.0,
        10,
        Some(0),
        Some(8),
    )]);

    let pending = vec![
        req(0, current_slot, current_slot + 3),
        req(1, current_slot, current_slot + 3),
        req(2, current_slot, current_slot + 4),
    ];
    let (assignments, _ctx) = call_solve_dp_with_state(current_slot, &pending, &cfg, &ss);

    let result_ids: HashSet<u64> = assignments.iter().map(|a| a.request_id).collect();
    assert!(
        result_ids.contains(&future_id),
        "future assignment should be re-planned when dp_lock_future_assignments=false"
    );
}

/// When all flavours have error > threshold, DP is infeasible.
/// The greedy fallback must cover all requests and the status must reflect it.
#[test]
fn test_greedy_fallback_covers_all_requests() {
    let cfg = make_config(|c| {
        c.max_error_threshold = 0.0;
        c.flavours = vec![Flavour {
            name: "Only".to_string(),
            error: 1.0,
            duration: 10,
        }];
    });

    let current_slot = 0;
    let pending = vec![req(0, 0, 5), req(1, 0, 5), req(2, 0, 5)];
    let (assignments, ctx) = call_solve_dp(current_slot, &pending, &cfg);

    assert_eq!(
        assignments.len(),
        3,
        "greedy fallback should cover all 3 requests"
    );
    assert!(
        ctx.status.contains("greedy"),
        "expected greedy status, got: {}",
        ctx.status
    );
    for a in &assignments {
        assert!(a.scheduled_slot >= 0 && a.scheduled_slot <= 5);
    }
}

/// `get_effective_pruning_mode` returns "none" when batch_size < threshold
/// and the configured method when batch_size >= threshold.
#[test]
fn test_pruning_threshold_gate() {
    let cfg = make_config(|c| {
        c.solver.dp_pruning_min_batch_size = 5;
        c.solver.dp_pruning_method = "beam".to_string();
    });
    assert_eq!(get_effective_pruning_mode(3, &cfg.solver), "none");
    assert_eq!(get_effective_pruning_mode(5, &cfg.solver), "beam");
    assert_eq!(get_effective_pruning_mode(10, &cfg.solver), "beam");
}

/// `get_effective_pruning_mode` returns "none" when the threshold is 0
/// (disabled), regardless of batch size.
#[test]
fn test_pruning_threshold_zero_means_never_prune() {
    let cfg = make_config(|c| {
        c.solver.dp_pruning_min_batch_size = 0;
        c.solver.dp_pruning_method = "beam".to_string();
    });
    assert_eq!(get_effective_pruning_mode(100, &cfg.solver), "none");
}

/// All pending requests should appear in the result even when deadlines vary.
#[test]
fn test_all_pending_requests_are_scheduled() {
    let cfg = make_config(|_| {});
    let current_slot = 0;
    let pending = vec![req(10, 0, 2), req(11, 0, 6), req(12, 0, 10)];
    let (assignments, _ctx) = call_solve_dp(current_slot, &pending, &cfg);

    let result_ids: HashSet<u64> = assignments.iter().map(|a| a.request_id).collect();
    for id in [10u64, 11, 12] {
        assert!(result_ids.contains(&id), "request {} not scheduled", id);
    }
}

/// A request carrying its own (per-task) flavour list must be assigned
/// one of *those* flavours, not one from `cfg.flavours` — this is what
/// lets a dynamically-registered task's flavours actually reach the DP
/// solver (see `Request::new_for_task` / `service::handlers::register_task`).
#[test]
fn test_per_task_flavour_override_reaches_dp_solver() {
    let cfg = make_config(|_| {});
    let current_slot = 0;
    let task_flavour = Flavour {
        name: "OnlyForThisTask".to_string(),
        error: 1.23,
        duration: 45,
    };
    let pending = vec![Request::new_for_task(
        20,
        0,
        5,
        "custom_task".to_string(),
        vec![task_flavour.clone()],
        None,
        None,
    )];
    let (assignments, _ctx) = call_solve_dp(current_slot, &pending, &cfg);

    assert_eq!(assignments.len(), 1);
    assert_eq!(assignments[0].flavour_name, "OnlyForThisTask");
    assert_eq!(assignments[0].error, 1.23);
    // cfg.flavours (the default task) must be untouched by this override.
    assert!(cfg.flavours.iter().all(|f| f.name != "OnlyForThisTask"));
}

/// A per-task `max_error_threshold` override actually changes the DP's
/// choice: without it, the global default (4%) rejects a cheap-but-
/// high-error flavour in favour of the accurate one; with a lenient
/// override, the cheap flavour becomes feasible and wins on cost.
#[test]
fn test_per_task_error_threshold_override_allows_cheaper_flavour() {
    let cfg = make_config(|_| {});
    let current_slot = 0;
    let cheap = Flavour {
        name: "Cheap".to_string(),
        error: 30.0,
        duration: 5,
    };
    let accurate = Flavour {
        name: "Accurate2".to_string(),
        error: 0.0,
        duration: 60,
    };

    let strict = vec![Request::new_for_task(
        30,
        0,
        5,
        "t".to_string(),
        vec![cheap.clone(), accurate.clone()],
        None,
        None,
    )];
    let (assignments_strict, _ctx) = call_solve_dp(current_slot, &strict, &cfg);
    assert_eq!(assignments_strict[0].flavour_name, "Accurate2");

    let lenient = vec![Request::new_for_task(
        31,
        0,
        5,
        "t".to_string(),
        vec![cheap, accurate],
        Some(50.0),
        None,
    )];
    let (assignments_lenient, _ctx) = call_solve_dp(current_slot, &lenient, &cfg);
    assert_eq!(assignments_lenient[0].flavour_name, "Cheap");
}

/// `advance_to_next_slot` should bump the virtual clock by exactly one
/// slot, regardless of `manual_clock`/`skip_empty_slots` settings (it's
/// a direct clock manipulation, not gated by them).
#[test]
fn test_advance_to_next_slot_moves_exactly_one_slot() {
    let cfg = make_config(|c| {
        c.simulation.manual_clock = true;
        c.slot_duration_seconds = 1800.0; // 30 minutes
        c.simulation.slot_speed_scale = 1.0;
    });
    let ss = SharedState::new();
    assert_eq!(ss.get_current_slot(), 0);

    let new_slot = advance_to_next_slot(&ss, cfg.total_slots, cfg.effective_slot_duration_secs());
    assert_eq!(new_slot, 1);
    assert_eq!(ss.virtual_elapsed_secs(), 1800.0);

    let new_slot = advance_to_next_slot(&ss, cfg.total_slots, cfg.effective_slot_duration_secs());
    assert_eq!(new_slot, 2);
    assert_eq!(ss.virtual_elapsed_secs(), 3600.0);
}

/// `advance_to_next_slot` never pushes the slot past `total_slots - 1`.
#[test]
fn test_advance_to_next_slot_clamped_to_horizon() {
    let cfg = make_config(|c| {
        c.simulation.manual_clock = true;
        c.total_slots = 2;
    });
    let ss = SharedState::new();
    ss.set_virtual_elapsed_ms(
        ((cfg.total_slots - 1) as u64) * (cfg.slot_duration_seconds * 1000.0) as u64,
    );
    ss.set_current_slot(cfg.total_slots - 1);

    let new_slot = advance_to_next_slot(&ss, cfg.total_slots, cfg.effective_slot_duration_secs());
    assert_eq!(new_slot, cfg.total_slots - 1);
}

// ── greedy_singleton tests ──────────────────────────────────────────────

fn call_solve_greedy_singleton(
    current_slot: i32,
    pending: &[Request],
    cfg: &Config,
    ss: &SharedState,
) -> (Vec<Assignment>, SolveContext) {
    let forecast = flat_forecast(cfg.total_slots, 100.0);
    run_batch_strategy(
        "greedy_singleton",
        current_slot,
        pending,
        cfg,
        ss,
        &forecast,
    )
}

fn call_solve_greedy_singleton_with_forecast(
    current_slot: i32,
    pending: &[Request],
    cfg: &Config,
    ss: &SharedState,
    forecast: &[f64],
) -> (Vec<Assignment>, SolveContext) {
    run_batch_strategy("greedy_singleton", current_slot, pending, cfg, ss, forecast)
}

#[test]
fn batch_solver_registry_resolves_trimmed_case_insensitive_names() {
    let cfg = make_config(|_| {});
    let current_slot = 1;
    let pending = vec![req(1, current_slot, current_slot + 3)];
    let forecast = flat_forecast(cfg.total_slots, 100.0);
    let shared_state = SharedState::new();

    let (assignments, context) = run_batch_strategy(
        " GREEDY_SINGLETON ",
        current_slot,
        &pending,
        &cfg,
        &shared_state,
        &forecast,
    );

    assert_eq!(assignments.len(), 1);
    assert_eq!(context.mode, "greedy_singleton");
}

#[test]
fn batch_solver_registry_preserves_dp_fallback_for_unknown_names() {
    let cfg = make_config(|_| {});
    let current_slot = 1;
    let pending = vec![req(2, current_slot, current_slot + 3)];
    let forecast = flat_forecast(cfg.total_slots, 100.0);
    let shared_state = SharedState::new();

    let (assignments, context) = run_batch_strategy(
        "not_registered",
        current_slot,
        &pending,
        &cfg,
        &shared_state,
        &forecast,
    );

    assert_eq!(assignments.len(), 1);
    assert!(
        context.mode.starts_with("dp_"),
        "unknown strategies must keep the historical DP fallback, got {}",
        context.mode
    );
}

/// A single request must be scheduled at/after current_slot, tagged with
/// the "greedy_singleton" solver mode.
#[test]
fn test_greedy_singleton_schedules_single_request() {
    let cfg = make_config(|_| {});
    let ss = SharedState::new();
    let current_slot = 3;
    let pending = vec![req(0, current_slot, current_slot + 4)];

    let (assignments, ctx) = call_solve_greedy_singleton(current_slot, &pending, &cfg, &ss);

    assert_eq!(assignments.len(), 1);
    assert!(assignments[0].scheduled_slot >= current_slot);
    assert_eq!(ctx.mode, "greedy_singleton");
    assert_eq!(ctx.status, "ok");
}

/// For a single request (batch_size=1), the exhaustive greedy scan must
/// pick the same (slot, flavour, cost) as the DP solver — with only one
/// request there is no combinatorial ordering effect, so both are
/// exhaustive searches over the same feasible set.
///
/// Uses a non-flat carbon forecast so there is a unique cheapest slot —
/// with a flat forecast every candidate slot ties on cost and each
/// solver's internal (unspecified) tie-breaking order may legitimately
/// differ (DP iterates a HashMap; greedy scans slots ascending).
#[test]
fn test_greedy_singleton_matches_dp_for_single_request() {
    let cfg = make_config(|_| {});
    let current_slot = 2;
    let pending = vec![req(7, current_slot, current_slot + 5)];
    let mut forecast = flat_forecast(cfg.total_slots, 100.0);
    forecast[4] = 10.0; // slot 4 is uniquely the cheapest candidate

    let ss_dp = SharedState::new();
    let (dp_assignments, _) =
        call_solve_dp_with_forecast(current_slot, &pending, &cfg, &ss_dp, &forecast);

    let ss_greedy = SharedState::new();
    let (greedy_assignments, _) = call_solve_greedy_singleton_with_forecast(
        current_slot,
        &pending,
        &cfg,
        &ss_greedy,
        &forecast,
    );

    assert_eq!(dp_assignments.len(), 1);
    assert_eq!(greedy_assignments.len(), 1);
    assert_eq!(dp_assignments[0].scheduled_slot, 4);
    assert_eq!(
        dp_assignments[0].scheduled_slot,
        greedy_assignments[0].scheduled_slot
    );
    assert_eq!(
        dp_assignments[0].flavour_name,
        greedy_assignments[0].flavour_name
    );
    assert!((dp_assignments[0].carbon_cost - greedy_assignments[0].carbon_cost).abs() < 1e-9);
}

/// Requests already committed to a slot must raise the capacity-tier
/// multiplier for subsequent requests placed in the same slot — the
/// greedy scan must read this from live shared state, not start "fresh"
/// every call.
#[test]
fn test_greedy_singleton_respects_existing_slot_load() {
    let mut cfg = make_config(|_| {});
    cfg.capacity_tiers = vec![
        crate::types::CapacityTier {
            max_requests: Some(1),
            multiplier: 1.0,
        },
        crate::types::CapacityTier {
            max_requests: None,
            multiplier: 5.0,
        },
    ];
    let ss = SharedState::new();
    let current_slot = 0;

    // Pre-fill slot 0 with one committed assignment so the *next* request
    // placed there would be position=2 → the expensive (5.0×) tier.
    ss.add_assignments(vec![Assignment::new(
        42,
        0,
        cfg.flavours[0].name.clone(),
        1.0,
        cfg.flavours[0].error,
        cfg.flavours[0].duration,
        Some(0),
        Some(0),
    )]);

    // A request whose only feasible slot is 0 (arrival==deadline==0).
    let pending = vec![req(1, current_slot, current_slot)];
    let (assignments, _ctx) = call_solve_greedy_singleton(current_slot, &pending, &cfg, &ss);

    assert_eq!(assignments.len(), 1);
    assert_eq!(assignments[0].scheduled_slot, 0);
    // Cost must reflect the 5.0× multiplier (position 2), not the 1.0× baseline.
    let cheapest_flavour_duration = cfg.flavours.iter().map(|f| f.duration).min().unwrap();
    let expected_min_cost =
        100.0 * 5.0 * cheapest_flavour_duration as f64 * cfg.carbon_cost_duration_scale;
    assert!(
        assignments[0].carbon_cost >= expected_min_cost - 1e-9,
        "cost {} should reflect the higher capacity tier (>= {})",
        assignments[0].carbon_cost,
        expected_min_cost
    );
}

/// Global error constraint must be retrospective (based on the average
/// error *before* this request), matching solve_dp's step-function
/// behaviour, and must never exclude every flavour.
#[test]
fn test_greedy_singleton_global_error_constraint_is_retrospective() {
    let cfg = make_config(|c| {
        c.global_error_constraint_enabled = true;
        c.global_error_constraint_hard = true;
        c.max_error_threshold = 1.0;
    });
    let ss = SharedState::new();
    let current_slot = 0;

    // Push the global average error above threshold with a high-error assignment.
    ss.add_assignments(vec![Assignment::new(
        1,
        0,
        "Slow".to_string(),
        1.0,
        10.0,
        20,
        Some(0),
        Some(0),
    )]);

    let pending = vec![req(2, current_slot, current_slot + 3)];
    let (assignments, ctx) = call_solve_greedy_singleton(current_slot, &pending, &cfg, &ss);

    assert_eq!(
        assignments.len(),
        1,
        "request must still be scheduled under a hard constraint"
    );
    assert!(ctx.global_error_constraint_active);
}
