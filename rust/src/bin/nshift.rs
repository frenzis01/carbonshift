//! Multi-N speed benchmark for the CarbonShift Rust scheduler.
//!
//! Runs the scheduler on a single scenario for several batch sizes (N) and
//! writes Python-compatible output so the existing Jupyter notebooks work
//! unchanged (just set `BACKEND = "rust"` in the first cell).
//!
//! # Build
//! ```text
//! cd rust/
//! cargo build --release --bin nshift
//! ```
//! The binary is then at `rust/target/release/nshift`.
//!
//! # Run
//! ```text
//! ./target/release/nshift [--config <path>] [--speed-scale <f>] [--realtime-slots] [--verbose]
//! ```
//! All flags are optional; the binary defaults to `config.json` in the
//! current working directory.
//!
//! | Flag                    | Default | Description                                      |
//! |-------------------------|---------|--------------------------------------------------|
//! | `--config <path>`       | `config.json` | Path to JSON config file             |
//! | `--speed-scale <f>`     | `1.0`   | Slot speed multiplier (only used with `--realtime-slots`) |
//! | `--realtime-slots`      | off     | Wait real slot durations (for live-like testing)          |
//! | `--verbose`             | off     | Print per-batch solver logs                               |
//!
//! # Config file format
//! ```json
//! {
//!   "batch_sizes":      [1, 4, 6, 8, 10],
//!   "scenario_path":    "scenario_seed_2030.json",
//!   "output_dir":       "output",
//!   "rust_output_dir":  "output_rust",
//!   "runner": {
//!     "flush_partial_batch":          true,
//!     "include_greedy_baseline":      true,
//!     "realtime_slots":               false,
//!     "realtime_speed_scale":         1.0,
//!     "dp_allow_relaxed_error_retry": true
//!   }
//! }
//! ```
//!
//! **Key runner fields:**
//! - `dp_allow_relaxed_error_retry` (default: `true`): when `false`, the scheduler skips the
//!   relaxed-window DP retry and falls back directly to greedy on infeasibility.
//!
//! **Key fields:**
//! - `batch_sizes`: list of N values to benchmark.
//! - `scenario_path`: path to the scenario JSON, resolved relative to the config file.
//! - `output_dir`: where Python writes its output (used by notebooks with `BACKEND="python"`).
//! - `rust_output_dir`: where THIS binary writes output (used by notebooks with `BACKEND="rust"`).
//!   Falls back to `output_dir` if absent.
//! - `realtime_slots`: set `false` (default) for fast simulation using the skip-empty-slots
//!   mechanism; set `true` to wait actual slot durations.
//! - `realtime_speed_scale`: slot-duration multiplier when `realtime_slots=true`
//!   (e.g. `0.1` runs 10× faster than real time).
//!
//! # Output layout  (mirrors Python's run_nshift_speed.py)
//! ```text
//! <rust_output_dir>/
//!   summary_by_n.json, summary_by_n.csv
//!   baseline_summary.json, baseline_summary.csv
//!   baseline_greedy/ { summary.{json,csv} per_request.{json,csv}
//!                      per_timeslot.{json,csv} batch_timings.{json,csv}
//!                      assignments_runtime.csv }
//!   N<n>/  { same files as above }
//! ```

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use carbonshift_rs::config::Config;
use carbonshift_rs::generator::RequestGenerator;
use carbonshift_rs::metrics_logger::MetricsLogger;
use carbonshift_rs::scenario::Scenario;
use carbonshift_rs::scheduler::BatchScheduler;
use carbonshift_rs::shared_state::SharedState;
use carbonshift_rs::types::CapacityTier;

use serde_json::Value;

// ─── benchmark config ─────────────────────────────────────────────────────────

struct BenchmarkConfig {
    batch_sizes: Vec<usize>,
    scenario_path: PathBuf,
    output_dir: PathBuf,
    realtime_slots: bool,
    realtime_speed_scale: f64,
    include_greedy_baseline: bool,
    /// When false, skip relaxed-window DP retry on infeasibility and go straight to greedy.
    dp_allow_relaxed_error_retry: bool,
    /// Max consecutive rollbacks before force-committing (0 = rollback disabled).
    rollback_max_consecutive: usize,
}

