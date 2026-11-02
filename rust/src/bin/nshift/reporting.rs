//! Scenario-run reporting, summaries, and Python-compatible output.

use super::*;

// ─── row types (post-processed metrics) ──────────────────────────────────────

#[derive(Clone)]
pub(super) struct SolverRunRow {
    pub(super) run_id: String,
    pub(super) run_sequence: u64,
    pub(super) current_slot: i32,
    pub(super) pending_batch_size: usize,
    pub(super) new_assignments: usize,
    pub(super) solver_elapsed_ms: f64,
    pub(super) solver_status: String,
    pub(super) solver_mode: String,
    pub(super) mock_recovery_injected_count: i32,
}

#[derive(Clone)]
pub(super) struct AssignmentRow {
    pub(super) run_id: String,
    pub(super) request_id: u64,
    pub(super) current_slot: i32,
    pub(super) is_new_assignment_in_run: bool,
    pub(super) scheduled_slot: i32,
    pub(super) flavour_name: String,
    pub(super) error: f64,
    pub(super) carbon_cost: f64,
    pub(super) arrival_slot: i32,
    pub(super) deadline_slot: i32,
}

// Python-compatible per_request row
pub(super) struct PerRequest {
    pub(super) request_id: u64,
    pub(super) arrival_time: f64,
    pub(super) arrival_slot: i32,
    pub(super) deadline_slot: i32,
    pub(super) included_in_batch_slot: i32,
    pub(super) batch_sequence: u64,
    pub(super) scheduled_slot: i32,
    pub(super) queue_wait_slots: i32,
    pub(super) queue_wait_seconds: f64,
    pub(super) final_wait_slots: i32,
    pub(super) final_wait_seconds: f64,
    pub(super) flavour_name: String,
    pub(super) error: f64,
    pub(super) carbon_cost: f64,
    pub(super) assignment_solver_mode: String,
    pub(super) assignment_solver_status: String,
    pub(super) assigned_with_greedy_fallback: bool,
    pub(super) assigned_with_relaxed_retry: bool,
    /// The request's assigning solve had a nonempty mock recovery pool.
    pub(super) assigned_with_injection: bool,
    pub(super) assigned_with_rollback: bool,
    /// Slots past deadline at scheduling time (0 = on time or early).
    pub(super) lateness_slots: i32,
}

pub(super) struct BatchTiming {
    pub(super) batch_sequence: u64,
    pub(super) batch_size_n: usize,
    pub(super) effective_batch_size: usize,
    pub(super) slot: i32,
    pub(super) pending_before: usize,
    pub(super) solver_elapsed_ms: f64,
    pub(super) scheduled: bool,
    pub(super) flush_partial_batch: bool,
}

pub(super) struct PerTimeslot {
    pub(super) timeslot: i32,
    pub(super) window_start: i32,
    pub(super) window_end: i32,
    pub(super) real_request_count: f64,
    pub(super) modeled_request_count: f64,
    pub(super) window_avg_error_real: f64,
    pub(super) window_avg_error_modeled: f64,
}

