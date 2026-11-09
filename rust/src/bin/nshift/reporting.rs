//! Scenario-run reporting, summaries, and Python-compatible output.

use super::*;

pub(super) fn write_output_schema_manifest(directory: &Path) {
    std::fs::create_dir_all(directory).expect("Cannot create schema output directory");
    let manifest = serde_json::json!({
        "schema_version": carbonshift_rs::metrics_logger::OUTPUT_SCHEMA_VERSION,
        "schema_name": "carbonshift.nshift",
    });
    std::fs::write(
        directory.join("output_schema.json"),
        serde_json::to_string_pretty(&manifest).expect("schema manifest must serialize"),
    )
    .expect("Cannot write output schema manifest");
}

// Python-compatible per_request row
pub(super) struct PerRequest {
    pub(super) request_id: u64,
    pub(super) qos_profile_id: String,
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
    pub(super) assignment_solver_status: String,
    pub(super) fallback_assigned: bool,
    pub(super) late_assigned: bool,
    /// Slots past deadline at scheduling time (0 = on time or early).
    pub(super) lateness_slots: i32,
}

/// Constant-size solver timing statistics used in run summaries.
#[derive(Debug, Clone, Copy, Default)]
pub(super) struct SolverTimingSummary {
    pub(super) count: usize,
    pub(super) min_ms: f64,
    pub(super) max_ms: f64,
    sum_ms: f64,
}

impl SolverTimingSummary {
    pub(super) fn zero_elapsed_samples(count: usize) -> Self {
        Self {
            count,
            ..Self::default()
        }
    }

    pub(super) fn from_scheduler_stats(
        count: usize,
        min_ms: f64,
        max_ms: f64,
        average_ms: f64,
    ) -> Self {
        if count == 0 {
            Self::default()
        } else {
            Self {
                count,
                min_ms,
                max_ms,
                sum_ms: average_ms * count as f64,
            }
        }
    }

    pub(super) fn record(&mut self, elapsed_ms: f64) {
        if self.count == 0 {
            self.min_ms = elapsed_ms;
            self.max_ms = elapsed_ms;
        } else {
            self.min_ms = self.min_ms.min(elapsed_ms);
            self.max_ms = self.max_ms.max(elapsed_ms);
        }
        self.sum_ms += elapsed_ms;
        self.count += 1;
    }

    pub(super) fn average_ms(self) -> f64 {
        if self.count == 0 {
            0.0
        } else {
            self.sum_ms / self.count as f64
        }
    }
}

pub(super) struct PerTimeslot {
    pub(super) timeslot: i32,
    pub(super) window_start: i32,
    pub(super) window_end: i32,
    pub(super) reqs_count: f64,
    pub(super) window_avg_error: f64,
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
    pub(super) reqs_total: usize,
    pub(super) reqs_scheduled: usize,
    pub(super) reqs_unscheduled: usize,
    pub(super) batches_executed: usize,
    pub(super) total_carbon_cost: f64,
    pub(super) global_average_error: f64,
    pub(super) reqs_fallback_assigned: usize,
    pub(super) peak_concurrent_workers: usize,
    pub(super) avg_concurrent_workers: f64,
    /// Requests scheduled past their deadline (late flush or post-run drain).
    pub(super) reqs_late: usize,
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

#[derive(Debug, Clone, Copy, Default)]
pub(super) struct SlotErrorAggregate {
    pub(super) error_sum: f64,
    pub(super) count: u64,
}

/// Bounded-size per-slot errors used after assignment rows have been evicted.
pub(super) fn compute_per_timeslot_from_aggregates(
    slot_errors: &[SlotErrorAggregate],
    error_window_past: i32,
    error_window_future: i32,
) -> Vec<PerTimeslot> {
    (0..slot_errors.len())
        .map(|slot| {
            let slot = slot as i32;
            let window_start = slot - error_window_past;
            let window_end = slot + error_window_future;
            let start = window_start.max(0) as usize;
            let end = window_end.min(slot_errors.len() as i32 - 1).max(-1);
            let mut error_sum = 0.0;
            let mut count = 0u64;
            if start <= end as usize && end >= 0 {
                for aggregate in &slot_errors[start..=end as usize] {
                    error_sum += aggregate.error_sum;
                    count += aggregate.count;
                }
            }
            PerTimeslot {
                timeslot: slot,
                window_start,
                window_end,
                reqs_count: count as f64,
                window_avg_error: if count > 0 {
                    error_sum / count as f64
                } else {
                    0.0
                },
            }
        })
        .collect()
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
                reqs_count: errors.len() as f64,
                window_avg_error: avg,
            }
        })
        .collect()
}

#[derive(Debug, Clone, Copy, Default)]
struct RangeAccumulator {
    count: usize,
    sum: f64,
    min: f64,
    max: f64,
}

impl RangeAccumulator {
    fn add(&mut self, value: f64) {
        if self.count == 0 {
            self.min = value;
            self.max = value;
        } else {
            self.min = self.min.min(value);
            self.max = self.max.max(value);
        }
        self.sum += value;
        self.count += 1;
    }