fn load_benchmark_config(config_path: &Path) -> BenchmarkConfig {
    let text = std::fs::read_to_string(config_path)
        .unwrap_or_else(|e| panic!("Cannot read config {}: {e}", config_path.display()));
    let v: Value = serde_json::from_str(&text)
        .unwrap_or_else(|e| panic!("Invalid JSON in config: {e}"));

    let batch_sizes: Vec<usize> = v["batch_sizes"]
        .as_array()
        .expect("batch_sizes must be an array")
        .iter()
        .map(|x| x.as_u64().expect("batch_sizes elements must be integers") as usize)
        .collect();

    let config_dir = config_path.parent().unwrap_or(Path::new("."));
    let scenario_path = config_dir.join(
        v["scenario_path"].as_str().expect("scenario_path must be a string"),
    );
    // Prefer rust_output_dir (so Python and Rust outputs don't overwrite each other).
    // Fall back to output_dir if rust_output_dir is absent.
    let out_key = if v.get("rust_output_dir").and_then(|x| x.as_str()).is_some() {
        "rust_output_dir"
    } else {
        "output_dir"
    };
    let output_dir = config_dir.join(
        v[out_key].as_str().expect("output_dir (or rust_output_dir) must be a string"),
    );

    let runner = &v["runner"];
    let realtime_slots =
        runner.get("realtime_slots").and_then(|x| x.as_bool()).unwrap_or(false);
    let realtime_speed_scale =
        runner.get("realtime_speed_scale").and_then(|x| x.as_f64()).unwrap_or(1.0);
    let include_greedy_baseline =
        runner.get("include_greedy_baseline").and_then(|x| x.as_bool()).unwrap_or(true);
    let dp_allow_relaxed_error_retry =
        runner.get("dp_allow_relaxed_error_retry").and_then(|x| x.as_bool()).unwrap_or(true);
    // TODO in run_battery this is set in a tmp config. ideally unify with the same param in config.rs
    let rollback_max_consecutive =
        runner.get("rollback_max_consecutive").and_then(|x| x.as_u64()).unwrap_or(3) as usize;

    BenchmarkConfig { batch_sizes, scenario_path, output_dir, realtime_slots, realtime_speed_scale, include_greedy_baseline, dp_allow_relaxed_error_retry, rollback_max_consecutive }
}

// ─── row types (post-processed metrics) ──────────────────────────────────────

#[derive(Clone)]
struct SolverRunRow {
    run_id: String,
    run_sequence: u64,
    current_slot: i32,
    pending_batch_size: usize,
    new_assignments: usize,
    solver_elapsed_ms: f64,
    solver_status: String,
    solver_mode: String,
}

#[derive(Clone)]
struct AssignmentRow {
    run_id: String,
    request_id: u64,
    current_slot: i32,
    is_new_assignment_in_run: bool,
    scheduled_slot: i32,
    flavour_name: String,
    error: f64,
    carbon_cost: f64,
    arrival_slot: i32,
    deadline_slot: i32,
}

// Python-compatible per_request row
struct PerRequest {
    request_id: u64,
    arrival_time: f64,
    arrival_slot: i32,
    deadline_slot: i32,
    included_in_batch_slot: i32,
    batch_sequence: u64,
    scheduled_slot: i32,
    queue_wait_slots: i32,
    queue_wait_seconds: f64,
    final_wait_slots: i32,
    final_wait_seconds: f64,
    flavour_name: String,
    error: f64,
    carbon_cost: f64,
    assignment_solver_mode: String,
    assignment_solver_status: String,
    assigned_with_greedy_fallback: bool,
    assigned_with_relaxed_retry: bool,
    assigned_with_rollback: bool,
}

struct BatchTiming {
    batch_sequence: u64,
    batch_size_n: usize,
    effective_batch_size: usize,
    slot: i32,
    pending_before: usize,
    solver_elapsed_ms: f64,
    scheduled: bool,
    flush_partial_batch: bool,
}

struct PerTimeslot {
    timeslot: i32,
    window_start: i32,
    window_end: i32,
    real_request_count: f64,
    modeled_request_count: f64,
    window_avg_error_real: f64,
    window_avg_error_modeled: f64,
}

// Python-compatible summary row (flatten_summary_for_csv equivalent)
#[derive(Clone)]
struct RunSummary {
    execution_mode: String,
    batch_size: usize,
    realtime_slots: bool,
    realtime_speed_scale: f64,
    baseline_flavour_name: String,
    baseline_flavour_duration: i32,
    baseline_flavour_error: f64,
    requests_total: usize,
    requests_scheduled: usize,
    requests_unscheduled: usize,
    batches_executed: usize,
    total_carbon_cost: f64,
    global_average_error: f64,
    global_average_error_real: f64,
    global_average_error_modeled: f64,
    global_average_error_real_skip_first_k: f64,
    global_average_error_modeled_skip_first_k: f64,
    requests_assigned_with_greedy_fallback: usize,
    requests_assigned_with_relaxed_retry: usize,
    total_rollbacks: u64,
    max_consecutive_rollbacks: u64,
    requests_assigned_with_rollback: usize,
    peak_concurrent_workers: usize,
    avg_concurrent_workers: f64,
    solver_time_ms_min: f64,
    solver_time_ms_max: f64,
    solver_time_ms_avg: f64,
    queue_wait_seconds_min: f64,
    queue_wait_seconds_max: f64,
    queue_wait_seconds_avg: f64,
    final_wait_seconds_min: f64,
    final_wait_seconds_max: f64,
    final_wait_seconds_avg: f64,
    baseline_total_carbon_cost: f64,
    carbon_cost_saving_vs_baseline: f64,
    carbon_cost_saving_vs_baseline_pct: f64,
}

// ─── CSV readers ──────────────────────────────────────────────────────────────