// Python-compatible summary row (flatten_summary_for_csv equivalent)
#[derive(Clone)]
pub(super) struct RunSummary {
    pub(super) execution_mode: String,
    pub(super) batch_size: usize,
    pub(super) realtime_slots: bool,
    pub(super) realtime_speed_scale: f64,
    pub(super) baseline_flavour_name: String,
    pub(super) baseline_flavour_duration: f64,
    pub(super) baseline_flavour_error: f64,
    pub(super) requests_total: usize,
    pub(super) requests_scheduled: usize,
    pub(super) requests_unscheduled: usize,
    pub(super) batches_executed: usize,
    pub(super) total_carbon_cost: f64,
    pub(super) global_average_error: f64,
    pub(super) global_average_error_real: f64,
    pub(super) global_average_error_modeled: f64,
    pub(super) global_average_error_real_skip_first_k: f64,
    pub(super) global_average_error_modeled_skip_first_k: f64,
    pub(super) requests_assigned_with_greedy_fallback: usize,
    pub(super) requests_assigned_with_relaxed_retry: usize,
    pub(super) total_rollbacks: u64,
    pub(super) max_consecutive_rollbacks: u64,
    pub(super) requests_assigned_with_rollback: usize,
    pub(super) peak_concurrent_workers: usize,
    pub(super) avg_concurrent_workers: f64,
    /// Requests scheduled past their deadline (late flush or post-run drain).
    pub(super) requests_late: usize,
    /// Maximum lateness observed (slots past deadline, 0 = all on time).
    pub(super) max_lateness_slots: i32,
    pub(super) solver_time_ms_min: f64,
    pub(super) solver_time_ms_max: f64,
    pub(super) solver_time_ms_avg: f64,
    pub(super) queue_wait_seconds_min: f64,
    pub(super) queue_wait_seconds_max: f64,
    pub(super) queue_wait_seconds_avg: f64,
    pub(super) final_wait_seconds_min: f64,
    pub(super) final_wait_seconds_max: f64,
    pub(super) final_wait_seconds_avg: f64,
    pub(super) baseline_total_carbon_cost: f64,
    pub(super) carbon_cost_saving_vs_baseline: f64,
    pub(super) carbon_cost_saving_vs_baseline_pct: f64,
    /// Wall-clock seconds spent on this single N run (0.0 for the greedy baseline).
    pub(super) run_elapsed_seconds: f64,
}

// ─── CSV readers ──────────────────────────────────────────────────────────────

pub(super) fn read_solver_runs(path: &Path) -> Vec<SolverRunRow> {
    let Ok(mut rdr) = csv::Reader::from_path(path) else {
        return Vec::new();
    };
    let headers: Vec<String> = match rdr.headers() {
        Ok(h) => h.iter().map(|s| s.to_string()).collect(),
        Err(_) => return Vec::new(),
    };
    let idx = |name: &str| headers.iter().position(|h| h == name);
    let i_run_id = idx("run_id");
    let i_seq = idx("run_sequence");
    let i_slot = idx("current_slot");
    let i_new = idx("new_assignments");
    let i_pending = idx("pending_batch_size").or(i_new);
    let i_new = i_new.or(i_pending);
    let i_ms = idx("solver_elapsed_ms");
    let i_status = idx("solver_status");
    let i_mode = idx("solver_mode");
    let i_mock_recovery_injected = idx("mock_recovery_injected_count");

    let mut rows = Vec::new();
    for result in rdr.records() {
        let Ok(rec) = result else { continue };
        let g = |i: Option<usize>| i.and_then(|j| rec.get(j)).unwrap_or("").to_string();
        let gf = |i: Option<usize>| g(i).parse::<f64>().unwrap_or(0.0);
        let gi = |i: Option<usize>| g(i).parse::<i64>().unwrap_or(0);
        rows.push(SolverRunRow {
            run_id: g(i_run_id),
            run_sequence: gi(i_seq) as u64,
            current_slot: gi(i_slot) as i32,
            pending_batch_size: gi(i_pending) as usize,
            new_assignments: gi(i_new) as usize,
            solver_elapsed_ms: gf(i_ms),
            solver_status: g(i_status),
            solver_mode: g(i_mode),
            mock_recovery_injected_count: gi(i_mock_recovery_injected) as i32,
        });
    }
    rows
}

