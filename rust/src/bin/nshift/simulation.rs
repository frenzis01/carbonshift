//! Streaming scheduler simulation and baseline execution for nshift.

use super::*;

/// Greedy baseline: assign each request immediately on arrival to the "best"
/// flavour (lowest error; mirrors Python's `run_greedy_baseline`).
pub(super) fn run_greedy_baseline(
    scenario: &Scenario,
    assignment: &AssignmentPolicy<'_>,
    slot_duration_seconds: f64,
    realtime_slots: bool,
    realtime_speed_scale: f64,
) -> (Vec<PerRequest>, Vec<BatchTiming>, RunSummary) {
    let flav = assignment
        .flavours
        .iter()
        .min_by(|a, b| a.error.partial_cmp(&b.error).unwrap())
        .expect("assignment policy must have at least one flavour");
    let slot_dur = slot_duration_seconds;
    let carbon = &scenario.carbon_forecast;
    let by_slot = scenario.requests_by_slot();
    let tiers = assignment.capacity_tiers;
    let scale = assignment.carbon_cost_duration_scale;

    let mut per_req: Vec<PerRequest> = Vec::new();
    let mut batch_timings: Vec<BatchTiming> = Vec::new();
    let mut seq: u64 = 0;

    for (slot, requests) in by_slot.iter().enumerate() {
        if requests.is_empty() {
            continue;
        }
        let ci = carbon.get(slot).copied().unwrap_or(1.0);
        let mut before_count: i64 = 0;
        for req in requests {
            seq += 1;
            let position = before_count + 1;
            let mult = get_capacity_multiplier(tiers, position);
            let cost = ci * mult * flav.duration * scale;
            before_count = position;
            per_req.push(PerRequest {
                request_id: req.id,
                arrival_time: req.arrival_slot as f64 * slot_dur,
                arrival_slot: req.arrival_slot,
                deadline_slot: req.deadline_slot,
                included_in_batch_slot: slot as i32,
                batch_sequence: seq,
                scheduled_slot: slot as i32,
                queue_wait_slots: 0,
                queue_wait_seconds: 0.0,
                final_wait_slots: 0,
                final_wait_seconds: 0.0,
                flavour_name: flav.name.clone(),
                error: flav.error,
                carbon_cost: cost,
                assignment_solver_mode: "greedy".to_string(),
                assignment_solver_status: "ok".to_string(),
                assigned_with_greedy_fallback: false,
                assigned_with_relaxed_retry: false,
                assigned_with_injection: false,
                assigned_with_rollback: false,
                lateness_slots: 0,
            });
            batch_timings.push(BatchTiming {
                batch_sequence: seq,
                batch_size_n: 0,
                effective_batch_size: 1,
                slot: slot as i32,
                pending_before: 1,
                solver_elapsed_ms: 0.0,
                scheduled: true,
                flush_partial_batch: false,
            });
        }
    }

    let total_requests: usize = by_slot.iter().map(|s| s.len()).sum();
    let summary = compute_summary(
        "greedy_baseline_immediate",
        0,
        realtime_slots,
        realtime_speed_scale,
        &per_req,
        &batch_timings,
        total_requests,
        &flav.name,
        flav.duration,
        flav.error,
        0,
        0,
        0,
        0,   // peak_concurrent_workers
        0.0, // avg_concurrent_workers
    );
    (per_req, batch_timings, summary)
}