fn read_solver_runs(path: &Path) -> Vec<SolverRunRow> {
    let Ok(mut rdr) = csv::Reader::from_path(path) else { return Vec::new() };
    let headers: Vec<String> = match rdr.headers() {
        Ok(h) => h.iter().map(|s| s.to_string()).collect(),
        Err(_) => return Vec::new(),
    };
    let idx = |name: &str| headers.iter().position(|h| h == name);
    let i_run_id   = idx("run_id");
    let i_seq      = idx("run_sequence");
    let i_slot     = idx("current_slot");
    let i_pending  = idx("pending_batch_size");
    let i_new      = idx("new_assignments");
    let i_ms       = idx("solver_elapsed_ms");
    let i_status   = idx("solver_status");
    let i_mode     = idx("solver_mode");

    let mut rows = Vec::new();
    for result in rdr.records() {
        let Ok(rec) = result else { continue };
        let g  = |i: Option<usize>| i.and_then(|j| rec.get(j)).unwrap_or("").to_string();
        let gf = |i: Option<usize>| g(i).parse::<f64>().unwrap_or(0.0);
        let gi = |i: Option<usize>| g(i).parse::<i64>().unwrap_or(0);
        rows.push(SolverRunRow {
            run_id:             g(i_run_id),
            run_sequence:       gi(i_seq) as u64,
            current_slot:       gi(i_slot) as i32,
            pending_batch_size: gi(i_pending) as usize,
            new_assignments:    gi(i_new) as usize,
            solver_elapsed_ms:  gf(i_ms),
            solver_status:      g(i_status),
            solver_mode:        g(i_mode),
        });
    }
    rows
}

fn read_solver_assignments(path: &Path) -> Vec<AssignmentRow> {
    let Ok(mut rdr) = csv::Reader::from_path(path) else { return Vec::new() };
    let headers: Vec<String> = match rdr.headers() {
        Ok(h) => h.iter().map(|s| s.to_string()).collect(),
        Err(_) => return Vec::new(),
    };
    let idx = |name: &str| headers.iter().position(|h| h == name);
    let i_run_id = idx("run_id");
    let i_rid    = idx("request_id");
    let i_slot   = idx("current_slot");
    let i_is_new = idx("is_new_assignment_in_run");
    let i_sched  = idx("scheduled_slot");
    let i_flav   = idx("flavour_name");
    let i_err    = idx("error");
    let i_cost   = idx("carbon_cost");
    let i_arr    = idx("arrival_slot");
    let i_dead   = idx("deadline_slot");

    let mut rows = Vec::new();
    for result in rdr.records() {
        let Ok(rec) = result else { continue };
        let g  = |i: Option<usize>| i.and_then(|j| rec.get(j)).unwrap_or("").to_string();
        let gf = |i: Option<usize>| g(i).parse::<f64>().unwrap_or(0.0);
        let gi = |i: Option<usize>| g(i).parse::<i64>().unwrap_or(0);
        let is_new_str = g(i_is_new).to_lowercase();
        let is_new = is_new_str == "true" || is_new_str == "1";
        rows.push(AssignmentRow {
            run_id:                  g(i_run_id),
            request_id:              gi(i_rid) as u64,
            current_slot:            gi(i_slot) as i32,
            is_new_assignment_in_run: is_new,
            scheduled_slot:          gi(i_sched) as i32,
            flavour_name:            g(i_flav),
            error:                   gf(i_err),
            carbon_cost:             gf(i_cost),
            arrival_slot:            gi(i_arr) as i32,
            deadline_slot:           gi(i_dead) as i32,
        });
    }
    rows
}

// ─── metrics computation ──────────────────────────────────────────────────────

fn compute_per_request(
    assignments: &[AssignmentRow],
    runs_by_id: &HashMap<String, &SolverRunRow>,
    slot_dur: f64,
    rolled_back_ids: &HashSet<u64>,
) -> Vec<PerRequest> {
    let mut seen: HashMap<u64, PerRequest> = HashMap::new();
    for a in assignments.iter().filter(|a| a.is_new_assignment_in_run) {
        seen.entry(a.request_id).or_insert_with(|| {
            let run = runs_by_id.get(a.run_id.as_str());
            let batch_seq             = run.map(|r| r.run_sequence).unwrap_or(0);
            let solver_status         = run.map(|r| r.solver_status.clone()).unwrap_or_default();
            let solver_mode           = run.map(|r| r.solver_mode.clone()).unwrap_or_default();
            let assigned_with_relaxed  = solver_mode.contains("relaxed");
            let assigned_with_greedy   = solver_mode.contains("greedy_after_infeasible");
            let assigned_with_rollback = rolled_back_ids.contains(&a.request_id);
            let queue_wait_slots       = (a.current_slot  - a.arrival_slot).max(0);
            let final_wait_slots      = (a.scheduled_slot - a.arrival_slot).max(0);
            PerRequest {
                request_id:               a.request_id,
                arrival_time:             a.arrival_slot as f64 * slot_dur,
                arrival_slot:             a.arrival_slot,
                deadline_slot:            a.deadline_slot,
                included_in_batch_slot:   a.current_slot,
                batch_sequence:           batch_seq,
                scheduled_slot:           a.scheduled_slot,
                queue_wait_slots,
                queue_wait_seconds:       queue_wait_slots as f64 * slot_dur,
                final_wait_slots,
                final_wait_seconds:       final_wait_slots as f64 * slot_dur,
                flavour_name:             a.flavour_name.clone(),
                error:                    a.error,
                carbon_cost:              a.carbon_cost,
                assignment_solver_mode:   solver_mode,
                assignment_solver_status: solver_status,
                assigned_with_greedy_fallback: assigned_with_greedy,
                assigned_with_relaxed_retry:   assigned_with_relaxed,
                assigned_with_rollback,
            }
        });
    }
    let mut v: Vec<PerRequest> = seen.into_values().collect();
    v.sort_by_key(|r| r.request_id);
    v
}

