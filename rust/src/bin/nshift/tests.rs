//! Unit tests for the nshift scenario benchmark.

use super::*;
use carbonshift_rs::scenario::{ScenarioMetadata, ScenarioRequest};
use carbonshift_rs::types::CapacityTier;

fn make_scenario(requests: Vec<ScenarioRequest>, carbon_forecast: Vec<f64>) -> Scenario {
    Scenario {
        metadata: ScenarioMetadata {
            total_slots: carbon_forecast.len() as i32,
            slot_duration_seconds: 60.0,
            requests_per_slot: 0.0,
            request_rate_std_factor: 0.0,
            deadline_min_slack: 0,
            deadline_max_slack: 1,
            max_error_threshold: 7.5,
            error_window_past: 0,
            error_window_future: 0,
            error_window_past_decay_slots: 0,
            carbon_intensity_cycle_slots: 2,
            carbon_random_noise_amplitude: 0.0,
            seed: 1234,
            capacity_tiers: None,
        },
        carbon_forecast,
        requests,
    }
}

fn make_request(id: u64, arrival_slot: i32, deadline_slot: i32) -> ScenarioRequest {
    ScenarioRequest {
        request_id: id,
        arrival_slot,
        deadline_slot,
        arrival_time: arrival_slot as f64 * 60.0,
    }
}

fn make_assignment(id: u64, slot: i32, dur: f64) -> Assignment {
    Assignment::new(id, slot, "Balanced".into(), 0.0, 0.0, dur, None, None)
}

fn make_per_req(id: u64, slot: i32) -> PerRequest {
    PerRequest {
        request_id: id,
        arrival_time: 0.0,
        arrival_slot: slot,
        deadline_slot: slot,
        included_in_batch_slot: slot,
        batch_sequence: 0,
        scheduled_slot: slot,
        queue_wait_slots: 0,
        queue_wait_seconds: 0.0,
        final_wait_slots: 0,
        final_wait_seconds: 0.0,
        flavour_name: "Balanced".into(),
        error: 0.0,
        carbon_cost: 999.0, // deliberately wrong — will be recomputed
        assignment_solver_mode: "dp".into(),
        assignment_solver_status: "ok".into(),
        assigned_with_greedy_fallback: false,
        assigned_with_relaxed_retry: false,
        assigned_with_injection: false,
        assigned_with_rollback: false,
        lateness_slots: 0,
    }
}

#[test]
fn compute_per_request_marks_assignments_with_injected_mock_pool() {
    let run_rows = vec![
        SolverRunRow {
            run_id: "injected".into(),
            run_sequence: 1,
            current_slot: 0,
            pending_batch_size: 1,
            new_assignments: 1,
            solver_elapsed_ms: 0.0,
            solver_status: "ok".into(),
            solver_mode: "dp_none".into(),
            mock_recovery_injected_count: 2,
        },
        SolverRunRow {
            run_id: "plain".into(),
            run_sequence: 2,
            current_slot: 0,
            pending_batch_size: 1,
            new_assignments: 1,
            solver_elapsed_ms: 0.0,
            solver_status: "ok".into(),
            solver_mode: "dp_none".into(),
            mock_recovery_injected_count: 0,
        },
    ];
    let runs_by_id = run_rows
        .iter()
        .map(|run| (run.run_id.clone(), run))
        .collect::<HashMap<_, _>>();
    let assignments = ["injected", "plain"]
        .into_iter()
        .enumerate()
        .map(|(i, run_id)| AssignmentRow {
            run_id: run_id.into(),
            request_id: i as u64 + 1,
            current_slot: 0,
            is_new_assignment_in_run: true,
            assigned_with_greedy_fallback: false,
            scheduled_slot: 0,
            flavour_name: "Accurate".into(),
            error: 0.0,
            carbon_cost: 0.0,
            arrival_slot: 0,
            deadline_slot: 1,
        })
        .collect::<Vec<_>>();

    let rows = compute_per_request(&assignments, &runs_by_id, 60.0, &HashSet::new());

    assert_eq!(rows.len(), 2);
    assert!(rows[0].assigned_with_injection);
    assert!(!rows[1].assigned_with_injection);
}

