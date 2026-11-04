//! Regression tests for common-runtime scheduling and scheduler orchestration.

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use super::*;
use crate::engine::qos::{
    CumulativeErrorPolicy, ErrorWindowPolicy, QosProfile, QosProfileId, TaskKindId,
};
use crate::engine::solver::{
    CommitReport, ProposedPlacement, SolveInput, SolverAlgorithm, SolverError, SolverProposal,
    SolverRegistry, SolverScope,
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

#[derive(Default)]
struct SolveGateState {
    entered: usize,
    released: bool,
}

#[derive(Default)]
struct SolveGate {
    state: Mutex<SolveGateState>,
    changed: Condvar,
    active_solves: AtomicUsize,
    peak_active_solves: AtomicUsize,
}

impl SolveGate {
    fn wait_for_two_solves(&self, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        let mut state = self.state.lock().unwrap();
        while state.entered < 2 {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return false;
            }
            let (next_state, result) = self.changed.wait_timeout(state, remaining).unwrap();
            state = next_state;
            if result.timed_out() && state.entered < 2 {
                return false;
            }
        }
        true
    }

    fn release(&self) {
        self.state.lock().unwrap().released = true;
        self.changed.notify_all();
    }

    fn enter_and_wait(&self) -> Result<(), SolverError> {
        let active = self.active_solves.fetch_add(1, Ordering::AcqRel) + 1;
        self.peak_active_solves.fetch_max(active, Ordering::Relaxed);

        let deadline = Instant::now() + Duration::from_secs(15);
        let mut state = self.state.lock().map_err(|_| SolverError::LockPoisoned)?;
        state.entered += 1;
        self.changed.notify_all();
        while !state.released {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                self.active_solves.fetch_sub(1, Ordering::AcqRel);
                return Err(SolverError::Algorithm(
                    "test solver timed out waiting for concurrent batch workers".to_owned(),
                ));
            }
            let (next_state, result) = self
                .changed
                .wait_timeout(state, remaining)
                .map_err(|_| SolverError::LockPoisoned)?;
            state = next_state;
            if result.timed_out() && !state.released {
                self.active_solves.fetch_sub(1, Ordering::AcqRel);
                return Err(SolverError::Algorithm(
                    "test solver timed out waiting for concurrent batch workers".to_owned(),
                ));
            }
        }
        self.active_solves.fetch_sub(1, Ordering::AcqRel);
        Ok(())
    }
}

#[derive(Clone)]
struct SchedulerGateState(Vec<u64>);

struct SchedulerGateDelta(Vec<u64>);

struct SchedulerGateAlgorithm {
    gate: Arc<SolveGate>,
    settled_batches: Arc<Mutex<Vec<Vec<(u64, f64)>>>>,
}

impl SolverAlgorithm for SchedulerGateAlgorithm {
    type State = SchedulerGateState;
    type Delta = SchedulerGateDelta;

    fn initial_state(&self, _scope: &SolverScope) -> Self::State {
        SchedulerGateState(Vec::new())
    }

    fn solve(
        &self,
        _snapshot: &Self::State,
        _scope: &SolverScope,
        input: &SolveInput,
    ) -> Result<(SolverProposal, Self::Delta), SolverError> {
        self.gate.enter_and_wait()?;
        let placements = input
            .requests
            .iter()
            .map(|request| {
                ProposedPlacement::new(
                    request.request_id,
                    request.arrival_slot.max(input.current_slot),
                    "Test".to_owned(),
                )
            })
            .collect();
        let request_ids = input
            .requests
            .iter()
            .map(|request| request.request_id)
            .collect();
        Ok((
            SolverProposal { placements },
            SchedulerGateDelta(request_ids),
        ))
    }

