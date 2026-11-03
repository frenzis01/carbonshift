//! Regression tests for common-runtime scheduling and scheduler orchestration.

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use super::*;
use crate::engine::qos::{
    CumulativeErrorPolicy, ErrorWindowPolicy, QosProfile, QosProfileId, TaskKindId,
};
use crate::shared_state::SharedState;
use crate::types::{Assignment, Flavour, Request};

fn make_config(overrides: impl FnOnce(&mut Config)) -> Config {
    let mut config = Config {
        solver: crate::config::SolverConfig {
            batch_size: 1,
            dp_pruning_min_batch_size: 0,
            dp_pruning_method: "none".to_owned(),
            ..crate::config::SolverConfig::default()
        },
        total_slots: 24,
        error_window_past: 4,
        error_window_future: 4,
        max_error_threshold: 4.0,
        logging: crate::config::LoggingConfig {
            verbose: false,
            enable_solver_logging: false,
            ..crate::config::LoggingConfig::default()
        },
        global_error_constraint_enabled: false,
        ..Config::default()
    };
    overrides(&mut config);
    config
}

fn flat_forecast(slots: i32, value: f64) -> Vec<f64> {
    vec![value; slots as usize]
}

fn profile(
    profile_id: &str,
    task_kind: &str,
    flavours: Vec<Flavour>,
    max_error_threshold: f64,
) -> Arc<QosProfile> {
    Arc::new(QosProfile {
        profile_id: QosProfileId::parse(profile_id).unwrap(),
        task_kind: TaskKindId::parse(task_kind).unwrap(),
        flavours,
        error_semantics: format!("{task_kind}-error-v1"),
        max_error_threshold,
        error_window: ErrorWindowPolicy {
            past_slots: 0,
            future_slots: 0,
            past_decay_slots: 0,
        },
        cumulative_error: CumulativeErrorPolicy {
            enabled: false,
            hard: false,
        },
    })
}

#[test]
fn common_solve_input_uses_global_fixed_occupancy_and_profile_only_errors() {
    let config = make_config(|_| {});
    let mut profile_a = profile(
        "profile-a",
        "text_generation",
        config.flavours.clone(),
        config.max_error_threshold,
    );
    Arc::make_mut(&mut profile_a).error_window.future_slots = 2;
    let profile_a_id = profile_a.profile_id.clone();
    let profile_b_id = QosProfileId::parse("profile-b").unwrap();

    let shared_state = SharedState::new();
    shared_state.add_assignments(vec![
        Assignment::new_for_profile(
            90,
            1,
            "Fast".to_owned(),
            1.0,
            2.0,
            10.0,
            Some(0),
            Some(4),
            profile_a_id.clone(),
        ),
        Assignment::new_for_profile(
            91,
            2,
            "Fast".to_owned(),
            1.0,
            99.0,
            10.0,
            Some(0),
            Some(4),
            profile_b_id,
        ),
        Assignment::new_for_profile(
            92,
            3,
            "Fast".to_owned(),
            1.0,
            4.0,
            10.0,
            Some(0),
            Some(5),
            profile_a_id.clone(),
        ),
    ]);

    let request = Request::new_for_qos_profile(10, 1, 5, profile_a.clone());
    let assignment = config.assignment_policy_for_profile(&profile_a);
    let snapshot = shared_state.snapshot_for_solver();
    let forecast = flat_forecast(config.total_slots, 100.0);
    let input = super::input::build_solve_input_from_snapshot(
        1,
        &[request],
        &snapshot,
        &assignment,
        &forecast,
    )
    .unwrap();

    assert_eq!(input.qos.profile_id, profile_a_id);
    assert_eq!(input.requests.len(), 1);
    assert_eq!(input.baseline_slot_counts.get(&1), Some(&1));
    assert_eq!(input.baseline_slot_counts.get(&2), Some(&1));
    assert_eq!(input.baseline_slot_counts.get(&3), Some(&1));
    assert_eq!(input.qos.cumulative_err.count, 2.0);
    assert_eq!(input.qos.cumulative_err.sum, 6.0);
    assert_eq!(input.qos.err_by_slot.get(&1).unwrap().sum, 2.0);
    assert_eq!(input.qos.err_by_slot.get(&3).unwrap().sum, 4.0);
    assert!(!input.qos.err_by_slot.contains_key(&2));
    assert_eq!(input.qos.error_window.baseline_err.count, 2.0);
    assert_eq!(input.qos.error_window.baseline_err.sum, 6.0);
    assert_eq!(input.forecast_max_slot, Some(config.total_slots - 1));
}