pub(super) fn read_solver_assignments(path: &Path) -> Vec<AssignmentRow> {
    let Ok(mut rdr) = csv::Reader::from_path(path) else {
        return Vec::new();
    };
    let headers: Vec<String> = match rdr.headers() {
        Ok(h) => h.iter().map(|s| s.to_string()).collect(),
        Err(_) => return Vec::new(),
    };
    let idx = |name: &str| headers.iter().position(|h| h == name);
    let i_run_id = idx("run_id");
    let i_rid = idx("request_id");
    let i_slot = idx("current_slot");
    let i_is_new = idx("is_new_assignment_in_run");
    let i_sched = idx("scheduled_slot");
    let i_flav = idx("flavour_name");
    let i_err = idx("error");
    let i_cost = idx("carbon_cost");
    let i_arr = idx("arrival_slot");
    let i_dead = idx("deadline_slot");

    let mut rows = Vec::new();
    for result in rdr.records() {
        let Ok(rec) = result else { continue };
        let g = |i: Option<usize>| i.and_then(|j| rec.get(j)).unwrap_or("").to_string();
        let gf = |i: Option<usize>| g(i).parse::<f64>().unwrap_or(0.0);
        let gi = |i: Option<usize>| g(i).parse::<i64>().unwrap_or(0);
        let is_new = i_is_new
            .map(|index| {
                let value = g(Some(index)).to_lowercase();
                value == "true" || value == "1"
            })
            .unwrap_or(true);
        rows.push(AssignmentRow {
            run_id: g(i_run_id),
            request_id: gi(i_rid) as u64,
            current_slot: gi(i_slot) as i32,
            is_new_assignment_in_run: is_new,
            scheduled_slot: gi(i_sched) as i32,
            flavour_name: g(i_flav),
            error: gf(i_err),
            carbon_cost: gf(i_cost),
            arrival_slot: gi(i_arr) as i32,
            deadline_slot: gi(i_dead) as i32,
        });
    }
    rows
}

// ─── metrics computation ──────────────────────────────────────────────────────

pub(super) fn compute_per_request(
    assignments: &[AssignmentRow],
    runs_by_id: &HashMap<String, &SolverRunRow>,
    slot_dur: f64,
    rolled_back_ids: &HashSet<u64>,
) -> Vec<PerRequest> {
    let mut seen: HashMap<u64, PerRequest> = HashMap::new();
    for a in assignments.iter().filter(|a| a.is_new_assignment_in_run) {
        seen.entry(a.request_id).or_insert_with(|| {
            let run = runs_by_id.get(a.run_id.as_str());
            let batch_seq = run.map(|r| r.run_sequence).unwrap_or(0);
            let solver_status = run.map(|r| r.solver_status.clone()).unwrap_or_default();
            let solver_mode = run.map(|r| r.solver_mode.clone()).unwrap_or_default();
            let assigned_with_relaxed = solver_mode.contains("relaxed");
            let assigned_with_greedy = solver_mode.contains("greedy_after_infeasible");
            let assigned_with_injection = run
                .map(|r| r.mock_recovery_injected_count > 0)
                .unwrap_or(false);
            let assigned_with_rollback = rolled_back_ids.contains(&a.request_id);
            let queue_wait_slots = (a.current_slot - a.arrival_slot).max(0);
            let final_wait_slots = (a.scheduled_slot - a.arrival_slot).max(0);
            PerRequest {
                request_id: a.request_id,
                arrival_time: a.arrival_slot as f64 * slot_dur,
                arrival_slot: a.arrival_slot,
                deadline_slot: a.deadline_slot,
                included_in_batch_slot: a.current_slot,
                batch_sequence: batch_seq,
                scheduled_slot: a.scheduled_slot,
                queue_wait_slots,
                queue_wait_seconds: queue_wait_slots as f64 * slot_dur,
                final_wait_slots,
                final_wait_seconds: final_wait_slots as f64 * slot_dur,
                flavour_name: a.flavour_name.clone(),
                error: a.error,
                carbon_cost: a.carbon_cost,
                assignment_solver_mode: solver_mode,
                assignment_solver_status: solver_status,
                assigned_with_greedy_fallback: assigned_with_greedy,
                assigned_with_relaxed_retry: assigned_with_relaxed,
                assigned_with_injection,
                assigned_with_rollback,
                lateness_slots: (a.scheduled_slot - a.deadline_slot).max(0),
            }
        });
    }
    let mut v: Vec<PerRequest> = seen.into_values().collect();
    v.sort_by_key(|r| r.request_id);
    v
}

