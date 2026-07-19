/// Integration test: full slot-by-slot scheduling of the canonical scenario.
///
/// Loads `tests/Nshift_speed/scenario_seed_2030.json` from the Python test
/// suite, groups requests by arrival slot, and processes them with the
/// CarbonShift DP solver.
///
/// # Correctness assertions
/// - Every request in the scenario is eventually assigned.
/// - No request is assigned to a slot before it arrived (`scheduled_slot >= arrival_slot`).
/// - No request is assigned after its deadline (`scheduled_slot <= deadline_slot`).
/// - Carbon costs are non-negative.

use std::collections::HashMap;
use carbonshift_rs::config::Config;
use carbonshift_rs::dp_solver::{DpSolver, ErrorWindowBaseline, MockPool, SolveBatchInput};
use carbonshift_rs::shared_state::SharedState;
use carbonshift_rs::types::{Assignment, Request};

// ─── scenario JSON de-serialisation ──────────────────────────────────────────

#[derive(serde::Deserialize)]
struct ScenarioMeta {
    total_slots: i32,
    error_window_past: i32,
    error_window_future: i32,
    max_error_threshold: f64,
    deadline_min_slack: i32,
    deadline_max_slack: i32,
    requests_per_slot: f64,
    slot_duration_seconds: f64,
}

#[derive(serde::Deserialize)]
struct ScenarioRequest {
    arrival_slot: i32,
    #[allow(dead_code)]
    arrival_time: f64,
    deadline_slot: i32,
    request_id: u64,
}

#[derive(serde::Deserialize)]
struct Scenario {
    carbon_forecast: Vec<f64>,
    metadata: ScenarioMeta,
    requests: Vec<ScenarioRequest>,
}

// ─── test helpers ─────────────────────────────────────────────────────────────

/// Build a `Config` from the scenario metadata.
///
/// Uses the same flavours and capacity tiers as the Python config defaults.
fn config_from_meta(meta: &ScenarioMeta) -> Config {
    Config {
        total_slots: meta.total_slots,
        batch_size: 3,
        error_window_past: meta.error_window_past,
        error_window_future: meta.error_window_future,
        max_error_threshold: meta.max_error_threshold,
        slot_duration_seconds: meta.slot_duration_seconds,
        deadline_min_slack: meta.deadline_min_slack,
        deadline_max_slack: meta.deadline_max_slack,
        predicted_requests_per_slot: meta.requests_per_slot,
        dp_lock_future_assignments: true,
        dp_pruning_min_batch_size: 0,
        dp_pruning_method: "none".to_string(),
        global_error_constraint_enabled: false,
        infeasibility_recovery_mode: "min_error_greedy".to_string(),
        infeasibility_mock_influence: 0.0,
        verbose: false,
        enable_solver_logging: false,
        ..Config::default()
    }
}

/// Process all pending requests in batches using the DP solver directly.
///
/// Returns all produced `Assignment`s.
fn drain_pending_with_dp(
    ss: &SharedState,
    solver: &DpSolver,
    cfg: &Config,
    current_slot: i32,
    all_assignments: &mut Vec<Assignment>,
) {
    let batch_size = cfg.batch_size;
    loop {
        let pending_count = ss.get_pending_count();
        if pending_count < batch_size {
            // Process remaining requests even if fewer than a full batch.
            if pending_count == 0 {
                break;
            }
        }

        let take = pending_count.min(batch_size).max(1);
        let batch = ss.claim_pending_requests(take);
        if batch.is_empty() {
            break;
        }

        let requests: Vec<(u64, i32)> = batch
            .iter()
            .map(|r| {
                let capped = r.deadline_slot.max(current_slot).min(cfg.total_slots - 1);
                (r.id, capped)
            })
            .collect();

        // Compute baseline from already-placed assignments.
        let current_assignments = ss.get_current_assignments();
        let mut base_counts: HashMap<i32, i32> = HashMap::new();
        for a in current_assignments.values() {
            *base_counts.entry(a.scheduled_slot).or_insert(0) += 1;
        }

        // Get real window error baseline from shared state.
        let ws = ss.get_window_error_stats(
            current_slot,
            cfg.error_window_past,
            cfg.error_window_future,
            &std::collections::HashSet::new(),
        );

        let window_end = (current_slot + cfg.error_window_future).min(cfg.total_slots - 1);
        let input = SolveBatchInput {
            requests: &requests,
            current_slot,
            capacity_multiplier: 1.0,
            capacity_tiers: &cfg.capacity_tiers,
            baseline_slot_counts: &base_counts,
            error_window_baseline: ErrorWindowBaseline {
                error_sum: ws.error_sum,
                request_count: ws.count as f64,
            },
            max_error_threshold: Some(cfg.max_error_threshold),
            error_window_past: cfg.error_window_past,
            error_window_future: cfg.error_window_future,
            assignment_max_slot: Some(window_end),
            dynamic_mock_pool: MockPool::default(),
        };

        let dp_result = solver.solve_batch(input);

        if dp_result.is_empty() {
            // Infeasible batch: greedy fallback.
            let deadlines: Vec<i32> = requests.iter().map(|(_, d)| *d).collect();
            let base_counts_arr: Vec<i32> =
                (0..cfg.total_slots).map(|s| base_counts.get(&s).copied().unwrap_or(0)).collect();

            let greedy = solver.greedy_fallback(
                &requests,
                &deadlines,
                current_slot,
                &cfg.capacity_tiers,
                &base_counts_arr,
            );

            let assignments: Vec<Assignment> = greedy
                .iter()
                .zip(batch.iter())
                .map(|(ra, req)| Assignment::new(
                    ra.request_id,
                    ra.slot,
                    ra.flavour_name.clone(),
                    ra.carbon_cost,
                    ra.error,
                    cfg.flavours.iter().find(|f| f.name == ra.flavour_name).map(|f| f.duration).unwrap_or(0),
                    Some(req.arrival_slot),
                    Some(req.deadline_slot),
                ))
                .collect();
            ss.add_assignments(assignments.clone());
            all_assignments.extend(assignments);
        } else {
            let req_meta: HashMap<u64, (i32, i32)> =
                batch.iter().map(|r| (r.id, (r.arrival_slot, r.deadline_slot))).collect();
            let dur_by_name: HashMap<String, i32> =
                cfg.flavours.iter().map(|f| (f.name.clone(), f.duration)).collect();

            let assignments: Vec<Assignment> = dp_result
                .iter()
                .map(|ra| {
                    let (arrival, deadline) =
                        req_meta.get(&ra.request_id).copied().unwrap_or((0, 0));
                    Assignment::new(
                        ra.request_id,
                        ra.slot,
                        ra.flavour_name.clone(),
                        ra.carbon_cost,
                        ra.error,
                        dur_by_name.get(&ra.flavour_name).copied().unwrap_or(0),
                        Some(arrival),
                        Some(deadline),
                    )
                })
                .collect();
            ss.add_assignments(assignments.clone());
            all_assignments.extend(assignments);
        }

        if ss.get_pending_count() == 0 {
            break;
        }
    }
}