#[test]
fn common_solve_input_keeps_real_decayed_history_without_synthetic_prehistory() {
    let config = make_config(|config| {
        config.error_window_past = 1;
        config.error_window_past_decay_slots = 2;
    });
    let mut qos_profile = profile(
        "decayed-history",
        "text_generation",
        config.flavours.clone(),
        config.max_error_threshold,
    );
    Arc::make_mut(&mut qos_profile).error_window.past_slots = 1;
    Arc::make_mut(&mut qos_profile)
        .error_window
        .past_decay_slots = 2;
    let profile_id = qos_profile.profile_id.clone();
    let shared_state = SharedState::new();
    shared_state.add_assignments(vec![
        Assignment::new_for_profile(
            1,
            1,
            "Fast".to_owned(),
            1.0,
            4.0,
            1.0,
            Some(1),
            Some(1),
            profile_id.clone(),
        ),
        Assignment::new_for_profile(
            2,
            0,
            "Fast".to_owned(),
            1.0,
            2.0,
            1.0,
            Some(0),
            Some(0),
            profile_id.clone(),
        ),
    ]);

    let request = Request::new_for_qos_profile(3, 3, 3, qos_profile.clone());
    let assignment = config.assignment_policy_for_profile(&qos_profile);
    let snapshot = shared_state.snapshot_for_solver();
    let input = super::input::build_solve_input_from_snapshot(
        3,
        &[request],
        &snapshot,
        &assignment,
        &flat_forecast(config.total_slots, 100.0),
    )
    .unwrap();

    assert!((input.qos.error_window.baseline_err.sum - 10.0 / 3.0).abs() < 1e-9);
    assert!((input.qos.error_window.baseline_err.count - 1.0).abs() < 1e-9);
}