pub(super) fn compute_batch_timings(
    runs: &[SolverRunRow],
    batch_size_n: usize,
) -> Vec<BatchTiming> {
    let mut timings: Vec<BatchTiming> = runs
        .iter()
        .map(|r| BatchTiming {
            batch_sequence: r.run_sequence,
            batch_size_n,
            effective_batch_size: r.new_assignments,
            slot: r.current_slot,
            pending_before: r.pending_batch_size,
            solver_elapsed_ms: r.solver_elapsed_ms,
            scheduled: r.new_assignments > 0,
            flush_partial_batch: r.pending_batch_size < batch_size_n,
        })
        .collect();
    timings.sort_by_key(|t| t.batch_sequence);
    timings
}

pub(super) fn compute_per_timeslot(
    per_req: &[PerRequest],
    total_slots: i32,
    error_window_past: i32,
    error_window_future: i32,
) -> Vec<PerTimeslot> {
    let mut slot_errors: HashMap<i32, Vec<f64>> = HashMap::new();
    for pr in per_req {
        slot_errors
            .entry(pr.scheduled_slot)
            .or_default()
            .push(pr.error);
    }
    (0..total_slots)
        .map(|slot| {
            let window_start = slot - error_window_past;
            let window_end = slot + error_window_future;
            let errors: Vec<f64> = (window_start..=window_end)
                .filter_map(|s| slot_errors.get(&s))
                .flat_map(|v| v.iter().copied())
                .collect();
            let avg = if errors.is_empty() {
                0.0
            } else {
                errors.iter().sum::<f64>() / errors.len() as f64
            };
            PerTimeslot {
                timeslot: slot,
                window_start,
                window_end,
                real_request_count: errors.len() as f64,
                modeled_request_count: errors.len() as f64,
                window_avg_error_real: avg,
                window_avg_error_modeled: avg,
            }
        })
        .collect()
}

pub(super) fn min_max_avg(v: &[f64]) -> (f64, f64, f64) {
    if v.is_empty() {
        return (0.0, 0.0, 0.0);
    }
    let min = v.iter().cloned().fold(f64::INFINITY, f64::min);
    let max = v.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    let avg = v.iter().sum::<f64>() / v.len() as f64;
    (min, max, avg)
}

pub(super) fn compute_summary(
    mode: &str,
    batch_size: usize,
    realtime_slots: bool,
    realtime_speed_scale: f64,
    per_req: &[PerRequest],
    batch_timings: &[BatchTiming],
    total_received: usize,
    baseline_flavour_name: &str,
    baseline_flavour_duration: f64,
    baseline_flavour_error: f64,
    total_rollbacks: u64,
    max_consecutive_rollbacks: u64,
    requests_assigned_with_rollback: usize,
    peak_concurrent_workers: usize,
    avg_concurrent_workers: f64,
) -> RunSummary {
    let scheduled = per_req.len();
    let total_carbon: f64 = per_req.iter().map(|r| r.carbon_cost).sum();
    let avg_error = if scheduled > 0 {
        per_req.iter().map(|r| r.error).sum::<f64>() / scheduled as f64
    } else {
        0.0
    };

    let (st_min, st_max, st_avg) = min_max_avg(
        &batch_timings
            .iter()
            .map(|b| b.solver_elapsed_ms)
            .collect::<Vec<_>>(),
    );
    let (qw_min, qw_max, qw_avg) = min_max_avg(
        &per_req
            .iter()
            .map(|r| r.queue_wait_seconds)
            .collect::<Vec<_>>(),
    );
    let (fw_min, fw_max, fw_avg) = min_max_avg(
        &per_req
            .iter()
            .map(|r| r.final_wait_seconds)
            .collect::<Vec<_>>(),
    );
    let greedy = per_req
        .iter()
        .filter(|r| r.assigned_with_greedy_fallback)
        .count();
    let relaxed = per_req
        .iter()
        .filter(|r| r.assigned_with_relaxed_retry)
        .count();
    let requests_late = per_req.iter().filter(|r| r.lateness_slots > 0).count();
    let max_lateness_slots = per_req.iter().map(|r| r.lateness_slots).max().unwrap_or(0);

    RunSummary {
        execution_mode: mode.to_string(),
        batch_size,
        realtime_slots,
        realtime_speed_scale,
        baseline_flavour_name: baseline_flavour_name.to_string(),
        baseline_flavour_duration,
        baseline_flavour_error,
        requests_total: total_received,
        requests_scheduled: scheduled,
        requests_unscheduled: total_received.saturating_sub(scheduled),
        requests_late,
        max_lateness_slots,
        batches_executed: batch_timings.len(),
        total_carbon_cost: total_carbon,
        global_average_error: avg_error,
        global_average_error_real: avg_error,
        global_average_error_modeled: avg_error,
        global_average_error_real_skip_first_k: 0.0,
        global_average_error_modeled_skip_first_k: 0.0,
        requests_assigned_with_greedy_fallback: greedy,
        requests_assigned_with_relaxed_retry: relaxed,
        total_rollbacks,
        max_consecutive_rollbacks,
        requests_assigned_with_rollback,
        peak_concurrent_workers,
        avg_concurrent_workers,
        solver_time_ms_min: st_min,
        solver_time_ms_max: st_max,
        solver_time_ms_avg: st_avg,
        queue_wait_seconds_min: qw_min,
        queue_wait_seconds_max: qw_max,
        queue_wait_seconds_avg: qw_avg,
        final_wait_seconds_min: fw_min,
        final_wait_seconds_max: fw_max,
        final_wait_seconds_avg: fw_avg,
        baseline_total_carbon_cost: 0.0,
        carbon_cost_saving_vs_baseline: 0.0,
        carbon_cost_saving_vs_baseline_pct: 0.0,
        run_elapsed_seconds: 0.0,
    }
}