fn compute_batch_timings(runs: &[SolverRunRow], batch_size_n: usize) -> Vec<BatchTiming> {
    let mut timings: Vec<BatchTiming> = runs.iter().map(|r| BatchTiming {
        batch_sequence:    r.run_sequence,
        batch_size_n,
        effective_batch_size: r.new_assignments,
        slot:              r.current_slot,
        pending_before:    r.pending_batch_size,
        solver_elapsed_ms: r.solver_elapsed_ms,
        scheduled:         r.new_assignments > 0,
        flush_partial_batch: r.pending_batch_size < batch_size_n,
    }).collect();
    timings.sort_by_key(|t| t.batch_sequence);
    timings
}

fn compute_per_timeslot(
    per_req: &[PerRequest],
    total_slots: i32,
    error_window_past: i32,
    error_window_future: i32,
) -> Vec<PerTimeslot> {
    let mut slot_errors: HashMap<i32, Vec<f64>> = HashMap::new();
    for pr in per_req {
        slot_errors.entry(pr.scheduled_slot).or_default().push(pr.error);
    }
    (0..total_slots).map(|slot| {
        let window_start = slot - error_window_past;
        let window_end   = slot + error_window_future;
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
            real_request_count:      errors.len() as f64,
            modeled_request_count:   errors.len() as f64,
            window_avg_error_real:   avg,
            window_avg_error_modeled: avg,
        }
    }).collect()
}

fn min_max_avg(v: &[f64]) -> (f64, f64, f64) {
    if v.is_empty() { return (0.0, 0.0, 0.0); }
    let min = v.iter().cloned().fold(f64::INFINITY,      f64::min);
    let max = v.iter().cloned().fold(f64::NEG_INFINITY,  f64::max);
    let avg = v.iter().sum::<f64>() / v.len() as f64;
    (min, max, avg)
}

fn compute_summary(
    mode: &str,
    batch_size: usize,
    realtime_slots: bool,
    realtime_speed_scale: f64,
    per_req: &[PerRequest],
    batch_timings: &[BatchTiming],
    total_received: usize,
    baseline_flavour_name: &str,
    baseline_flavour_duration: i32,
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
    } else { 0.0 };

    let (st_min, st_max, st_avg) = min_max_avg(
        &batch_timings.iter().map(|b| b.solver_elapsed_ms).collect::<Vec<_>>(),
    );
    let (qw_min, qw_max, qw_avg) = min_max_avg(
        &per_req.iter().map(|r| r.queue_wait_seconds).collect::<Vec<_>>(),
    );
    let (fw_min, fw_max, fw_avg) = min_max_avg(
        &per_req.iter().map(|r| r.final_wait_seconds).collect::<Vec<_>>(),
    );
    let relaxed = per_req.iter().filter(|r| r.assigned_with_relaxed_retry).count();

    RunSummary {
        execution_mode:          mode.to_string(),
        batch_size,
        realtime_slots,
        realtime_speed_scale,
        baseline_flavour_name:   baseline_flavour_name.to_string(),
        baseline_flavour_duration,
        baseline_flavour_error,
        requests_total:          total_received,
        requests_scheduled:      scheduled,
        requests_unscheduled:    total_received - scheduled,
        batches_executed:        batch_timings.len(),
        total_carbon_cost:       total_carbon,
        global_average_error:    avg_error,
        global_average_error_real:              avg_error,
        global_average_error_modeled:           avg_error,
        global_average_error_real_skip_first_k: 0.0,
        global_average_error_modeled_skip_first_k: 0.0,
        requests_assigned_with_greedy_fallback: 0,
        requests_assigned_with_relaxed_retry:   relaxed,
        total_rollbacks,
        max_consecutive_rollbacks,
        requests_assigned_with_rollback,
        peak_concurrent_workers,
        avg_concurrent_workers,
        solver_time_ms_min:      st_min,
        solver_time_ms_max:      st_max,
        solver_time_ms_avg:      st_avg,
        queue_wait_seconds_min:  qw_min,
        queue_wait_seconds_max:  qw_max,
        queue_wait_seconds_avg:  qw_avg,
        final_wait_seconds_min:  fw_min,
        final_wait_seconds_max:  fw_max,
        final_wait_seconds_avg:  fw_avg,
        baseline_total_carbon_cost:          0.0,
        carbon_cost_saving_vs_baseline:      0.0,
        carbon_cost_saving_vs_baseline_pct:  0.0,
    }
}

// ─── greedy baseline ──────────────────────────────────────────────────────────

fn get_capacity_multiplier(tiers: &[CapacityTier], count: i64) -> f64 {
    for tier in tiers {
        match tier.max_requests {
            None => return tier.multiplier,          // overflow tier
            Some(max) if count <= max => return tier.multiplier,
            _ => {}
        }
    }
    tiers.last().map(|t| t.multiplier).unwrap_or(1.0)
}

