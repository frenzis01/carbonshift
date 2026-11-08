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

// ─── row types (post-processed metrics) ──────────────────────────────────────

#[derive(Clone)]
pub(super) struct SolverRunRow {
    pub(super) run_id: String,
    pub(super) run_sequence: u64,
    pub(super) new_assignments: usize,
    pub(super) solver_elapsed_ms: f64,
    #[cfg_attr(not(test), allow(dead_code))]
    pub(super) solver_status: String,
}

#[cfg(test)]
#[derive(Clone)]
pub(super) struct AssignmentRow {
    pub(super) run_id: String,
    pub(super) request_id: u64,
    pub(super) current_slot: i32,
    pub(super) qos_profile_id: String,
    pub(super) fallback_assigned: bool,
    pub(super) late_assigned: bool,
    pub(super) scheduled_slot: i32,
    pub(super) flavour_name: String,
    pub(super) carbon_cost: f64,
    pub(super) arrival_slot: i32,
    pub(super) deadline_slot: i32,
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
    let i_new = idx("new_assignments");
    let i_ms = idx("solver_elapsed_ms");
    let i_status = idx("solver_status");
    for (field, index) in [
        ("run_id", i_run_id),
        ("run_sequence", i_seq),
        ("new_assignments", i_new),
        ("solver_elapsed_ms", i_ms),
        ("solver_status", i_status),
    ] {
        assert!(
            index.is_some(),
            "solver-run CSV missing schema-v2 field {field}"
        );
    }

    let mut rows = Vec::new();
    for result in rdr.records() {
        let Ok(rec) = result else { continue };
        let g = |i: Option<usize>| i.and_then(|j| rec.get(j)).unwrap_or("").to_string();
        let gf = |i: Option<usize>| g(i).parse::<f64>().unwrap_or(0.0);
        let gi = |i: Option<usize>| g(i).parse::<i64>().unwrap_or(0);
        rows.push(SolverRunRow {
            run_id: g(i_run_id),
            run_sequence: gi(i_seq) as u64,
            new_assignments: gi(i_new) as usize,
            solver_elapsed_ms: gf(i_ms),
            solver_status: g(i_status),
        });
    }
    rows
}

#[cfg(test)]
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
    let i_profile = idx("qos_profile_id");
    let i_fallback = idx("fallback_assigned");
    let i_late = idx("late_assigned");
    let i_sched = idx("scheduled_slot");
    let i_flav = idx("flavour_name");
    let i_cost = idx("carbon_cost");
    let i_arr = idx("arrival_slot");
    let i_dead = idx("deadline_slot");
    for (field, index) in [
        ("run_id", i_run_id),
        ("current_slot", i_slot),
        ("request_id", i_rid),
        ("qos_profile_id", i_profile),
        ("fallback_assigned", i_fallback),
        ("late_assigned", i_late),
        ("scheduled_slot", i_sched),
        ("flavour_name", i_flav),
        ("carbon_cost", i_cost),
        ("arrival_slot", i_arr),
        ("deadline_slot", i_dead),
    ] {
        assert!(
            index.is_some(),
            "solver-assignment CSV missing schema-v2 field {field}"
        );
    }

    let mut rows = Vec::new();
    for result in rdr.records() {
        let Ok(rec) = result else { continue };
        let g = |i: Option<usize>| i.and_then(|j| rec.get(j)).unwrap_or("").to_string();
        let gi = |i: Option<usize>| g(i).parse::<i64>().unwrap_or(0);
        let is_true = |index: Option<usize>| {
            index.is_some_and(|index| {
                let value = g(Some(index)).to_lowercase();
                value == "true" || value == "1"
            })
        };
        rows.push(AssignmentRow {
            run_id: g(i_run_id),
            request_id: gi(i_rid) as u64,
            current_slot: gi(i_slot) as i32,
            qos_profile_id: g(i_profile),
            fallback_assigned: is_true(i_fallback),
            late_assigned: is_true(i_late),
            scheduled_slot: gi(i_sched) as i32,
            flavour_name: g(i_flav),
            carbon_cost: g(i_cost).parse::<f64>().unwrap_or(0.0),
            arrival_slot: gi(i_arr) as i32,
            deadline_slot: gi(i_dead) as i32,
        });
    }
    rows
}

// ─── metrics computation ──────────────────────────────────────────────────────