// ─── output helpers ───────────────────────────────────────────────────────────

pub(super) const SUMMARY_CSV_HEADER: &[&str] = &[
    "execution_mode",
    "batch_size",
    "realtime_slots",
    "realtime_speed_scale",
    "baseline_flavour_name",
    "baseline_flavour_duration_sec",
    "baseline_flavour_error",
    "requests_total",
    "requests_scheduled",
    "requests_unscheduled",
    "requests_late",
    "max_lateness_slots",
    "batches_executed",
    "carbon_cost",
    "global_average_error",
    "global_average_error_real",
    "global_average_error_modeled",
    "global_average_error_real_skip_first_k",
    "global_average_error_modeled_skip_first_k",
    "requests_assigned_with_greedy_fallback",
    "requests_assigned_with_relaxed_retry",
    "total_rollbacks",
    "peak_consecutive_rollbacks",
    "requests_assigned_with_rollback",
    "peak_concurrent_workers",
    "avg_concurrent_workers",
    "solver_time_ms_min",
    "solver_time_ms_max",
    "solver_time_ms_avg",
    "queue_wait_seconds_min",
    "queue_wait_seconds_max",
    "queue_wait_seconds_avg",
    "final_wait_seconds_min",
    "final_wait_seconds_max",
    "final_wait_seconds_avg",
    "baseline_carbon_cost",
    "carbon_cost_saving_vs_baseline",
    "carbon_saving",
    "run_elapsed_seconds",
];