#[test]
fn compact_solver_logs_keep_legacy_report_inputs_available() {
    let prefix = std::env::temp_dir().join(format!(
        "carbonshift-nshift-compact-log-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    let runs_path = prefix.with_extension("runs.csv");
    let assignments_path = prefix.with_extension("assignments.csv");

    std::fs::write(
        &runs_path,
        concat!(
            "run_id,run_sequence,current_slot,qos_profile_id,solver_mode,solver_status,",
            "new_assignments,solver_elapsed_ms,total_carbon_cost,error_window_avg_after,",
            "error_window_avg_after_real,error_window_start_slot,error_window_end_slot,",
            "error_window_threshold,profile_error_constraint_active\n",
            "run_s0_n1_1,1,0,default,dp,ok,1,1.5,2.0,0.0,0.0,0,1,4.0,false\n"
        ),
    )
    .unwrap();
    std::fs::write(
        &assignments_path,
        concat!(
            "run_id,current_slot,request_id,qos_profile_id,scheduled_slot,flavour_name,",
            "assigned_with_greedy_fallback,flavour_duration_sec,error,carbon_cost,",
            "arrival_slot,deadline_slot\n",
            "run_s0_n1_1,0,1,default,0,Accurate,false,30,0.0,2.0,0,1\n"
        ),
    )
    .unwrap();

    let runs = super::reporting::read_solver_runs(&runs_path);
    let assignments = super::reporting::read_solver_assignments(&assignments_path);

    assert_eq!(runs.len(), 1);
    assert_eq!(runs[0].pending_batch_size, 1);
    assert_eq!(runs[0].new_assignments, 1);
    assert_eq!(runs[0].mock_recovery_injected_count, 0);
    assert_eq!(runs[0].solver_mode, "dp");
    assert_eq!(assignments.len(), 1);
    assert!(assignments[0].is_new_assignment_in_run);
    assert!(!assignments[0].assigned_with_greedy_fallback);

    std::fs::remove_file(runs_path).unwrap();
    std::fs::remove_file(assignments_path).unwrap();
}

/// Single tier (mult=1.0 up to 20): all 10 requests pay the same.
/// Under per-request model: each costs carbon * 1.0 * dur * scale.
#[test]
fn recompute_corrects_concurrent_baseline_drift() {
    let tiers = vec![CapacityTier {
        max_requests: Some(20),
        multiplier: 1.0,
    }];
    let carbon_forecast = vec![120.0_f64]; // slot 0 only
    let scale = 1.0 / 3600.0;

    // 10 requests in slot 0, all duration 30s
    let final_assignments: HashMap<u64, Assignment> = (1..=10u64)
        .map(|id| (id, make_assignment(id, 0, 30.0)))
        .collect();

    let mut per_req: Vec<PerRequest> = (1..=10u64).map(|id| make_per_req(id, 0)).collect();

    recompute_carbon_costs(
        &mut per_req,
        &final_assignments,
        &carbon_forecast,
        &tiers,
        scale,
    );

    // Per-request model: each request at position 1..10 all in tier 1 (mult=1.0)
    // cost per request = 120 * 1.0 * 30 * scale
    let per_req_expected = 120.0 * 1.0 * 30.0 * scale;
    for r in &per_req {
        assert!(
            (r.carbon_cost - per_req_expected).abs() < 1e-9,
            "request {} cost={} expected={}",
            r.request_id,
            r.carbon_cost,
            per_req_expected
        );
    }
    let expected_total = per_req_expected * 10.0;
    let computed_total: f64 = per_req.iter().map(|r| r.carbon_cost).sum();
    assert!(
        (computed_total - expected_total).abs() < 1e-9,
        "total={computed_total}, expected={expected_total}"
    );
}

/// Tier boundary crossing: 70 requests split across two tiers.
/// Positions 1-60 use mult=1.0, positions 61-70 use mult=2.0.
/// Earlier requests are NOT repriced when the boundary is crossed.
#[test]
fn recompute_handles_tier_crossing_correctly() {
    let tiers = vec![
        CapacityTier {
            max_requests: Some(60),
            multiplier: 1.0,
        },
        CapacityTier {
            max_requests: None,
            multiplier: 2.0,
        },
    ];
    let carbon = 100.0_f64;
    let scale = 1.0 / 3600.0;

    let final_assignments: HashMap<u64, Assignment> = (1..=70u64)
        .map(|id| (id, make_assignment(id, 0, 30.0)))
        .collect();
    let mut per_req: Vec<PerRequest> = (1..=70u64).map(|id| make_per_req(id, 0)).collect();

    recompute_carbon_costs(&mut per_req, &final_assignments, &[carbon], &tiers, scale);

    // Positions 1..60 → mult=1.0; positions 61..70 → mult=2.0
    // IDs are sorted 1..70 so IDs 1..60 are positions 1..60, IDs 61..70 are positions 61..70
    let cost_tier1 = carbon * 1.0 * 30.0 * scale;
    let cost_tier2 = carbon * 2.0 * 30.0 * scale;
    per_req.sort_by_key(|r| r.request_id);
    for r in &per_req[..60] {
        assert!(
            (r.carbon_cost - cost_tier1).abs() < 1e-9,
            "id={}: cost={}, expected={cost_tier1}",
            r.request_id,
            r.carbon_cost
        );
    }
    for r in &per_req[60..] {
        assert!(
            (r.carbon_cost - cost_tier2).abs() < 1e-9,
            "id={}: cost={}, expected={cost_tier2}",
            r.request_id,
            r.carbon_cost
        );
    }
    let expected_total = 60.0 * cost_tier1 + 10.0 * cost_tier2;
    let computed_total: f64 = per_req.iter().map(|r| r.carbon_cost).sum();
    assert!(
        (computed_total - expected_total).abs() < 1e-9,
        "total={computed_total}, expected={expected_total}"
    );
}

// ─── load_benchmark_config: online_batch_sizes parsing ────────────────

/// Writes `json_body` to a uniquely-named file under the OS temp dir and
/// returns its path; avoids pulling in a `tempfile` dependency for tests.
fn write_tmp_config(name: &str, json_body: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "nshift_test_{name}_{}.json",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::write(&path, json_body).unwrap();
    path
}

#[test]
fn online_batch_sizes_absent_defaults_to_none() {
    let path = write_tmp_config(
        "absent",
        r#"{"batch_sizes": [1, 4], "scenario_path": "s.json", "output_dir": "out", "runner": {}}"#,
    );
    let bcfg = load_benchmark_config(&path);
    std::fs::remove_file(&path).ok();
    assert_eq!(bcfg.online_batch_sizes, None);
    assert_eq!(bcfg.batch_sizes, vec![1, 4]);
}

#[test]
fn online_batch_sizes_explicit_value_is_parsed() {
    let path = write_tmp_config(
        "explicit",
        r#"{"batch_sizes": [], "scenario_path": "s.json", "output_dir": "out",
                "runner": {"online_batch_sizes": [1, 6, 12]}}"#,
    );
    let bcfg = load_benchmark_config(&path);
    std::fs::remove_file(&path).ok();
    assert_eq!(bcfg.online_batch_sizes, Some(vec![1, 6, 12]));
    assert!(bcfg.batch_sizes.is_empty());
}

#[test]
fn online_batch_sizes_only_sweep_for_ant_colony() {
    let configured = [1, 4, 8];
    for strategy in ["bandit", "singleton_best", "hazard"] {
        assert_eq!(
            online_batch_sizes_for_strategy(strategy, &configured),
            vec![1]
        );
    }
    assert_eq!(
        online_batch_sizes_for_strategy("ant_colony", &configured),
        configured
    );
}

#[test]
fn random_flavour_probabilities_match_threshold_with_nearest_uniform_mix() {
    let probabilities = carbonshift_rs::random_selection::closest_to_uniform_flavour_probabilities(
        &[0.0, 5.0, 10.0],
        7.5,
    )
    .unwrap();

    assert!((probabilities[0] - 1.0 / 12.0).abs() < 1e-9);
    assert!((probabilities[1] - 1.0 / 3.0).abs() < 1e-9);
    assert!((probabilities[2] - 7.0 / 12.0).abs() < 1e-9);
    let average_error: f64 = probabilities
        .iter()
        .zip([0.0, 5.0, 10.0])
        .map(|(probability, error)| probability * error)
        .sum();
    assert!((average_error - 7.5).abs() < 1e-9);
}

#[test]
fn random_flavour_probabilities_support_boundary_and_interior_targets() {
    let low_boundary = carbonshift_rs::random_selection::closest_to_uniform_flavour_probabilities(
        &[0.0, 0.0, 10.0],
        0.0,
    )
    .unwrap();
    assert_eq!(low_boundary, vec![0.5, 0.5, 0.0]);

    let low_target = carbonshift_rs::random_selection::closest_to_uniform_flavour_probabilities(
        &[0.0, 5.0, 10.0],
        1.0,
    )
    .unwrap();
    assert!(low_target.iter().all(|probability| *probability >= 0.0));
    let average_error: f64 = low_target
        .iter()
        .zip([0.0, 5.0, 10.0])
        .map(|(probability, error)| probability * error)
        .sum();
    assert!((average_error - 1.0).abs() < 1e-9);
}

#[test]
fn random_flavour_probabilities_reject_unattainable_targets() {
    let error = carbonshift_rs::random_selection::closest_to_uniform_flavour_probabilities(
        &[0.0, 5.0, 10.0],
        12.0,
    )
    .unwrap_err();
    assert!(error.contains("outside the flavour error range"));
}

#[test]
fn offline_strategy_names_resolve_to_common_engine_names() {
    assert_eq!(
        canonical_offline_engine_name("offline_singleton_best").unwrap(),
        ("singleton_best", "offline_singleton_best")
    );
    assert_eq!(
        canonical_offline_engine_name("offline_singleton_best_accurate").unwrap(),
        ("singleton_best_accurate", "offline_singleton_best_accurate")
    );
    assert!(canonical_offline_engine_name("random").is_err());
    assert!(canonical_offline_engine_name("not-a-solver").is_err());

    let registry = build_solver_registry(&Config::default(), 42).unwrap();
    let scope = SolverScope {
        profile_id: QosProfileId::default_profile(),
        total_slots: 2,
    };
    for name in [
        "dp",
        "singleton_best",
        "singleton_best_accurate",
        "hazard",
        "bandit",
        "ant_colony",
    ] {
        assert!(registry.create(name, scope.clone()).is_ok());
    }
}

#[test]
fn offline_singleton_best_accurate_uses_min_error_flavour_and_cheapest_slot() {
    let scenario = make_scenario(vec![make_request(1, 0, 1)], vec![100.0, 10.0]);
    let mut config = Config::default();
    config.total_slots = 2;
    config.max_error_threshold = 7.5;
    config.error_window_past = 0;
    config.error_window_future = 1;
    config.global_error_constraint_enabled = false;
    config.carbon_cost_duration_scale = 1.0;
    config.capacity_tiers = vec![CapacityTier {
        max_requests: None,
        multiplier: 1.0,
    }];
    config.flavours = vec![
        Flavour {
            name: "Accurate".to_string(),
            error: 0.0,
            duration: 10.0,
        },
        Flavour {
            name: "Fast".to_string(),
            error: 5.0,
            duration: 1.0,
        },
    ];

    let registry =
        build_solver_registry(&config, scenario.metadata.seed ^ HAZARD_SEED_SALT).unwrap();
    let (assignments, _, summary) = run_strategy(
        &registry,
        "offline_singleton_best_accurate",
        &scenario,
        &config,
        60.0,
        false,
        1.0,
    )
    .unwrap();

    assert_eq!(assignments[0].flavour_name, "Accurate");
    assert_eq!(assignments[0].scheduled_slot, 1);
    assert_eq!(summary.execution_mode, "offline_singleton_best_accurate");

    let (best_assignments, _, best_summary) = run_strategy(
        &registry,
        "offline_singleton_best",
        &scenario,
        &config,
        60.0,
        false,
        1.0,
    )
    .unwrap();
    assert_eq!(best_assignments[0].flavour_name, "Fast");
    assert_eq!(best_assignments[0].scheduled_slot, 1);
    assert_eq!(best_summary.execution_mode, "offline_singleton_best");
}

#[test]
fn offline_dp_marks_greedy_fallback_and_reports_its_flavour() {
    let scenario = make_scenario(vec![make_request(7, 0, 1)], vec![10.0, 1.0]);
    let mut config = Config::default();
    config.total_slots = 2;
    config.error_window_future = 1;
    config.max_error_threshold = 0.0;
    config.global_error_constraint_enabled = false;
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
    let registry = build_solver_registry(&config, 42).unwrap();

    let (per_request, _, summary) =
        run_strategy(&registry, "dp", &scenario, &config, 60.0, false, 1.0).unwrap();

    assert_eq!(per_request.len(), 1);
    assert!(per_request[0].assigned_with_greedy_fallback);
    assert_eq!(per_request[0].flavour_name, "LowestError");
    assert_eq!(summary.requests_assigned_with_greedy_fallback, 1);
}

#[test]
fn offline_common_engine_commit_prices_each_next_capacity_position() {
    let scenario = make_scenario(
        vec![make_request(1, 0, 0), make_request(2, 0, 0)],
        vec![10.0],
    );
    let mut config = Config::default();
    config.total_slots = 1;
    config.error_window_future = 0;
    config.max_error_threshold = 0.0;
    config.global_error_constraint_enabled = false;
    config.carbon_cost_duration_scale = 1.0;
    config.capacity_tiers = vec![
        CapacityTier {
            max_requests: Some(1),
            multiplier: 1.0,
        },
        CapacityTier {
            max_requests: None,
            multiplier: 5.0,
        },
    ];
    config.flavours = vec![Flavour {
        name: "Accurate".to_owned(),
        error: 0.0,
        duration: 2.0,
    }];
    let registry = build_solver_registry(&config, 42).unwrap();

    let (assignments, _, _) = run_strategy(
        &registry,
        "singleton_best",
        &scenario,
        &config,
        60.0,
        false,
        1.0,
    )
    .unwrap();

    assert_eq!(assignments.len(), 2);
    assert_eq!(assignments[0].carbon_cost, 20.0);
    assert_eq!(assignments[1].carbon_cost, 100.0);
}

#[test]
fn offline_common_runner_executes_bandit_and_aco_through_the_registry() {
    let scenario = make_scenario(
        vec![make_request(1, 0, 1), make_request(2, 0, 1)],
        vec![100.0, 10.0],
    );
    let mut config = Config::default();
    config.total_slots = 2;
    config.error_window_future = 1;
    config.max_error_threshold = 0.0;
    config.global_error_constraint_enabled = false;
    config.flavours = vec![Flavour {
        name: "Accurate".to_owned(),
        error: 0.0,
        duration: 1.0,
    }];
    let registry = build_solver_registry(&config, 42).unwrap();

    for strategy in ["bandit", "ant_colony"] {
        let (assignments, _, summary) =
            run_strategy(&registry, strategy, &scenario, &config, 60.0, false, 1.0).unwrap();

        assert_eq!(assignments.len(), 2);
        assert_eq!(summary.execution_mode, strategy);
        assert!(
            assignments
                .iter()
                .all(|assignment| assignment.carbon_cost.is_finite())
        );
    }
}

#[test]
fn offline_output_preserves_the_proposal_late_flag() {
    let scenario = make_scenario(vec![make_request(1, 0, 1)], vec![10.0; 4]);
    let mut config = Config::default();
    config.total_slots = 4;
    config.error_window_future = 3;
    config.global_error_constraint_enabled = false;
    let assignment = config.assignment_policy();
    let (scope, mut solve_input) = project_scenario_to_solve_input(&scenario, &assignment).unwrap();
    solve_input.current_slot = 2;
    let proposal = carbonshift_rs::engine::solver::SolverProposal {
        placements: vec![carbonshift_rs::engine::solver::ProposedPlacement::late(
            1,
            2,
            "Accurate".to_owned(),
        )],
    };

    let materialized =
        materialize_offline_proposal(&proposal, &solve_input, &scope, &scenario, &assignment)
            .unwrap();
    assert!(materialized[0].late);

    let (per_request, _, summary) = convert_engine_results_to_outputs(
        materialized,
        "test",
        &scenario,
        &assignment,
        60.0,
        false,
        1.0,
    );

    assert_eq!(per_request[0].lateness_slots, 1);
    assert_eq!(summary.requests_late, 1);
}

#[test]
fn offline_proposal_validation_rejects_assignments_past_the_deadline() {
    let scenario = make_scenario(vec![make_request(1, 0, 0)], vec![10.0; 2]);
    let mut config = Config::default();
    config.total_slots = 2;
    config.error_window_future = 1;
    let assignment = config.assignment_policy();
    let (scope, solve_input) = project_scenario_to_solve_input(&scenario, &assignment).unwrap();
    let proposal = carbonshift_rs::engine::solver::SolverProposal {
        placements: vec![carbonshift_rs::engine::solver::ProposedPlacement::new(
            1,
            1,
            "Accurate".to_owned(),
        )],
    };

    assert!(
        materialize_offline_proposal(&proposal, &solve_input, &scope, &scenario, &assignment,)
            .is_err()
    );
}

#[test]
fn hazard_is_seeded_and_targets_threshold_in_expectation() {
    let requests = (0..20_000).map(|id| make_request(id, 0, 1)).collect();
    let scenario = make_scenario(requests, vec![100.0, 100.0]);
    let mut config = Config::default();
    config.total_slots = 2;
    config.error_window_future = 1;
    config.max_error_threshold = 7.5;
    config.global_error_constraint_enabled = false;
    config.flavours = vec![
        Flavour {
            name: "Accurate".to_string(),
            error: 0.0,
            duration: 10.0,
        },
        Flavour {
            name: "Balanced".to_string(),
            error: 5.0,
            duration: 5.0,
        },
        Flavour {
            name: "Fast".to_string(),
            error: 10.0,
            duration: 1.0,
        },
    ];

    config.error_window_future = 1;
    let registry =
        build_solver_registry(&config, scenario.metadata.seed ^ HAZARD_SEED_SALT).unwrap();
    let (assignments, _, _) =
        run_strategy(&registry, "hazard", &scenario, &config, 60.0, false, 1.0).unwrap();
    let (repeated, _, _) =
        run_strategy(&registry, "hazard", &scenario, &config, 60.0, false, 1.0).unwrap();
    let average_error = assignments
        .iter()
        .map(|assignment| assignment.error)
        .sum::<f64>()
        / assignments.len() as f64;

    assert!((average_error - 7.5).abs() < 0.15);
    assert!(
        assignments
            .iter()
            .all(|assignment| (0..=1).contains(&assignment.scheduled_slot))
    );
    assert!(
        assignments
            .iter()
            .map(|assignment| (&assignment.flavour_name, assignment.scheduled_slot))
            .eq(repeated
                .iter()
                .map(|assignment| (&assignment.flavour_name, assignment.scheduled_slot)))
    );
}