    fn merge(&self, state: &mut Self::State, delta: Self::Delta, report: &CommitReport) {
        let proposed_ids: HashSet<_> = delta.0.into_iter().collect();
        let accepted: Vec<_> = report
            .assignments
            .iter()
            .filter(|assignment| proposed_ids.contains(&assignment.request_id))
            .map(|assignment| (assignment.request_id, assignment.carbon_cost))
            .collect();
        state
            .0
            .extend(accepted.iter().map(|(request_id, _)| *request_id));
        self.settled_batches.lock().unwrap().push(accepted);
    }
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
    assert_eq!(input.requests[0].deadline_slot, 5);
    assert_eq!(input.requests[0].assignment_deadline_slot, 3);
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
fn common_solve_input_keeps_real_decayed_history() {
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
    assert_eq!(row.get(column("fallback_assigned")), Some("true"));
    let mut runs_reader = csv::Reader::from_path(&runs_path).unwrap();
    let run_headers = runs_reader.headers().unwrap().clone();
    let run_row = runs_reader.records().next().unwrap().unwrap();
    let fallback_count_column = run_headers
        .iter()
        .position(|header| header == "fallback_assigned_count")
        .unwrap();
    assert_eq!(run_row.get(fallback_count_column), Some("1"));

    drop(assignments_reader);
    drop(runs_reader);
    drop(logger);
    std::fs::remove_file(runs_path).unwrap();
    std::fs::remove_file(assignments_path).unwrap();
}

#[test]
fn online_scheduler_runs_parallel_batches_within_worker_cap_and_merges_final_commit_reports() {
    let mut config = make_config(|config| {
        config.total_slots = 4;
        config.error_window_future = 3;
        config.solver.batch_size = 1;
        config.solver.solver_strategy = "test_gate".to_owned();
        config.solver.max_batch_solver_parallelism = 2;
        config.simulation.manual_clock = true;
        config.simulation.skip_empty_slots = false;
        config.carbon_cost_duration_scale = 1.0;
        config.flavours = vec![Flavour {
            name: "Test".to_owned(),
            error: 1.0,
            duration: 10.0,
        }];
        config.capacity_tiers = vec![
            crate::types::CapacityTier {
                max_requests: Some(1),
                multiplier: 1.0,
            },
            crate::types::CapacityTier {
                max_requests: None,
                multiplier: 2.0,
            },
        ];
    });
    config.solver.batch_timeout_secs = 0.0;
    let config = Arc::new(config);
    let mut qos_profile = profile(
        "scheduler-parallel-test",
        "text_generation",
        config.flavours.clone(),
        100.0,
    );
    Arc::make_mut(&mut qos_profile).error_window.future_slots = 3;

    let gate = Arc::new(SolveGate::default());
    let settled_batches = Arc::new(Mutex::new(Vec::new()));
    let mut registry = SolverRegistry::default();
    let gate_for_factory = Arc::clone(&gate);
    let settlements_for_factory = Arc::clone(&settled_batches);
    registry
        .register("test_gate", move || SchedulerGateAlgorithm {
            gate: Arc::clone(&gate_for_factory),
            settled_batches: Arc::clone(&settlements_for_factory),
        })
        .unwrap();

    let shared_state = SharedState::new();
    shared_state.add_requests(
        (1..=3)
            .map(|request_id| Request::new_for_qos_profile(request_id, 0, 3, qos_profile.clone()))
            .collect(),
    );
    let metrics_logger = Arc::new(MetricsLogger::new(false, String::new(), String::new()));
    let forecast = Arc::new(RwLock::new(flat_forecast(config.total_slots, 100.0)));
    let mut scheduler = BatchScheduler::new_with_parallel_profile_batches(
        shared_state.clone(),
        config.clone(),
        metrics_logger,
        forecast,
    );
    // Replace the built-in registry before starting the scheduler so worker
    // dispatch, runtime caching, commits, and delta settlement remain real.
    scheduler.solver_registry = Arc::new(registry);
    scheduler.start();

    let observed_parallel_solves = gate.wait_for_two_solves(Duration::from_secs(5));
    gate.release();

    let drain_deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < drain_deadline {
        let stats = scheduler.get_statistics();
        if shared_state.get_pending_count() == 0
            && stats.active_batch_workers == 0
            && shared_state.get_current_assignments().len() == 3
        {
            break;
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    scheduler.stop();

    assert!(
        observed_parallel_solves,
        "the two first batch solves must overlap before the gate is released"
    );
    assert_eq!(gate.peak_active_solves.load(Ordering::Acquire), 2);

    let stats = scheduler.get_statistics();
    assert_eq!(stats.active_batch_workers, 0);
    assert_eq!(stats.peak_concurrent_workers, 2);
    assert_eq!(stats.batches_processed, 3);
    assert_eq!(shared_state.get_pending_count(), 0);

    let assignments = shared_state.get_current_assignments();
    assert_eq!(assignments.len(), 3);
    assert_eq!(
        shared_state
            .get_profile_error_stats(&qos_profile.profile_id)
            .count,
        3
    );
    let mut committed_costs: Vec<_> = assignments
        .values()
        .map(|assignment| assignment.carbon_cost)
        .collect();
    committed_costs.sort_by(f64::total_cmp);
    assert_eq!(committed_costs, vec![1_000.0, 2_000.0, 2_000.0]);

    let settled = settled_batches.lock().unwrap();
    assert_eq!(
        settled.len(),
        3,
        "one accepted delta should settle per batch"
    );
    let mut settled_ids_and_costs: Vec<_> = settled.iter().flatten().copied().collect();
    settled_ids_and_costs.sort_by_key(|(request_id, _)| *request_id);
    let mut committed_ids_and_costs: Vec<_> = assignments
        .values()
        .map(|assignment| (assignment.request_id, assignment.carbon_cost))
        .collect();
    committed_ids_and_costs.sort_by_key(|(request_id, _)| *request_id);
    assert_eq!(settled_ids_and_costs, committed_ids_and_costs);
    assert_eq!(
        settled_ids_and_costs
            .iter()
            .map(|(id, _)| *id)
            .collect::<Vec<_>>(),
        vec![1, 2, 3]
    );
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
fn timeout_flush_can_claim_an_aged_partial_profile_while_another_full_profile_is_active() {
    let config = make_config(|config| config.solver.batch_size = 2);
    let full_profile = profile(
        "timeout-full",
        "text_generation",
        config.flavours.clone(),
        config.max_error_threshold,
    );
    let partial_profile = profile(
        "timeout-partial",
        "text_generation",
        config.flavours.clone(),
        config.max_error_threshold,
    );
    let shared_state = SharedState::new();
    shared_state.add_requests(vec![
        Request::new_for_qos_profile(1, 0, 3, full_profile.clone()),
        Request::new_for_qos_profile(2, 0, 3, full_profile.clone()),
    ]);
    shared_state.set_virtual_elapsed_ms(500);
    shared_state.add_request(Request::new_for_qos_profile(
        3,
        0,
        3,
        partial_profile.clone(),
    ));
    shared_state.set_virtual_elapsed_ms(1_500);

    let mutable = Arc::new(Mutex::new(SchedulerMutableState {
        active_workers: 1,
        active_profiles: HashSet::from([full_profile.profile_id.clone()]),
        last_infeasible: HashMap::new(),
        last_dispatched_profile: None,
        stats: SchedulerStats::default(),
        solver_runtimes: HashMap::new(),
    }));

    assert!(!has_dispatchable_full_profile_batch(
        &shared_state,
        &mutable,
        0,
        config.solver.batch_size,
        false,
    ));
    let mut scheduler_state = mutable.lock().unwrap();
    let claimed = claim_next_profile_batch(
        0,
        1,
        false,
        Some(1_000),
        &shared_state,
        &config,
        &mut scheduler_state,
    )
    .expect("the aged partial profile should be eligible for timeout flush");
    assert_eq!(claimed.0, partial_profile.profile_id);
    assert_eq!(
        claimed
            .1
            .requests
            .iter()
            .map(|request| request.id)
            .collect::<Vec<_>>(),
        vec![3]
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