#[test]
fn common_worker_reports_greedy_fallback_and_selected_flavour() {
    let config = make_config(|config| {
        config.solver.solver_strategy = "dp".to_owned();
        config.flavours = vec![
            Flavour {
                name: "FastButInaccurate".to_owned(),
                error: 10.0,
                duration: 1.0,
            },
            Flavour {
                name: "LowestError".to_owned(),
                error: 1.0,
                duration: 10.0,
            },
        ];
    });
    let qos_profile = profile(
        "fallback-test",
        "text_generation",
        config.flavours.clone(),
        0.0,
    );
    let shared_state = SharedState::new();
    shared_state.initialize_capacity_tiers(config.capacity_tiers.clone());
    shared_state.add_request(Request::new_for_qos_profile(1000, 0, 0, qos_profile));

    let claimed = shared_state.claim_pending_requests_for_profile_with_snapshot(
        &QosProfileId::parse("fallback-test").unwrap(),
        1,
    );
    let registry = build_solver_registry(&config);
    let forecast = flat_forecast(config.total_slots, 100.0);
    let mutable = Arc::new(Mutex::new(SchedulerMutableState {
        active_workers: 1,
        active_profiles: HashSet::new(),
        last_infeasible: HashMap::new(),
        last_dispatched_profile: None,
        stats: SchedulerStats::default(),
        solver_runtimes: HashMap::new(),
    }));

    let prefix = std::env::temp_dir().join(format!(
        "carbonshift-fallback-metrics-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    let runs_path = prefix.with_extension("runs.csv");
    let assignments_path = prefix.with_extension("assignments.csv");
    let logger = MetricsLogger::new(
        true,
        runs_path.to_string_lossy().into_owned(),
        assignments_path.to_string_lossy().into_owned(),
    );

    let outcome = batch_worker_entry(
        0,
        claimed.requests,
        claimed.snapshot,
        &shared_state,
        &config,
        &registry,
        &forecast,
        &mutable,
        &logger,
    );
    assert!(matches!(outcome, BatchWorkerOutcome::Committed));

    let mut assignments_reader = csv::Reader::from_path(&assignments_path).unwrap();
    let headers = assignments_reader.headers().unwrap().clone();
    let row = assignments_reader.records().next().unwrap().unwrap();
    let column = |name: &str| headers.iter().position(|header| header == name).unwrap();
    assert_eq!(row.get(column("flavour_name")), Some("LowestError"));
    assert_eq!(
        row.get(column("assigned_with_greedy_fallback")),
        Some("true")
    );
    let mut runs_reader = csv::Reader::from_path(&runs_path).unwrap();
    let run_headers = runs_reader.headers().unwrap().clone();
    let run_row = runs_reader.records().next().unwrap().unwrap();
    let fallback_count_column = run_headers
        .iter()
        .position(|header| header == "greedy_fallback_assignments")
        .unwrap();
    assert_eq!(run_row.get(fallback_count_column), Some("1"));

    drop(assignments_reader);
    drop(runs_reader);
    drop(logger);
    std::fs::remove_file(runs_path).unwrap();
    std::fs::remove_file(assignments_path).unwrap();
}

#[test]
fn profile_dispatch_round_robins_and_respects_profile_serialization() {
    let qa = QosProfileId::parse("qa-standard-v1").unwrap();
    let ner = QosProfileId::parse("ner-standard-v1").unwrap();
    let pending = vec![(qa.clone(), 5), (ner.clone(), 5)];
    let active_profiles = HashSet::new();
    let infeasible = HashMap::new();

    assert_eq!(
        next_profile_to_dispatch(&pending, None, &active_profiles, true, &infeasible, 3, 2),
        Some((ner.clone(), 5))
    );
    assert_eq!(
        next_profile_to_dispatch(
            &pending,
            Some(&ner),
            &active_profiles,
            true,
            &infeasible,
            3,
            2,
        ),
        Some((qa.clone(), 5))
    );

    let single_profile = vec![(qa.clone(), 10)];
    let active_profiles = HashSet::from([qa.clone()]);
    assert_eq!(
        next_profile_to_dispatch(
            &single_profile,
            None,
            &active_profiles,
            false,
            &infeasible,
            3,
            2,
        ),
        None
    );
    assert_eq!(
        next_profile_to_dispatch(
            &single_profile,
            None,
            &active_profiles,
            true,
            &infeasible,
            3,
            2,
        ),
        Some((qa, 10))
    );
}

#[test]
fn average_worker_concurrency_uses_worker_busy_time() {
    let start = Instant::now();
    let mut stats = SchedulerStats::default();
    stats.start(start);
    stats.record_worker_count_change(0, 0, start);
    let two_seconds = start + Duration::from_secs(2);
    stats.record_worker_count_change(0, 2, two_seconds);
    let five_seconds_later = two_seconds + Duration::from_secs(3);
    stats.record_worker_count_change(2, 0, five_seconds_later);
    let end = five_seconds_later + Duration::from_secs(5);
    stats.finish(0, end);

    assert!((stats.average_concurrent_workers(0, end) - 2.0).abs() < 1e-9);
}

#[test]
fn advance_to_next_slot_moves_exactly_one_slot() {
    let config = make_config(|config| {
        config.simulation.manual_clock = true;
        config.slot_duration_seconds = 1800.0;
        config.simulation.slot_speed_scale = 1.0;
    });
    let shared_state = SharedState::new();
    assert_eq!(shared_state.get_current_slot(), 0);

    assert_eq!(
        advance_to_next_slot(
            &shared_state,
            config.total_slots,
            config.effective_slot_duration_secs()
        ),
        1
    );
    assert_eq!(shared_state.virtual_elapsed_secs(), 1800.0);

    assert_eq!(
        advance_to_next_slot(
            &shared_state,
            config.total_slots,
            config.effective_slot_duration_secs()
        ),
        2
    );
    assert_eq!(shared_state.virtual_elapsed_secs(), 3600.0);
}

#[test]
fn advance_to_next_slot_is_clamped_to_horizon() {
    let config = make_config(|config| {
        config.simulation.manual_clock = true;
        config.total_slots = 2;
    });
    let shared_state = SharedState::new();
    shared_state.set_virtual_elapsed_ms(
        ((config.total_slots - 1) as u64) * (config.slot_duration_seconds * 1000.0) as u64,
    );
    shared_state.set_current_slot(config.total_slots - 1);

    assert_eq!(
        advance_to_next_slot(
            &shared_state,
            config.total_slots,
            config.effective_slot_duration_secs()
        ),
        config.total_slots - 1
    );
}