    fn min_max_avg(self) -> (f64, f64, f64) {
        if self.count == 0 {
            (0.0, 0.0, 0.0)
        } else {
            (self.min, self.max, self.sum / self.count as f64)
        }
    }
}

/// Incremental summary inputs for online nshift; one row is folded as soon as
/// its slot is finalized instead of retaining all PerRequest values.
#[derive(Debug, Clone, Copy, Default)]
pub(super) struct PerRequestSummaryAccumulator {
    pub(super) count: usize,
    total_carbon_cost: f64,
    total_error: f64,
    queue_wait_seconds: RangeAccumulator,
    final_wait_seconds: RangeAccumulator,
    fallback_count: usize,
    late_count: usize,
    max_lateness_slots: i32,
}

impl PerRequestSummaryAccumulator {
    pub(super) fn record(&mut self, request: &PerRequest) {
        self.count += 1;
        self.total_carbon_cost += request.carbon_cost;
        self.total_error += request.error;
        self.queue_wait_seconds.add(request.queue_wait_seconds);
        self.final_wait_seconds.add(request.final_wait_seconds);
        self.fallback_count += usize::from(request.fallback_assigned);
        self.late_count += usize::from(request.late_assigned);
        self.max_lateness_slots = self.max_lateness_slots.max(request.lateness_slots);
    }
}

pub(super) fn compute_summary(
    mode: &str,
    batch_size: usize,
    realtime_slots: bool,
    realtime_speed_scale: f64,
    per_req: &[PerRequest],
    solver_timings: SolverTimingSummary,
    total_received: usize,
    baseline_flavour_name: &str,
    baseline_flavour_duration: f64,
    baseline_flavour_error: f64,
    peak_concurrent_workers: usize,
    avg_concurrent_workers: f64,
) -> RunSummary {
    let mut request_summary = PerRequestSummaryAccumulator::default();
    for request in per_req {
        request_summary.record(request);
    }
    compute_summary_from_accumulators(
        mode,
        batch_size,
        realtime_slots,
        realtime_speed_scale,
        request_summary,
        solver_timings,
        total_received,
        baseline_flavour_name,
        baseline_flavour_duration,
        baseline_flavour_error,
        peak_concurrent_workers,
        avg_concurrent_workers,
    )
}