/// Greedy late scheduling: assign pending requests that remained unprocessed
/// after the run ended.
///
/// `scheduled_slot` is set to `deadline_slot` (clamped to [arrival_slot, total_slots-1])
/// so that the carbon cost is computed at the slot the request *should* have occupied.
///
/// `lateness_slots` = (total_slots-1) - deadline_slot, i.e. how far past the deadline
/// the request was still sitting unprocessed when the run ended. This is always >= 0
/// for drained requests because their deadline has already passed by end-of-run.
pub(super) fn schedule_late_requests(
    remaining: Vec<carbonshift_rs::types::Request>,
    carbon_forecast: &[f64],
    assignment: &AssignmentPolicy<'_>,
    slot_duration_seconds: f64,
) -> Vec<PerRequest> {
    if remaining.is_empty() {
        return Vec::new();
    }

    let slot_dur = slot_duration_seconds;
    let tiers = assignment.capacity_tiers;
    let scale = assignment.carbon_cost_duration_scale;
    let total_slots = assignment.total_slots;
    let fallback_flav = assignment
        .flavours
        .iter()
        .min_by(|a, b| a.error.partial_cmp(&b.error).unwrap())
        .expect("no flavours");

    let mut slot_count: HashMap<i32, i64> = HashMap::new();
    let mut out = Vec::with_capacity(remaining.len());

    let mut sorted = remaining;
    sorted.sort_by_key(|r| (r.deadline_slot, r.arrival_slot, r.id));

    for req in sorted {
        let sched_slot = req.deadline_slot.max(req.arrival_slot).min(total_slots - 1);
        let position = slot_count.get(&sched_slot).copied().unwrap_or(0) + 1;
        let ci = carbon_forecast
            .get(sched_slot as usize)
            .copied()
            .unwrap_or(1.0);
        let mult = get_capacity_multiplier(tiers, position);
        let cost = ci * mult * fallback_flav.duration * scale;
        // Lateness = how many slots past the deadline the request sat unprocessed.
        // We measure from the end of the run (total_slots-1), not from sched_slot,
        // because all drained requests were unprocessed until the run ended.
        let lateness = ((total_slots - 1) - req.deadline_slot).max(0);
        let queue_wait = (sched_slot - req.arrival_slot).max(0);

        *slot_count.entry(sched_slot).or_insert(0) += 1;

        out.push(PerRequest {
            request_id: req.id,
            arrival_time: req.arrival_slot as f64 * slot_dur,
            arrival_slot: req.arrival_slot,
            deadline_slot: req.deadline_slot,
            included_in_batch_slot: sched_slot,
            batch_sequence: 0,
            scheduled_slot: sched_slot,
            queue_wait_slots: queue_wait,
            queue_wait_seconds: queue_wait as f64 * slot_dur,
            final_wait_slots: queue_wait,
            final_wait_seconds: queue_wait as f64 * slot_dur,
            flavour_name: fallback_flav.name.clone(),
            error: fallback_flav.error,
            carbon_cost: cost,
            assignment_solver_mode: "late_fallback".to_string(),
            assignment_solver_status: "late".to_string(),
            assigned_with_greedy_fallback: false,
            assigned_with_relaxed_retry: false,
            assigned_with_injection: false,
            assigned_with_rollback: false,
            lateness_slots: lateness,
        });
    }
    out
}