pub(super) fn summary_to_json(s: &RunSummary) -> Value {
    serde_json::json!({
        "execution_mode":                          s.execution_mode,
        "batch_size":                              s.batch_size,
        "realtime_slots":                          s.realtime_slots,
        "realtime_speed_scale":                    s.realtime_speed_scale,
        "baseline_flavour_name":                   s.baseline_flavour_name,
        "baseline_flavour_duration_sec":        s.baseline_flavour_duration,
        "baseline_flavour_error":                  s.baseline_flavour_error,
        "requests_total":                          s.requests_total,
        "requests_scheduled":                      s.requests_scheduled,
        "requests_unscheduled":                    s.requests_unscheduled,
        "requests_late":                           s.requests_late,
        "max_lateness_slots":                      s.max_lateness_slots,
        "batches_executed":                        s.batches_executed,
        "carbon_cost":                             s.total_carbon_cost,
        "global_average_error":                    s.global_average_error,
        "global_average_error_real":               s.global_average_error_real,
        "global_average_error_modeled":            s.global_average_error_modeled,
        "global_average_error_real_skip_first_k":  s.global_average_error_real_skip_first_k,
        "global_average_error_modeled_skip_first_k": s.global_average_error_modeled_skip_first_k,
        "requests_assigned_with_greedy_fallback":  s.requests_assigned_with_greedy_fallback,
        "requests_assigned_with_relaxed_retry":    s.requests_assigned_with_relaxed_retry,
        "total_rollbacks":                         s.total_rollbacks,
        "peak_consecutive_rollbacks":              s.max_consecutive_rollbacks,
        "requests_assigned_with_rollback":         s.requests_assigned_with_rollback,
        "peak_concurrent_workers":                 s.peak_concurrent_workers,
        "avg_concurrent_workers":                  s.avg_concurrent_workers,
        "solver_time_ms_min":                      s.solver_time_ms_min,
        "solver_time_ms_max":                      s.solver_time_ms_max,
        "solver_time_ms_avg":                      s.solver_time_ms_avg,
        "queue_wait_seconds_min":                  s.queue_wait_seconds_min,
        "queue_wait_seconds_max":                  s.queue_wait_seconds_max,
        "queue_wait_seconds_avg":                  s.queue_wait_seconds_avg,
        "final_wait_seconds_min":                  s.final_wait_seconds_min,
        "final_wait_seconds_max":                  s.final_wait_seconds_max,
        "final_wait_seconds_avg":                  s.final_wait_seconds_avg,
        "baseline_carbon_cost":                    s.baseline_total_carbon_cost,
        "carbon_cost_saving_vs_baseline":          s.carbon_cost_saving_vs_baseline,
        "carbon_saving":                           s.carbon_cost_saving_vs_baseline_pct,
        "run_elapsed_seconds":                     s.run_elapsed_seconds,
    })
}

pub(super) fn summary_csv_row(s: &RunSummary) -> Vec<String> {
    vec![
        s.execution_mode.clone(),
        s.batch_size.to_string(),
        s.realtime_slots.to_string(),
        s.realtime_speed_scale.to_string(),
        s.baseline_flavour_name.clone(),
        s.baseline_flavour_duration.to_string(),
        s.baseline_flavour_error.to_string(),
        s.requests_total.to_string(),
        s.requests_scheduled.to_string(),
        s.requests_unscheduled.to_string(),
        s.requests_late.to_string(),
        s.max_lateness_slots.to_string(),
        s.batches_executed.to_string(),
        s.total_carbon_cost.to_string(),
        s.global_average_error.to_string(),
        s.global_average_error_real.to_string(),
        s.global_average_error_modeled.to_string(),
        s.global_average_error_real_skip_first_k.to_string(),
        s.global_average_error_modeled_skip_first_k.to_string(),
        s.requests_assigned_with_greedy_fallback.to_string(),
        s.requests_assigned_with_relaxed_retry.to_string(),
        s.total_rollbacks.to_string(),
        s.max_consecutive_rollbacks.to_string(),
        s.requests_assigned_with_rollback.to_string(),
        s.peak_concurrent_workers.to_string(),
        s.avg_concurrent_workers.to_string(),
        s.solver_time_ms_min.to_string(),
        s.solver_time_ms_max.to_string(),
        s.solver_time_ms_avg.to_string(),
        s.queue_wait_seconds_min.to_string(),
        s.queue_wait_seconds_max.to_string(),
        s.queue_wait_seconds_avg.to_string(),
        s.final_wait_seconds_min.to_string(),
        s.final_wait_seconds_max.to_string(),
        s.final_wait_seconds_avg.to_string(),
        s.baseline_total_carbon_cost.to_string(),
        s.carbon_cost_saving_vs_baseline.to_string(),
        s.carbon_cost_saving_vs_baseline_pct.to_string(),
        s.run_elapsed_seconds.to_string(),
    ]
}