pub(super) fn compute_summary_from_accumulators(
    mode: &str,
    batch_size: usize,
    realtime_slots: bool,
    realtime_speed_scale: f64,
    request_summary: PerRequestSummaryAccumulator,
    solver_timings: SolverTimingSummary,
    total_received: usize,
    baseline_flavour_name: &str,
    baseline_flavour_duration: f64,
    baseline_flavour_error: f64,
    peak_concurrent_workers: usize,
    avg_concurrent_workers: f64,
) -> RunSummary {
    let scheduled = request_summary.count;
    let avg_error = if scheduled > 0 {
        request_summary.total_error / scheduled as f64
    } else {
        0.0
    };

    let st_min = if solver_timings.count > 0 {
        solver_timings.min_ms
    } else {
        0.0
    };
    let st_max = solver_timings.max_ms;
    let st_avg = solver_timings.average_ms();
    let (qw_min, qw_max, qw_avg) = request_summary.queue_wait_seconds.min_max_avg();
    let (fw_min, fw_max, fw_avg) = request_summary.final_wait_seconds.min_max_avg();

    RunSummary {
        execution_mode: mode.to_string(),
        batch_size,
        realtime_slots,
        realtime_speed_scale,
        baseline_flavour_name: baseline_flavour_name.to_string(),
        baseline_flavour_duration,
        baseline_flavour_error,
        reqs_total: total_received,
        reqs_scheduled: scheduled,
        reqs_unscheduled: total_received.saturating_sub(scheduled),
        reqs_late: request_summary.late_count,
        max_lateness_slots: request_summary.max_lateness_slots,
        batches_executed: solver_timings.count,
        total_carbon_cost: request_summary.total_carbon_cost,
        global_average_error: avg_error,
        reqs_fallback_assigned: request_summary.fallback_count,
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
    "reqs_total",
    "reqs_scheduled",
    "reqs_unscheduled",
    "reqs_late",
    "max_lateness_slots",
    "batches_executed",
    "carbon_cost",
    "global_average_error",
    "reqs_fallback_assigned",
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
        "reqs_total":                              s.reqs_total,
        "reqs_scheduled":                          s.reqs_scheduled,
        "reqs_unscheduled":                        s.reqs_unscheduled,
        "reqs_late":                               s.reqs_late,
        "max_lateness_slots":                      s.max_lateness_slots,
        "batches_executed":                        s.batches_executed,
        "carbon_cost":                             s.total_carbon_cost,
        "global_average_error":                    s.global_average_error,
        "reqs_fallback_assigned":                  s.reqs_fallback_assigned,
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
        s.reqs_total.to_string(),
        s.reqs_scheduled.to_string(),
        s.reqs_unscheduled.to_string(),
        s.reqs_late.to_string(),
        s.max_lateness_slots.to_string(),
        s.batches_executed.to_string(),
        s.total_carbon_cost.to_string(),
        s.global_average_error.to_string(),
        s.reqs_fallback_assigned.to_string(),
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
    per_timeslot: &[PerTimeslot],
) {
    write_run_outputs_inner(run_dir, summary, Some(per_req), per_timeslot);
}

/// Write metadata tables for a run whose per-request CSV/JSON were already
/// streamed by the settled-slot finalizer.
pub(super) fn write_streamed_run_outputs(
    run_dir: &Path,
    summary: &RunSummary,
    per_timeslot: &[PerTimeslot],
) {
    write_run_outputs_inner(run_dir, summary, None, per_timeslot);
}

fn write_run_outputs_inner(
    run_dir: &Path,
    summary: &RunSummary,
    per_req: Option<&[PerRequest]>,
    per_timeslot: &[PerTimeslot],
) {
    std::fs::create_dir_all(run_dir).expect("Cannot create run output dir");
    // Clear legacy files when a run directory is reused; current nshift never writes them.
    for obsolete_file in [
        "batch_timings.csv",
        "batch_timings.json",
        "_solver_runs.csv",
        "_solver_assignments.csv",
    ] {
        match std::fs::remove_file(run_dir.join(obsolete_file)) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => panic!("Cannot remove obsolete {obsolete_file}: {error}"),
        }
    }
    write_output_schema_manifest(run_dir);

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

    // Vector-based runs still use this compatibility writer. Online nshift
    // writes these files incrementally as slots settle and passes `None`.
    if let Some(per_req) = per_req {
        let header = [
            "request_id",
            "qos_profile_id",
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
            "carbon_cost",
            "assignment_solver_status",
            "fallback_assigned",
            "late_assigned",
        ];
        let mut w = csv::Writer::from_path(run_dir.join("per_request.csv")).unwrap();
        w.write_record(header).unwrap();
        let mut jrows = Vec::new();
        for r in per_req {
            w.write_record(&[
                r.request_id.to_string(),
                r.qos_profile_id.clone(),
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
                r.carbon_cost.to_string(),
                r.assignment_solver_status.clone(),
                r.fallback_assigned.to_string(),
                r.late_assigned.to_string(),
            ])
            .unwrap();
            jrows.push(serde_json::json!({
                "request_id":r.request_id,"qos_profile_id":r.qos_profile_id.clone(),
                "arrival_time":r.arrival_time,
                "arrival_slot":r.arrival_slot,"deadline_slot":r.deadline_slot,
                "included_in_batch_slot":r.included_in_batch_slot,
                "batch_sequence":r.batch_sequence,"scheduled_slot":r.scheduled_slot,
                "queue_wait_slots":r.queue_wait_slots,
                "queue_wait_seconds":r.queue_wait_seconds,
                "final_wait_slots":r.final_wait_slots,
                "final_wait_seconds":r.final_wait_seconds,
                "flavour_name":r.flavour_name,"carbon_cost":r.carbon_cost,
                "assignment_solver_status":r.assignment_solver_status.clone(),
                "fallback_assigned":r.fallback_assigned,
                "late_assigned":r.late_assigned,
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
            "reqs_count",
            "window_avg_error",
        ];
        let mut w = csv::Writer::from_path(run_dir.join("per_timeslot.csv")).unwrap();
        w.write_record(header).unwrap();
        let mut jrows = Vec::new();
        for t in per_timeslot {
            w.write_record(&[
                t.timeslot.to_string(),
                t.window_start.to_string(),
                t.window_end.to_string(),
                t.reqs_count.to_string(),
                t.window_avg_error.to_string(),
            ])
            .unwrap();
            jrows.push(serde_json::json!({
                "timeslot":t.timeslot,"window_start":t.window_start,"window_end":t.window_end,
                "reqs_count":t.reqs_count,
                "window_avg_error":t.window_avg_error,
            }));
        }
        w.flush().unwrap();
        std::fs::write(
            run_dir.join("per_timeslot.json"),
            serde_json::to_string_pretty(&serde_json::json!({"rows":jrows})).unwrap(),
        )
        .unwrap();
    }

    // assignments_runtime.csv — Python-compatible format
    // Columns: request_id, QoS profile, scheduled slot, flavour, cost, assignment time
    if let Some(per_req) = per_req {
        use std::time::{SystemTime, UNIX_EPOCH};
        let ts = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64();
        let mut w = csv::Writer::from_path(run_dir.join("assignments_runtime.csv")).unwrap();
        w.write_record([
            "request_id",
            "qos_profile_id",
            "scheduled_slot",
            "flavour",
            "carbon_cost",
            "assignment_time",
        ])
        .unwrap();
        for r in per_req {
            w.write_record(&[
                r.request_id.to_string(),
                r.qos_profile_id.clone(),
                r.scheduled_slot.to_string(),
                r.flavour_name.clone(),
                r.carbon_cost.to_string(),
                ts.to_string(),
            ])
            .unwrap();
        }
        w.flush().unwrap();
    }
}

// ─── single-N run ─────────────────────────────────────────────────────────────