pub(super) fn run_single_n(
    scenario: &Scenario,
    base_cfg: &Config,
    batch_size: usize,
    realtime_slots: bool,
    realtime_speed_scale: f64,
    run_dir: &Path,
    verbose: bool,
) -> (Vec<PerRequest>, Vec<BatchTiming>, RunSummary) {
    // Build per-run config.
    let mut cfg = base_cfg.clone();
    cfg.solver.batch_size = batch_size;
    cfg.logging.verbose = verbose;
    // For fast simulation: use skip_empty_slots=true with slot_speed_scale=1.0.
    // The skip mechanism advances the virtual clock when a slot is empty, so the
    // scheduler races through the scenario without waiting for real time.
    // Do NOT set slot_speed_scale=0 here: with 0, the wall clock advances faster
    // than 1 virtual-slot/ms, causing the monitor to exit before all requests are
    // scheduled (the scheduler can't keep up with a 1ms/slot clock).
    if !realtime_slots {
        cfg.simulation.skip_empty_slots = true;
        cfg.simulation.slot_speed_scale = 1.0;
    } else {
        cfg.simulation.skip_empty_slots = false;
        cfg.simulation.slot_speed_scale = realtime_speed_scale;
    }
    cfg.logging.enable_solver_logging = true;
    let tmp_runs = run_dir.join("_solver_runs.csv");
    let tmp_assignments = run_dir.join("_solver_assignments.csv");
    let tmp_slot_mets = run_dir.join("_solver_slot_metrics.csv");
    cfg.logging.solver_runs_file = tmp_runs.to_str().unwrap().to_string();
    cfg.logging.solver_assignments_file = tmp_assignments.to_str().unwrap().to_string();
    cfg.logging.solver_slot_metrics_file = tmp_slot_mets.to_str().unwrap().to_string();
    cfg.logging.enable_infeasibility_debug_logging = false;
    cfg.simulation.total_requests = scenario.requests.len();

    let cfg = Arc::new(cfg);

    std::fs::create_dir_all(run_dir).unwrap();
    // Remove any stale temp files from a previous interrupted run so the
    // MetricsLogger (which appends) starts with a clean slate.
    let _ = std::fs::remove_file(&tmp_runs);
    let _ = std::fs::remove_file(&tmp_assignments);
    let _ = std::fs::remove_file(&tmp_slot_mets);

    let shared_state = SharedState::new();
    let ml = Arc::new(MetricsLogger::new(
        true,
        tmp_runs.to_str().unwrap().to_string(),
        tmp_assignments.to_str().unwrap().to_string(),
        tmp_slot_mets.to_str().unwrap().to_string(),
        None,
    ));

    let by_slot = scenario.requests_by_slot();
    let generator_cfg = RequestGeneratorConfig::from_config(&cfg);
    let mut generator =
        RequestGenerator::new_from_scenario(shared_state.clone(), generator_cfg, by_slot);
    let mut sched = BatchScheduler::new_with_parallel_profile_batches(
        shared_state.clone(),
        cfg.clone(),
        ml,
        Arc::new(RwLock::new(scenario.carbon_forecast.clone())),
    );

    sched.start();
    generator.start();

    // Phase 1: wait until the generator has finished emitting every slot.
    // `generator_processed_slot()` is the authoritative "generator done" signal
    // (set once per slot, right after that slot's requests are added). The
    // virtual clock (`virtual_elapsed_secs()`) is advanced independently by the
    // scheduler and must NOT be used as this gate: the generator now applies
    // backpressure (waiting for the scheduler to drain each slot before moving
    // on, see `generator.rs::wait_for_drain`), so under heavy load it can fall
    // far behind the virtual clock. Waiting on the clock alone would let this
    // phase end while the generator is still mid-scenario, and the fixed-size
    // drain timeout below would then cut it off, silently dropping every
    // not-yet-emitted slot's requests.
    let last_slot = cfg.total_slots - 1;
    loop {
        std::thread::sleep(std::time::Duration::from_millis(50));
        if shared_state.generator_processed_slot() >= last_slot {
            break;
        }
    }

    // Phase 2: let the scheduler drain the remaining pending queue.
    // Requests may still be waiting (or mid-solve) when Phase 1 ends, since
    // Phase 1 only guarantees the generator has *emitted* every slot, not that
    // the scheduler has finished processing them. We keep the scheduler
    // running until pending==0 AND every worker has finished.
    let drain_deadline = std::time::Instant::now() + std::time::Duration::from_secs(60);
    loop {
        let stats = sched.get_statistics();
        let pending = shared_state.get_pending_count();
        if (pending == 0 && stats.active_batch_workers == 0)
            || std::time::Instant::now() > drain_deadline
        {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }

    generator.stop();
    let sched_stats = sched.get_statistics();
    sched.stop();

    let total_received = shared_state.get_statistics().total_received as usize;
    let rollback_stats = shared_state.get_rollback_stats();
    let rolled_back_ids = shared_state.get_rolled_back_request_ids();

    // Post-process Rust CSVs into Python-compatible tables.
    let runs = read_solver_runs(&tmp_runs);
    let assignment_rows = read_solver_assignments(&tmp_assignments);
    let runs_by_id: HashMap<String, &SolverRunRow> =
        runs.iter().map(|r| (r.run_id.clone(), r)).collect();

    let mut per_req = compute_per_request(
        &assignment_rows,
        &runs_by_id,
        base_cfg.slot_duration_seconds,
        &rolled_back_ids,
    );
    // Drain any requests that never made it into a batch (e.g., deadline
    // expired while waiting in the pending queue) and schedule them late.
    let remaining = shared_state.drain_pending_requests();
    if !remaining.is_empty() {
        let late = schedule_late_requests(
            remaining,
            &scenario.carbon_forecast,
            &base_cfg.assignment_policy(),
            base_cfg.slot_duration_seconds,
        );
        per_req.extend(late);
    }
    // Correct per-request carbon costs using final committed state to eliminate
    // concurrent-baseline drift (see recompute_carbon_costs for details).
    let final_assignments = shared_state.get_current_assignments();
    recompute_carbon_costs(
        &mut per_req,
        &final_assignments,
        &scenario.carbon_forecast,
        &base_cfg.capacity_tiers,
        base_cfg.carbon_cost_duration_scale,
    );
    let batch_timings = compute_batch_timings(&runs, batch_size);
    let exec_mode = if cfg.solver.solver_strategy == "dp" || cfg.solver.solver_strategy.is_empty() {
        "nshift_dp".to_string()
    } else {
        format!("nshift_{}", cfg.solver.solver_strategy)
    };
    let summary = compute_summary(
        &exec_mode,
        batch_size,
        realtime_slots,
        realtime_speed_scale,
        &per_req,
        &batch_timings,
        total_received,
        "",
        0.0,
        0.0,
        rollback_stats.total_rollbacks,
        rollback_stats.max_consecutive_rollbacks as u64,
        rolled_back_ids.len(),
        sched_stats.peak_concurrent_workers,
        sched_stats.avg_concurrent_workers,
    );

    // Clean up temp files.
    let _ = std::fs::remove_file(&tmp_runs);
    let _ = std::fs::remove_file(&tmp_assignments);
    let _ = std::fs::remove_file(&tmp_slot_mets);

    // Print a definitive final progress line overwriting whatever partial
    // line the main_loop left behind.  This runs after late scheduling so
    // the numbers are always 100%.
    if !cfg.logging.verbose {
        let total = scenario.requests.len();
        let scheduled = per_req.len();
        let pct = if total > 0 {
            scheduled as f64 / total as f64 * 100.0
        } else {
            0.0
        };
        println!(
            "\r  [N={:2}] Scheduled {:>6}/{:<6} ({:5.1}%)  Received: {:>6}",
            batch_size, scheduled, total, pct, total_received
        );
    }

    (per_req, batch_timings, summary)
}

// ─── main ─────────────────────────────────────────────────────────────────────