#[cfg(test)]
pub(super) fn compute_per_request(
    assignments: &[AssignmentRow],
    runs_by_id: &HashMap<String, &SolverRunRow>,
    flavour_errors: &HashMap<(String, String), f64>,
    slot_dur: f64,
) -> Vec<PerRequest> {
    let mut seen: HashMap<u64, PerRequest> = HashMap::new();
    for a in assignments {
        seen.entry(a.request_id).or_insert_with(|| {
            let run = runs_by_id.get(a.run_id.as_str());
            let batch_seq = run.map(|r| r.run_sequence).unwrap_or(0);
            let solver_status = run.map(|r| r.solver_status.clone()).unwrap_or_default();
            let queue_wait_slots = (a.current_slot - a.arrival_slot).max(0);
            let final_wait_slots = (a.scheduled_slot - a.arrival_slot).max(0);
            let error = *flavour_errors
                .get(&(a.qos_profile_id.clone(), a.flavour_name.clone()))
                .unwrap_or_else(|| {
                    panic!(
                        "QoS flavour catalog is missing profile '{}' flavour '{}'",
                        a.qos_profile_id, a.flavour_name
                    )
                });
            PerRequest {
                request_id: a.request_id,
                qos_profile_id: a.qos_profile_id.clone(),
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
                error,
                carbon_cost: a.carbon_cost,
                assignment_solver_status: solver_status,
                fallback_assigned: a.fallback_assigned,
                late_assigned: a.late_assigned,
                lateness_slots: (a.scheduled_slot - a.deadline_slot).max(0),
            }
        });
    }
    let mut v: Vec<PerRequest> = seen.into_values().collect();
    v.sort_by_key(|r| r.request_id);
    v
}

/// Compute batch timing rows while retaining only one slot per solver run,
/// rather than materializing every solver assignment row in memory.
pub(super) fn compute_batch_timings_from_csv(
    runs: &[SolverRunRow],
    assignments_path: &Path,
    batch_size_n: usize,
) -> Result<Vec<BatchTiming>, Box<dyn std::error::Error + Send + Sync>> {
    let mut reader = csv::Reader::from_path(assignments_path)?;
    let headers = reader.headers()?.clone();
    let run_id_column = headers
        .iter()
        .position(|header| header == "run_id")
        .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::InvalidData, "missing run_id"))?;
    let slot_column = headers
        .iter()
        .position(|header| header == "current_slot")
        .ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::InvalidData, "missing current_slot")
        })?;
    let mut slot_by_run = HashMap::new();
    for record in reader.records() {
        let record = record?;
        let run_id = record.get(run_id_column).ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::InvalidData, "missing row run_id")
        })?;
        let slot = record
            .get(slot_column)
            .ok_or_else(|| {
                std::io::Error::new(std::io::ErrorKind::InvalidData, "missing row slot")
            })?
            .parse::<i32>()
            .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
        slot_by_run.entry(run_id.to_owned()).or_insert(slot);
    }

    let mut timings = runs
        .iter()
        .map(|run| {
            let slot = slot_by_run.get(&run.run_id).copied().ok_or_else(|| {
                std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    format!("solver run {} has no assignment rows", run.run_id),
                )
            })?;
            Ok(BatchTiming {
                batch_sequence: run.run_sequence,
                batch_size_n,
                effective_batch_size: run.new_assignments,
                slot,
                pending_before: run.new_assignments,
                solver_elapsed_ms: run.solver_elapsed_ms,
                scheduled: run.new_assignments > 0,
                flush_partial_batch: run.new_assignments < batch_size_n,
            })
        })
        .collect::<Result<Vec<_>, std::io::Error>>()?;
    timings.sort_by_key(|timing| timing.batch_sequence);
    Ok(timings)
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
    peak_concurrent_workers: usize,
    avg_concurrent_workers: f64,
) -> RunSummary {
    let mut request_summary = PerRequestSummaryAccumulator::default();
    for request in per_req {
        request_summary.record(request);
    }
    compute_summary_from_accumulator(
        mode,
        batch_size,
        realtime_slots,
        realtime_speed_scale,
        request_summary,
        batch_timings,
        total_received,
        baseline_flavour_name,
        baseline_flavour_duration,
        baseline_flavour_error,
        peak_concurrent_workers,
        avg_concurrent_workers,
    )
}

pub(super) fn compute_summary_from_accumulator(
    mode: &str,
    batch_size: usize,
    realtime_slots: bool,
    realtime_speed_scale: f64,
    request_summary: PerRequestSummaryAccumulator,
    batch_timings: &[BatchTiming],
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

    let (st_min, st_max, st_avg) = min_max_avg(
        &batch_timings
            .iter()
            .map(|b| b.solver_elapsed_ms)
            .collect::<Vec<_>>(),
    );
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
        batches_executed: batch_timings.len(),
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
    batch_timings: &[BatchTiming],
    per_timeslot: &[PerTimeslot],
) {
    write_run_outputs_inner(run_dir, summary, Some(per_req), batch_timings, per_timeslot);
}

/// Write metadata tables for a run whose per-request CSV/JSON were already
/// streamed by the settled-slot finalizer.
pub(super) fn write_streamed_run_outputs(
    run_dir: &Path,
    summary: &RunSummary,
    batch_timings: &[BatchTiming],
    per_timeslot: &[PerTimeslot],
) {
    write_run_outputs_inner(run_dir, summary, None, batch_timings, per_timeslot);
}

fn write_run_outputs_inner(
    run_dir: &Path,
    summary: &RunSummary,
    per_req: Option<&[PerRequest]>,
    batch_timings: &[BatchTiming],
    per_timeslot: &[PerTimeslot],
) {
    std::fs::create_dir_all(run_dir).expect("Cannot create run output dir");
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