/// Greedy baseline: assign each request immediately on arrival to the "best"
/// flavour (lowest error; mirrors Python's `run_greedy_baseline`).
fn run_greedy_baseline(
    scenario: &Scenario,
    cfg: &Config,
    realtime_slots: bool,
    realtime_speed_scale: f64,
) -> (Vec<PerRequest>, Vec<BatchTiming>, RunSummary) {
    let flav = cfg.flavours.iter().min_by(|a, b| a.error.partial_cmp(&b.error).unwrap())
        .expect("Config must have at least one flavour");
    let slot_dur = cfg.slot_duration_seconds;
    let carbon   = &scenario.carbon_forecast;
    let by_slot  = scenario.requests_by_slot();
    let tiers    = &cfg.capacity_tiers;
    let scale    = cfg.carbon_cost_duration_scale;

    let mut per_req: Vec<PerRequest> = Vec::new();
    let mut batch_timings: Vec<BatchTiming> = Vec::new();
    let mut seq: u64 = 0;

    for (slot, requests) in by_slot.iter().enumerate() {
        if requests.is_empty() { continue; }
        let ci = carbon.get(slot).copied().unwrap_or(1.0);
        let mut before_count: i64 = 0;
        let mut before_dur: i64 = 0;
        for req in requests {
            seq += 1;
            let after_count = before_count + 1;
            let after_dur = before_dur + flav.duration as i64;
            let before_mult = get_capacity_multiplier(tiers, before_count);
            let after_mult = get_capacity_multiplier(tiers, after_count);
            let cost = (ci * after_mult * after_dur as f64 - ci * before_mult * before_dur as f64) * scale;
            before_count = after_count;
            before_dur = after_dur;
            per_req.push(PerRequest {
                request_id:             req.id,
                arrival_time:           req.arrival_slot as f64 * slot_dur,
                arrival_slot:           req.arrival_slot,
                deadline_slot:          req.deadline_slot,
                included_in_batch_slot: slot as i32,
                batch_sequence:         seq,
                scheduled_slot:         slot as i32,
                queue_wait_slots:       0,
                queue_wait_seconds:     0.0,
                final_wait_slots:       0,
                final_wait_seconds:     0.0,
                flavour_name:           flav.name.clone(),
                error:                  flav.error,
                carbon_cost:            cost,
                assignment_solver_mode:   "greedy".to_string(),
                assignment_solver_status: "ok".to_string(),
                assigned_with_greedy_fallback: false,
                assigned_with_relaxed_retry:   false,
                assigned_with_rollback:        false,
            });
            batch_timings.push(BatchTiming {
                batch_sequence:      seq,
                batch_size_n:        0,
                effective_batch_size: 1,
                slot:                slot as i32,
                pending_before:      1,
                solver_elapsed_ms:   0.0,
                scheduled:           true,
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

// ─── output helpers ───────────────────────────────────────────────────────────

const SUMMARY_CSV_HEADER: &[&str] = &[
    "execution_mode", "batch_size", "realtime_slots", "realtime_speed_scale",
    "baseline_flavour_name", "baseline_flavour_duration", "baseline_flavour_error",
    "requests_total", "requests_scheduled", "requests_unscheduled", "batches_executed",
    "total_carbon_cost", "global_average_error", "global_average_error_real",
    "global_average_error_modeled", "global_average_error_real_skip_first_k",
    "global_average_error_modeled_skip_first_k",
    "requests_assigned_with_greedy_fallback", "requests_assigned_with_relaxed_retry",
    "total_rollbacks", "max_consecutive_rollbacks", "requests_assigned_with_rollback",
    "peak_concurrent_workers", "avg_concurrent_workers",
    "solver_time_ms_min", "solver_time_ms_max", "solver_time_ms_avg",
    "queue_wait_seconds_min", "queue_wait_seconds_max", "queue_wait_seconds_avg",
    "final_wait_seconds_min", "final_wait_seconds_max", "final_wait_seconds_avg",
    "baseline_total_carbon_cost", "carbon_cost_saving_vs_baseline",
    "carbon_cost_saving_vs_baseline_pct",
];

fn summary_to_json(s: &RunSummary) -> Value {
    serde_json::json!({
        "execution_mode":                          s.execution_mode,
        "batch_size":                              s.batch_size,
        "realtime_slots":                          s.realtime_slots,
        "realtime_speed_scale":                    s.realtime_speed_scale,
        "baseline_flavour_name":                   s.baseline_flavour_name,
        "baseline_flavour_duration":               s.baseline_flavour_duration,
        "baseline_flavour_error":                  s.baseline_flavour_error,
        "requests_total":                          s.requests_total,
        "requests_scheduled":                      s.requests_scheduled,
        "requests_unscheduled":                    s.requests_unscheduled,
        "batches_executed":                        s.batches_executed,
        "total_carbon_cost":                       s.total_carbon_cost,
        "global_average_error":                    s.global_average_error,
        "global_average_error_real":               s.global_average_error_real,
        "global_average_error_modeled":            s.global_average_error_modeled,
        "global_average_error_real_skip_first_k":  s.global_average_error_real_skip_first_k,
        "global_average_error_modeled_skip_first_k": s.global_average_error_modeled_skip_first_k,
        "requests_assigned_with_greedy_fallback":  s.requests_assigned_with_greedy_fallback,
        "requests_assigned_with_relaxed_retry":    s.requests_assigned_with_relaxed_retry,
        "total_rollbacks":                         s.total_rollbacks,
        "max_consecutive_rollbacks":               s.max_consecutive_rollbacks,
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
        "baseline_total_carbon_cost":              s.baseline_total_carbon_cost,
        "carbon_cost_saving_vs_baseline":          s.carbon_cost_saving_vs_baseline,
        "carbon_cost_saving_vs_baseline_pct":      s.carbon_cost_saving_vs_baseline_pct,
    })
}

fn summary_csv_row(s: &RunSummary) -> Vec<String> {
    vec![
        s.execution_mode.clone(),       s.batch_size.to_string(),
        s.realtime_slots.to_string(),   s.realtime_speed_scale.to_string(),
        s.baseline_flavour_name.clone(),s.baseline_flavour_duration.to_string(),
        s.baseline_flavour_error.to_string(),
        s.requests_total.to_string(),   s.requests_scheduled.to_string(),
        s.requests_unscheduled.to_string(), s.batches_executed.to_string(),
        s.total_carbon_cost.to_string(),s.global_average_error.to_string(),
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
        s.solver_time_ms_min.to_string(), s.solver_time_ms_max.to_string(),
        s.solver_time_ms_avg.to_string(),
        s.queue_wait_seconds_min.to_string(), s.queue_wait_seconds_max.to_string(),
        s.queue_wait_seconds_avg.to_string(),
        s.final_wait_seconds_min.to_string(), s.final_wait_seconds_max.to_string(),
        s.final_wait_seconds_avg.to_string(),
        s.baseline_total_carbon_cost.to_string(),
        s.carbon_cost_saving_vs_baseline.to_string(),
        s.carbon_cost_saving_vs_baseline_pct.to_string(),
    ]
}

fn write_run_outputs(
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
    ).unwrap();
    {
        let mut w = csv::Writer::from_path(run_dir.join("summary.csv")).unwrap();
        w.write_record(SUMMARY_CSV_HEADER).unwrap();
        w.write_record(&summary_csv_row(summary)).unwrap();
        w.flush().unwrap();
    }

    // per_request.csv + per_request.json
    {
        let header = [
            "request_id","arrival_time","arrival_slot","deadline_slot",
            "included_in_batch_slot","batch_sequence","scheduled_slot",
            "queue_wait_slots","queue_wait_seconds","final_wait_slots","final_wait_seconds",
            "flavour_name","error","carbon_cost",
            "assignment_solver_mode","assignment_solver_status",
            "assigned_with_greedy_fallback","assigned_with_relaxed_retry","assigned_with_rollback",
        ];
        let mut w = csv::Writer::from_path(run_dir.join("per_request.csv")).unwrap();
        w.write_record(header).unwrap();
        let mut jrows = Vec::new();
        for r in per_req {
            w.write_record(&[
                r.request_id.to_string(),    r.arrival_time.to_string(),
                r.arrival_slot.to_string(),  r.deadline_slot.to_string(),
                r.included_in_batch_slot.to_string(), r.batch_sequence.to_string(),
                r.scheduled_slot.to_string(),r.queue_wait_slots.to_string(),
                r.queue_wait_seconds.to_string(), r.final_wait_slots.to_string(),
                r.final_wait_seconds.to_string(), r.flavour_name.clone(),
                r.error.to_string(),         r.carbon_cost.to_string(),
                r.assignment_solver_mode.clone(), r.assignment_solver_status.clone(),
                r.assigned_with_greedy_fallback.to_string(),
                r.assigned_with_relaxed_retry.to_string(),
                r.assigned_with_rollback.to_string(),
            ]).unwrap();
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
                "assigned_with_rollback":r.assigned_with_rollback,
            }));
        }
        w.flush().unwrap();
        std::fs::write(
            run_dir.join("per_request.json"),
            serde_json::to_string_pretty(&serde_json::json!({"rows":jrows})).unwrap(),
        ).unwrap();
    }

    // per_timeslot.csv + per_timeslot.json
    {
        let header = [
            "timeslot","window_start","window_end",
            "real_request_count","modeled_request_count",
            "window_avg_error_real","window_avg_error_modeled",
        ];
        let mut w = csv::Writer::from_path(run_dir.join("per_timeslot.csv")).unwrap();
        w.write_record(header).unwrap();
        let mut jrows = Vec::new();
        for t in per_timeslot {
            w.write_record(&[
                t.timeslot.to_string(),      t.window_start.to_string(),
                t.window_end.to_string(),    t.real_request_count.to_string(),
                t.modeled_request_count.to_string(),
                t.window_avg_error_real.to_string(),
                t.window_avg_error_modeled.to_string(),
            ]).unwrap();
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
        ).unwrap();
    }

    // batch_timings.csv + batch_timings.json
    {
        let header = [
            "batch_sequence","batch_size_n","effective_batch_size","slot",
            "pending_before","solver_elapsed_ms","scheduled","flush_partial_batch",
        ];
        let mut w = csv::Writer::from_path(run_dir.join("batch_timings.csv")).unwrap();
        w.write_record(header).unwrap();
        let mut jrows = Vec::new();
        for b in batch_timings {
            w.write_record(&[
                b.batch_sequence.to_string(), b.batch_size_n.to_string(),
                b.effective_batch_size.to_string(), b.slot.to_string(),
                b.pending_before.to_string(),  b.solver_elapsed_ms.to_string(),
                b.scheduled.to_string(),       b.flush_partial_batch.to_string(),
            ]).unwrap();
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
        ).unwrap();
    }

    // assignments_runtime.csv — Python-compatible format
    // Columns: request_id, scheduled_slot, flavour, carbon_cost, error, assignment_time
    {
        use std::time::{SystemTime, UNIX_EPOCH};
        let ts = SystemTime::now()
            .duration_since(UNIX_EPOCH).unwrap_or_default().as_secs_f64();
        let mut w = csv::Writer::from_path(run_dir.join("assignments_runtime.csv")).unwrap();
        w.write_record(["request_id","scheduled_slot","flavour","carbon_cost","error","assignment_time"]).unwrap();
        for r in per_req {
            w.write_record(&[
                r.request_id.to_string(), r.scheduled_slot.to_string(),
                r.flavour_name.clone(),   r.carbon_cost.to_string(),
                r.error.to_string(),      ts.to_string(),
            ]).unwrap();
        }
        w.flush().unwrap();
    }
}

// ─── single-N run ─────────────────────────────────────────────────────────────

fn run_single_n(
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
    cfg.batch_size = batch_size;
    cfg.verbose    = verbose;
    // For fast simulation: use skip_empty_slots=true with slot_speed_scale=1.0.
    // The skip mechanism advances the virtual clock when a slot is empty, so the
    // scheduler races through the scenario without waiting for real time.
    // Do NOT set slot_speed_scale=0 here: with 0, the wall clock advances faster
    // than 1 virtual-slot/ms, causing the monitor to exit before all requests are
    // scheduled (the scheduler can't keep up with a 1ms/slot clock).
    if !realtime_slots {
        cfg.skip_empty_slots = true;
        cfg.slot_speed_scale = 1.0;
    } else {
        cfg.skip_empty_slots = false;
        cfg.slot_speed_scale = realtime_speed_scale;
    }
    cfg.enable_solver_logging = true;
    let tmp_runs        = run_dir.join("_solver_runs.csv");
    let tmp_assignments = run_dir.join("_solver_assignments.csv");
    let tmp_slot_mets   = run_dir.join("_solver_slot_metrics.csv");
    cfg.solver_runs_file            = tmp_runs.to_str().unwrap().to_string();
    cfg.solver_assignments_file     = tmp_assignments.to_str().unwrap().to_string();
    cfg.solver_slot_metrics_file    = tmp_slot_mets.to_str().unwrap().to_string();
    cfg.enable_infeasibility_debug_logging = false;

    let eff_dur     = cfg.effective_slot_duration_secs();
    let total_dur   = cfg.total_slots as f64 * eff_dur;
    let cfg         = Arc::new(cfg);

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
    let mut generator = RequestGenerator::new_from_scenario(shared_state.clone(), cfg.clone(), by_slot);
    let mut sched = BatchScheduler::new(
        shared_state.clone(),
        cfg.clone(),
        ml,
        Some(scenario.carbon_forecast.clone()),
    );

    sched.start();
    generator.start();

    // Poll until all slots have been processed.
    loop {
        std::thread::sleep(std::time::Duration::from_millis(50));
        if shared_state.virtual_elapsed_secs() >= total_dur {
            break;
        }
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

    let per_req      = compute_per_request(&assignment_rows, &runs_by_id, base_cfg.slot_duration_seconds, &rolled_back_ids);
    let batch_timings = compute_batch_timings(&runs, batch_size);
    let summary = compute_summary(
        "nshift_dp",
        batch_size,
        realtime_slots,
        realtime_speed_scale,
        &per_req,
        &batch_timings,
        total_received,
        "",
        0,
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

    (per_req, batch_timings, summary)
}

// ─── main ─────────────────────────────────────────────────────────────────────

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mut config_path: Option<PathBuf>    = None;
    let mut realtime_override: Option<bool> = None;
    let mut speed_scale_override: Option<f64> = None;
    let mut verbose = false;

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--config" => {
                i += 1;
                config_path = Some(PathBuf::from(
                    args.get(i).expect("--config requires a path"),
                ));
            }
            "--realtime-slots"    => { realtime_override   = Some(true);  }
            "--no-realtime-slots" => { realtime_override   = Some(false); }
            "--speed-scale"       => {
                i += 1;
                let v: f64 = args.get(i).and_then(|s| s.parse().ok())
                    .expect("--speed-scale requires a numeric value in [0, 1]");
                assert!((0.0..=1.0).contains(&v), "--speed-scale must be in [0.0, 1.0]");
                speed_scale_override = Some(v);
            }
            "--verbose" => { verbose = true; }
            _ => {}
        }
        i += 1;
    }

    let config_path = config_path.unwrap_or_else(|| PathBuf::from("config.json"));
    let bcfg = load_benchmark_config(&config_path);
    let realtime_slots  = realtime_override.unwrap_or(bcfg.realtime_slots);
    let speed_scale     = speed_scale_override.unwrap_or(bcfg.realtime_speed_scale);

    let scenario = Scenario::from_file(bcfg.scenario_path.to_str().unwrap())
        .unwrap_or_else(|e| panic!("Cannot load scenario '{}': {e}", bcfg.scenario_path.display()));

    // Build a base config that has all scenario parameters applied.
    let mut base_cfg = Config::default();
    base_cfg.apply_scenario_metadata(&scenario.metadata);
    base_cfg.dp_allow_relaxed_error_retry = bcfg.dp_allow_relaxed_error_retry;
    base_cfg.rollback_max_consecutive     = bcfg.rollback_max_consecutive;

    println!(
        "Loaded scenario: {} slots, {} requests ({})",
        scenario.metadata.total_slots,
        scenario.requests.len(),
        bcfg.scenario_path.display(),
    );
    println!(
        "Config: batch_sizes={:?}, realtime_slots={}, speed_scale={:.2}, rollback_max_consecutive={}, output={}",
        bcfg.batch_sizes,
        realtime_slots,
        speed_scale,
        bcfg.rollback_max_consecutive,
        bcfg.output_dir.display(),
    );

    std::fs::create_dir_all(&bcfg.output_dir).expect("Cannot create output dir");

    let mut all_summaries: Vec<RunSummary> = Vec::new();
    let mut baseline_cost: Option<f64>     = None;

    // ── greedy baseline ────────────────────────────────────────────────────
    if bcfg.include_greedy_baseline {
        let (per_req, batch_timings, summary) =
            run_greedy_baseline(&scenario, &base_cfg, realtime_slots, speed_scale);
        baseline_cost = Some(summary.total_carbon_cost);
        let per_ts = compute_per_timeslot(
            &per_req,
            scenario.metadata.total_slots,
            scenario.metadata.error_window_past,
            scenario.metadata.error_window_future,
        );
        let run_dir = bcfg.output_dir.join("baseline_greedy");
        write_run_outputs(&run_dir, &summary, &per_req, &batch_timings, &per_ts);
        // Top-level baseline files
        std::fs::write(
            bcfg.output_dir.join("baseline_summary.json"),
            serde_json::to_string_pretty(&summary_to_json(&summary)).unwrap(),
        ).unwrap();
        {
            let mut w = csv::Writer::from_path(bcfg.output_dir.join("baseline_summary.csv")).unwrap();
            w.write_record(SUMMARY_CSV_HEADER).unwrap();
            w.write_record(&summary_csv_row(&summary)).unwrap();
            w.flush().unwrap();
        }
        println!(
            "Completed baseline: mode={}, total_carbon={:.3}, flavour={}",
            summary.execution_mode,
            summary.total_carbon_cost,
            summary.baseline_flavour_name,
        );
    }

    // ── per-N runs ─────────────────────────────────────────────────────────
    for &batch_size in &bcfg.batch_sizes {
        let run_dir = bcfg.output_dir.join(format!("N{batch_size}"));

        let (per_req, batch_timings, mut summary) = run_single_n(
            &scenario,
            &base_cfg,
            batch_size,
            realtime_slots,
            speed_scale,
            &run_dir,
            verbose,
        );

        // Fill in baseline-relative fields.
        if let Some(bc) = baseline_cost {
            let saving = bc - summary.total_carbon_cost;
            let pct    = if bc > 0.0 { saving / bc * 100.0 } else { 0.0 };
            summary.baseline_total_carbon_cost         = bc;
            summary.carbon_cost_saving_vs_baseline     = saving;
            summary.carbon_cost_saving_vs_baseline_pct = pct;
        }

        let per_ts = compute_per_timeslot(
            &per_req,
            scenario.metadata.total_slots,
            scenario.metadata.error_window_past,
            scenario.metadata.error_window_future,
        );
        write_run_outputs(&run_dir, &summary, &per_req, &batch_timings, &per_ts);

        println!(
            "Completed N={batch_size}: solver_ms_avg={:.3}, total_carbon={:.3}, \
             global_error={:.3}, saving_vs_baseline={:.3}, \
             realtime_slots={realtime_slots}, scale={speed_scale:.2}",
            summary.solver_time_ms_avg,
            summary.total_carbon_cost,
            summary.global_average_error,
            summary.carbon_cost_saving_vs_baseline,
        );

        all_summaries.push(summary);
    }

    // ── aggregate output ───────────────────────────────────────────────────
    std::fs::write(
        bcfg.output_dir.join("summary_by_n.json"),
        serde_json::to_string_pretty(
            &serde_json::json!({ "rows": all_summaries.iter().map(summary_to_json).collect::<Vec<_>>() }),
        ).unwrap(),
    ).unwrap();
    if !all_summaries.is_empty() {
        let mut w = csv::Writer::from_path(bcfg.output_dir.join("summary_by_n.csv")).unwrap();
        w.write_record(SUMMARY_CSV_HEADER).unwrap();
        for s in &all_summaries {
            w.write_record(&summary_csv_row(s)).unwrap();
        }
        w.flush().unwrap();
    }

    println!("\nWrote benchmark output for {} batch sizes to {}.", all_summaries.len(), bcfg.output_dir.display());
}