pub(super) fn write_run_outputs(
    run_dir: &Path,
    summary: &RunSummary,
    per_req: &[PerRequest],
    batch_timings: &[BatchTiming],
    per_timeslot: &[PerTimeslot],
) {
    std::fs::create_dir_all(run_dir).expect("Cannot create run output dir");

    // summary.json + summary.csv
    std::fs::write(
        run_dir.join("summary.json"),
        serde_json::to_string_pretty(&summary_to_json(summary)).unwrap(),
    )
    .unwrap();
    {
        let mut w = csv::Writer::from_path(run_dir.join("summary.csv")).unwrap();
        w.write_record(SUMMARY_CSV_HEADER).unwrap();
        w.write_record(&summary_csv_row(summary)).unwrap();
        w.flush().unwrap();
    }

    // per_request.csv + per_request.json
    {
        let header = [
            "request_id",
            "arrival_time",
            "arrival_slot",
            "deadline_slot",
            "included_in_batch_slot",
            "batch_sequence",
            "scheduled_slot",
            "queue_wait_slots",
            "queue_wait_seconds",
            "final_wait_slots",
            "final_wait_seconds",
            "flavour_name",
            "error",
            "carbon_cost",
            "assignment_solver_mode",
            "assignment_solver_status",
            "assigned_with_greedy_fallback",
            "assigned_with_relaxed_retry",
            "assigned_with_injection",
            "assigned_with_rollback",
            "lateness_slots",
        ];
        let mut w = csv::Writer::from_path(run_dir.join("per_request.csv")).unwrap();
        w.write_record(header).unwrap();
        let mut jrows = Vec::new();
        for r in per_req {
            w.write_record(&[
                r.request_id.to_string(),
                r.arrival_time.to_string(),
                r.arrival_slot.to_string(),
                r.deadline_slot.to_string(),
                r.included_in_batch_slot.to_string(),
                r.batch_sequence.to_string(),
                r.scheduled_slot.to_string(),
                r.queue_wait_slots.to_string(),
                r.queue_wait_seconds.to_string(),
                r.final_wait_slots.to_string(),
                r.final_wait_seconds.to_string(),
                r.flavour_name.clone(),
                r.error.to_string(),
                r.carbon_cost.to_string(),
                r.assignment_solver_mode.clone(),
                r.assignment_solver_status.clone(),
                r.assigned_with_greedy_fallback.to_string(),
                r.assigned_with_relaxed_retry.to_string(),
                r.assigned_with_injection.to_string(),
                r.assigned_with_rollback.to_string(),
                r.lateness_slots.to_string(),
            ])
            .unwrap();
            jrows.push(serde_json::json!({
                "request_id":r.request_id,"arrival_time":r.arrival_time,
                "arrival_slot":r.arrival_slot,"deadline_slot":r.deadline_slot,
                "included_in_batch_slot":r.included_in_batch_slot,
                "batch_sequence":r.batch_sequence,"scheduled_slot":r.scheduled_slot,
                "queue_wait_slots":r.queue_wait_slots,
                "queue_wait_seconds":r.queue_wait_seconds,
                "final_wait_slots":r.final_wait_slots,
                "final_wait_seconds":r.final_wait_seconds,
                "flavour_name":r.flavour_name,"error":r.error,"carbon_cost":r.carbon_cost,
                "assignment_solver_mode":r.assignment_solver_mode.clone(),
                "assignment_solver_status":r.assignment_solver_status.clone(),
                "assigned_with_greedy_fallback":r.assigned_with_greedy_fallback,
                "assigned_with_relaxed_retry":r.assigned_with_relaxed_retry,
                "assigned_with_injection":r.assigned_with_injection,
                "assigned_with_rollback":r.assigned_with_rollback,
                "lateness_slots":r.lateness_slots,
            }));
        }
        w.flush().unwrap();
        std::fs::write(
            run_dir.join("per_request.json"),
            serde_json::to_string_pretty(&serde_json::json!({"rows":jrows})).unwrap(),
        )
        .unwrap();
    }

    // per_timeslot.csv + per_timeslot.json
    {
        let header = [
            "timeslot",
            "window_start",
            "window_end",
            "real_request_count",
            "modeled_request_count",
            "window_avg_error_real",
            "window_avg_error_modeled",
        ];
        let mut w = csv::Writer::from_path(run_dir.join("per_timeslot.csv")).unwrap();
        w.write_record(header).unwrap();
        let mut jrows = Vec::new();
        for t in per_timeslot {
            w.write_record(&[
                t.timeslot.to_string(),
                t.window_start.to_string(),
                t.window_end.to_string(),
                t.real_request_count.to_string(),
                t.modeled_request_count.to_string(),
                t.window_avg_error_real.to_string(),
                t.window_avg_error_modeled.to_string(),
            ])
            .unwrap();
            jrows.push(serde_json::json!({
                "timeslot":t.timeslot,"window_start":t.window_start,"window_end":t.window_end,
                "real_request_count":t.real_request_count,
                "modeled_request_count":t.modeled_request_count,
                "window_avg_error_real":t.window_avg_error_real,
                "window_avg_error_modeled":t.window_avg_error_modeled,
            }));
        }
        w.flush().unwrap();
        std::fs::write(
            run_dir.join("per_timeslot.json"),
            serde_json::to_string_pretty(&serde_json::json!({"rows":jrows})).unwrap(),
        )
        .unwrap();
    }

    // batch_timings.csv + batch_timings.json
    {
        let header = [
            "batch_sequence",
            "batch_size_n",
            "effective_batch_size",
            "slot",
            "pending_before",
            "solver_elapsed_ms",
            "scheduled",
            "flush_partial_batch",
        ];
        let mut w = csv::Writer::from_path(run_dir.join("batch_timings.csv")).unwrap();
        w.write_record(header).unwrap();
        let mut jrows = Vec::new();
        for b in batch_timings {
            w.write_record(&[
                b.batch_sequence.to_string(),
                b.batch_size_n.to_string(),
                b.effective_batch_size.to_string(),
                b.slot.to_string(),
                b.pending_before.to_string(),
                b.solver_elapsed_ms.to_string(),
                b.scheduled.to_string(),
                b.flush_partial_batch.to_string(),
            ])
            .unwrap();
            jrows.push(serde_json::json!({
                "batch_sequence":b.batch_sequence,"batch_size_n":b.batch_size_n,
                "effective_batch_size":b.effective_batch_size,"slot":b.slot,
                "pending_before":b.pending_before,"solver_elapsed_ms":b.solver_elapsed_ms,
                "scheduled":b.scheduled,"flush_partial_batch":b.flush_partial_batch,
            }));
        }
        w.flush().unwrap();
        std::fs::write(
            run_dir.join("batch_timings.json"),
            serde_json::to_string_pretty(&serde_json::json!({"rows":jrows})).unwrap(),
        )
        .unwrap();
    }

    // assignments_runtime.csv — Python-compatible format
    // Columns: request_id, scheduled_slot, flavour, carbon_cost, error, assignment_time
    {
        use std::time::{SystemTime, UNIX_EPOCH};
        let ts = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64();
        let mut w = csv::Writer::from_path(run_dir.join("assignments_runtime.csv")).unwrap();
        w.write_record([
            "request_id",
            "scheduled_slot",
            "flavour",
            "carbon_cost",
            "error",
            "assignment_time",
        ])
        .unwrap();
        for r in per_req {
            w.write_record(&[
                r.request_id.to_string(),
                r.scheduled_slot.to_string(),
                r.flavour_name.clone(),
                r.carbon_cost.to_string(),
                r.error.to_string(),
                ts.to_string(),
            ])
            .unwrap();
        }
        w.flush().unwrap();
    }
}

// ─── single-N run ─────────────────────────────────────────────────────────────