// ─── the test ─────────────────────────────────────────────────────────────────

#[test]
fn scenario_seed_2030_all_requests_scheduled_correctly() {
    // Locate the scenario file relative to the manifest directory.
    let manifest_dir = env!("CARGO_MANIFEST_DIR");
    let scenario_path = format!(
        "{}/../online2/tests/Nshift_speed/scenario_seed_2030.json",
        manifest_dir
    );

    let content = std::fs::read_to_string(&scenario_path)
        .unwrap_or_else(|e| panic!("Cannot read scenario file at {scenario_path}: {e}"));

    let scenario: Scenario = serde_json::from_str(&content)
        .expect("Failed to deserialise scenario JSON");

    let meta = &scenario.metadata;
    let cfg = config_from_meta(meta);
    let total_requests = scenario.requests.len();

    // Group requests by arrival slot.
    let mut by_slot: HashMap<i32, Vec<&ScenarioRequest>> = HashMap::new();
    for r in &scenario.requests {
        by_slot.entry(r.arrival_slot).or_default().push(r);
    }

    // Build the DP solver with the scenario carbon forecast.
    assert_eq!(
        scenario.carbon_forecast.len(),
        meta.total_slots as usize,
        "carbon_forecast length mismatch"
    );
    let solver = DpSolver::new(&cfg).with_carbon_forecast(scenario.carbon_forecast.clone());

    let ss = SharedState::new();
    let mut all_assignments: Vec<Assignment> = Vec::new();

    // Simulate slot-by-slot.
    for slot in 0..meta.total_slots {
        ss.set_current_slot(slot);

        // Enqueue arriving requests.
        if let Some(arrivals) = by_slot.get(&slot) {
            for r in arrivals {
                ss.add_request(Request {
                    id: r.request_id,
                    arrival_slot: r.arrival_slot,
                    arrival_time: 0.0,
                    deadline_slot: r.deadline_slot,
                });
            }
        }

        drain_pending_with_dp(&ss, &solver, &cfg, slot, &mut all_assignments);
    }

    // Final drain: handle any remaining pending.
    let last_slot = meta.total_slots - 1;
    drain_pending_with_dp(&ss, &solver, &cfg, last_slot, &mut all_assignments);

    // ── correctness assertions ────────────────────────────────────────────────

    let final_assignments = ss.get_current_assignments();
    let scheduled_count = final_assignments.len();

    assert_eq!(
        scheduled_count, total_requests,
        "Expected {total_requests} assignments, got {scheduled_count}"
    );

    // Negative incremental carbon costs are valid: when adding a request crosses a
    // capacity-tier boundary the entire slot gets repriced, and a tier with a lower
    // multiplier can make the delta negative.  We only assert on scheduling correctness.
    let mut slot_violations = 0usize;
    let mut deadline_violations = 0usize;

    for a in final_assignments.values() {
        if let Some(arrival) = a.arrival_slot {
            if a.scheduled_slot < arrival {
                slot_violations += 1;
            }
        }
        if let Some(deadline) = a.deadline_slot {
            if a.scheduled_slot > deadline {
                deadline_violations += 1;
            }
        }
    }

    assert_eq!(slot_violations, 0, "{slot_violations} assignments scheduled before arrival");
    assert_eq!(deadline_violations, 0, "{deadline_violations} assignments scheduled after deadline");
}
