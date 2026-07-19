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
//!     "infeasibility_recovery_mode":  "carryover"
//!   }
//! }
//! ```
//!
//! **Key runner fields:**
//! - `infeasibility_recovery_mode` (default: `Config::infeasibility_recovery_mode`, "carryover"):
//!   one of "min_error_greedy" | "carryover" | "forecast". The error-window constraint is never
//!   relaxed/removed; on infeasibility the scheduler always falls back directly to
//!   `greedy_fallback` (accurate flavour, cheapest feasible slot). This setting only controls
//!   whether/how synthetic mock requests dilute the error baseline used by the primary DP solve.
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

mod swarm;

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use carbonshift_rs::config::Config;
use carbonshift_rs::generator::RequestGenerator;
use carbonshift_rs::metrics_logger::MetricsLogger;
use carbonshift_rs::scenario::Scenario;
use carbonshift_rs::scheduler::BatchScheduler;
use carbonshift_rs::shared_state::SharedState;
use carbonshift_rs::types::{Assignment, CapacityTier, Flavour};

use serde_json::Value;

// ─── benchmark config ─────────────────────────────────────────────────────────

struct BenchmarkConfig {
    batch_sizes: Vec<usize>,
    scenario_path: PathBuf,
    output_dir: PathBuf,
    realtime_slots: bool,
    realtime_speed_scale: f64,
    include_greedy_baseline: bool,
    /// Overrides `Config::infeasibility_recovery_mode` when present; `None`
    /// keeps the default. One of "min_error_greedy" | "carryover" | "forecast".
    infeasibility_recovery_mode: Option<String>,
    /// Max consecutive rollbacks before force-committing (0 = rollback disabled).
    rollback_max_consecutive: usize,
    /// Additional offline strategies to run (e.g. "greedy_cheapest", "bandit", "ant_colony").
    additional_strategies: Vec<String>,
    /// Online strategies to run through the scheduler pipeline (e.g. "bandit", "ant_colony").
    online_strategies: Vec<String>,
    /// If > 0: flush a partial batch after this many seconds without a new request.
    batch_timeout_secs: f64,
    /// Overrides `Config::max_batch_solver_parallelism` when present; `None` keeps the default.
    max_batch_solver_parallelism: Option<usize>,
    /// Overrides `Config::online_swarm_mode` when present; `None` keeps the default ("serialized").
    online_swarm_mode: Option<String>,
    /// Precomputed baseline carbon cost to reuse when `include_greedy_baseline` is false
    /// (e.g. a second nshift invocation for additional/offline strategies that shouldn't
    /// recompute the greedy baseline already produced by an earlier invocation of the same
    /// scenario). When `None` and `include_greedy_baseline` is false, saving-vs-baseline
    /// fields simply stay at 0 for this run.
    baseline_total_carbon_cost: Option<f64>,
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
    let infeasibility_recovery_mode = runner
        .get("infeasibility_recovery_mode")
        .and_then(|x| x.as_str())
        .map(String::from);
    // TODO in run_battery this is set in a tmp config. ideally unify with the same param in config.rs
    let rollback_max_consecutive =
        runner.get("rollback_max_consecutive").and_then(|x| x.as_u64()).unwrap_or(3) as usize;

    let additional_strategies: Vec<String> = runner
        .get("additional_strategies")
        .and_then(|x| x.as_array())
        .map(|arr| arr.iter().filter_map(|x| x.as_str().map(String::from)).collect())
        .unwrap_or_default();

    let online_strategies: Vec<String> = runner
        .get("online_strategies")
        .and_then(|x| x.as_array())
        .map(|arr| arr.iter().filter_map(|x| x.as_str().map(String::from)).collect())
        .unwrap_or_default();

    let batch_timeout_secs =
        runner.get("batch_timeout_secs").and_then(|x| x.as_f64()).unwrap_or(0.0);

    let max_batch_solver_parallelism = runner
        .get("max_batch_solver_parallelism")
        .and_then(|x| x.as_u64())
        .map(|x| x as usize);

    let online_swarm_mode = runner
        .get("online_swarm_mode")
        .and_then(|x| x.as_str())
        .map(String::from);

    let baseline_total_carbon_cost = runner
        .get("baseline_total_carbon_cost")
        .and_then(|x| x.as_f64());

    BenchmarkConfig { batch_sizes, scenario_path, output_dir, realtime_slots, realtime_speed_scale, include_greedy_baseline, infeasibility_recovery_mode, rollback_max_consecutive, additional_strategies, online_strategies, batch_timeout_secs, max_batch_solver_parallelism, online_swarm_mode, baseline_total_carbon_cost }
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
    /// Slots past deadline at scheduling time (0 = on time or early).
    lateness_slots: i32,
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
    /// Requests scheduled past their deadline (late flush or post-run drain).
    requests_late: usize,
    /// Maximum lateness observed (slots past deadline, 0 = all on time).
    max_lateness_slots: i32,
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
    /// Wall-clock seconds spent on this single N run (0.0 for the greedy baseline).
    run_elapsed_seconds: f64,
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
                lateness_slots:           (a.scheduled_slot - a.deadline_slot).max(0),
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
    let greedy = per_req.iter().filter(|r| r.assigned_with_greedy_fallback).count();
    let relaxed = per_req.iter().filter(|r| r.assigned_with_relaxed_retry).count();
    let requests_late = per_req.iter().filter(|r| r.lateness_slots > 0).count();
    let max_lateness_slots = per_req.iter().map(|r| r.lateness_slots).max().unwrap_or(0);

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
        requests_unscheduled:    total_received.saturating_sub(scheduled),
        requests_late,
        max_lateness_slots,
        batches_executed:        batch_timings.len(),
        total_carbon_cost:       total_carbon,
        global_average_error:    avg_error,
        global_average_error_real:              avg_error,
        global_average_error_modeled:           avg_error,
        global_average_error_real_skip_first_k: 0.0,
        global_average_error_modeled_skip_first_k: 0.0,
        requests_assigned_with_greedy_fallback: greedy,
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
        run_elapsed_seconds:                 0.0,
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

/// Recompute per-request carbon costs from the final committed assignment state.
///
/// Under the per-request tier model each request is charged:
///   cost(r) = carbon[slot] × mult(position_K) × flavour_duration × scale
///
/// where `position_K` is the 1-indexed rank of request `r` within its slot.
/// Requests are sorted by request_id within each slot (deterministic canonical
/// order) to assign positions.
///
/// This function corrects for small baseline-drift errors that arise when
/// concurrent batches solve with slightly different snapshot baselines,
/// ensuring a consistent per-request breakdown that matches the true final
/// slot occupancies.
fn recompute_carbon_costs(
    per_req: &mut [PerRequest],
    final_assignments: &HashMap<u64, Assignment>,
    carbon_forecast: &[f64],
    tiers: &[CapacityTier],
    scale: f64,
) {
    // Group assignments by slot, sorted by request_id for a canonical position order.
    let mut slot_requests: HashMap<i32, Vec<(u64, i32)>> = HashMap::new();
    for a in final_assignments.values() {
        slot_requests.entry(a.scheduled_slot)
            .or_default()
            .push((a.request_id, a.flavour_duration));
    }
    for reqs in slot_requests.values_mut() {
        reqs.sort_by_key(|(id, _)| *id);
    }

    // Compute per-request cost: carbon * mult(1-indexed position) * duration * scale.
    let mut req_cost: HashMap<u64, f64> = HashMap::new();
    for (&slot, reqs) in &slot_requests {
        let carbon = carbon_forecast.get(slot as usize).copied().unwrap_or(0.0);
        for (pos_idx, &(req_id, dur)) in reqs.iter().enumerate() {
            let position = (pos_idx + 1) as i64;
            let mult = get_capacity_multiplier(tiers, position);
            req_cost.insert(req_id, carbon * mult * dur as f64 * scale);
        }
    }

    for r in per_req.iter_mut() {
        if let Some(&cost) = req_cost.get(&r.request_id) {
            r.carbon_cost = cost;
        }
    }
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
        for req in requests {
            seq += 1;
            let position = before_count + 1;
            let mult = get_capacity_multiplier(tiers, position);
            let cost = ci * mult * flav.duration as f64 * scale;
            before_count = position;
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
                lateness_slots:                0,
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

// ─── greedy cheapest strategy ─────────────────────────────────────────────────

/// Greedy cheapest: assign each request to the (slot, flavour) pair that
/// minimises carbon cost while satisfying:
/// - deadline and assignment_max_future_slots window,
/// - global error constraint (average error over all assigned requests ≤ threshold),
/// - window error constraint (average error in [slot−past, slot+future] ≤ threshold).
///
/// Falls back to the minimum-error flavour at the arrival slot when no
/// feasible (slot, flavour) pair exists.
fn run_greedy_cheapest(
    scenario: &Scenario,
    cfg: &Config,
    realtime_slots: bool,
    realtime_speed_scale: f64,
) -> (Vec<PerRequest>, Vec<BatchTiming>, RunSummary) {
    let slot_dur = cfg.slot_duration_seconds;
    let carbon   = &scenario.carbon_forecast;
    let tiers    = &cfg.capacity_tiers;
    let scale    = cfg.carbon_cost_duration_scale;
    let max_future   = cfg.assignment_max_future_slots;
    let total_slots  = cfg.total_slots;
    let win_past     = cfg.error_window_past;
    let win_future   = cfg.error_window_future;
    let max_err      = cfg.max_error_threshold;

    // Sort flavours cheapest-first (shortest duration = lowest carbon cost per slot).
    let mut sorted_flavours: Vec<&Flavour> = cfg.flavours.iter().collect();
    sorted_flavours.sort_by_key(|f| f.duration);

    // Fallback flavour = minimum error (for infeasible cases).
    let fallback_flav = cfg.flavours
        .iter()
        .min_by(|a, b| a.error.partial_cmp(&b.error).unwrap())
        .expect("no flavours");

    // Sort requests by (arrival_slot, request_id).
    let mut requests: Vec<_> = scenario.requests.iter().collect();
    requests.sort_by_key(|r| (r.arrival_slot, r.request_id));

    let mut slot_count: HashMap<i32, i32>       = HashMap::new();
    let mut slot_errors: HashMap<i32, Vec<f64>> = HashMap::new();
    let mut global_error_sum: f64 = 0.0;
    let mut global_count: usize   = 0;
    let mut per_req: Vec<PerRequest>    = Vec::new();
    let mut batch_timings: Vec<BatchTiming> = Vec::new();
    let mut seq: u64 = 0;

    for req in &requests {
        let arrival  = req.arrival_slot;
        let deadline = req
            .deadline_slot
            .min(arrival + max_future)
            .min(total_slots - 1);

        // Global error constraint: retrospective (based on the average error
        // *before* this request), not a per-candidate forward projection —
        // mirrors solve_dp's step-function behaviour (Step 4 in solve_dp).
        let global_avg = if global_count > 0 { global_error_sum / global_count as f64 } else { 0.0 };
        let global_constraint_active =
            cfg.global_error_constraint_enabled && global_count > 0 && global_avg > max_err;
        // If the hard constraint would exclude every flavour, fall back to the
        // full set (safety net identical to solve_dp's "never remove all flavours").
        let allowed_flavours: Vec<&Flavour> = if global_constraint_active && cfg.global_error_constraint_hard {
            let filtered: Vec<&Flavour> = sorted_flavours
                .iter()
                .filter(|f| f.error <= max_err)
                .copied()
                .collect();
            if filtered.is_empty() { sorted_flavours.clone() } else { filtered }
        } else {
            sorted_flavours.clone()
        };

        // Find the cheapest feasible (slot, flavour) pair with a full scan.
        let mut best: Option<(f64, i32, &Flavour)> = None;
        for slot in arrival..=deadline {
            let ci       = carbon.get(slot as usize).copied().unwrap_or(1.0);
            let position = *slot_count.get(&slot).unwrap_or(&0) + 1;
            let mult     = get_capacity_multiplier(tiers, position as i64);

            for flav in &allowed_flavours {
                let cost = ci * mult * flav.duration as f64 * scale;

                {
                    // Error window is anchored to `arrival` (when the request is
                    // being decided), not to the candidate `slot` being tried —
                    // mirrors solve_dp, which centers the window on current_slot
                    // regardless of where the request ends up being placed.
                    let mut win_sum = flav.error;
                    let mut win_cnt = 1usize;
                    for ws in (arrival - win_past)..=(arrival + win_future) {
                        if let Some(errs) = slot_errors.get(&ws) {
                            win_sum += errs.iter().sum::<f64>();
                            win_cnt += errs.len();
                        }
                    }
                    if win_sum / win_cnt as f64 > max_err { continue; }
                }
                if best.map(|(c, _, _)| cost < c).unwrap_or(true) {
                    best = Some((cost, slot, flav));
                }
            }
        }

        // Commit the cheapest feasible pair; fall back if none found.
        let (chosen_cost, chosen_slot, chosen_flav) = best.unwrap_or_else(|| {
            let slot     = arrival;
            let ci       = carbon.get(slot as usize).copied().unwrap_or(1.0);
            let position = *slot_count.get(&slot).unwrap_or(&0) + 1;
            let mult     = get_capacity_multiplier(tiers, position as i64);
            let cost     = ci * mult * fallback_flav.duration as f64 * scale;
            (cost, slot, fallback_flav)
        });

        *slot_count.entry(chosen_slot).or_insert(0) += 1;
        slot_errors.entry(chosen_slot).or_default().push(chosen_flav.error);
        global_error_sum += chosen_flav.error;
        global_count     += 1;
        seq += 1;

        let queue_wait_slots = (chosen_slot - arrival).max(0);
        per_req.push(PerRequest {
            request_id:             req.request_id,
            arrival_time:           arrival as f64 * slot_dur,
            arrival_slot:           arrival,
            deadline_slot:          req.deadline_slot,
            included_in_batch_slot: arrival,
            batch_sequence:         seq,
            scheduled_slot:         chosen_slot,
            queue_wait_slots,
            queue_wait_seconds:     queue_wait_slots as f64 * slot_dur,
            final_wait_slots:       queue_wait_slots,
            final_wait_seconds:     queue_wait_slots as f64 * slot_dur,
            flavour_name:           chosen_flav.name.clone(),
            error:                  chosen_flav.error,
            carbon_cost:            chosen_cost,
            assignment_solver_mode:   "greedy_cheapest".to_string(),
            assignment_solver_status: "ok".to_string(),
            assigned_with_greedy_fallback: false,
            assigned_with_relaxed_retry:   false,
            assigned_with_rollback:        false,
            lateness_slots:                (chosen_slot - req.deadline_slot).max(0),
        });
        batch_timings.push(BatchTiming {
            batch_sequence:       seq,
            batch_size_n:         0,
            effective_batch_size: 1,
            slot:                 arrival,
            pending_before:       1,
            solver_elapsed_ms:    0.0,
            scheduled:            true,
            flush_partial_batch:  false,
        });
    }

    let total_requests: usize = scenario.requests.len();
    let summary = compute_summary(
        "greedy_cheapest",
        0,
        realtime_slots,
        realtime_speed_scale,
        &per_req,
        &batch_timings,
        total_requests,
        &fallback_flav.name,
        fallback_flav.duration,
        fallback_flav.error,
        0, 0, 0,
        0,    // peak_concurrent_workers
        0.0,  // avg_concurrent_workers
    );
    (per_req, batch_timings, summary)
}

// ─── swarm strategy wrappers ──────────────────────────────────────────────────

/// Convert `SwarmAssignment` results into the standard `PerRequest` / summary
/// format used by the rest of the benchmark output.
fn convert_swarm_to_outputs(
    raw: Vec<swarm::SwarmAssignment>,
    mode_name: &str,
    scenario: &Scenario,
    cfg: &Config,
    realtime_slots: bool,
    realtime_speed_scale: f64,
) -> (Vec<PerRequest>, Vec<BatchTiming>, RunSummary) {
    let slot_dur = cfg.slot_duration_seconds;
    let best_flav = cfg.flavours
        .iter()
        .min_by(|a, b| a.error.partial_cmp(&b.error).unwrap())
        .expect("no flavours");

    let mut per_req: Vec<PerRequest> = Vec::with_capacity(raw.len());
    let mut batch_timings: Vec<BatchTiming> = Vec::with_capacity(raw.len());

    for (i, a) in raw.iter().enumerate() {
        let seq = (i + 1) as u64;
        let queue_wait_slots = (a.scheduled_slot - a.arrival_slot).max(0);
        per_req.push(PerRequest {
            request_id:             a.request_id,
            arrival_time:           a.arrival_slot as f64 * slot_dur,
            arrival_slot:           a.arrival_slot,
            deadline_slot:          a.deadline_slot,
            included_in_batch_slot: a.arrival_slot,
            batch_sequence:         seq,
            scheduled_slot:         a.scheduled_slot,
            queue_wait_slots,
            queue_wait_seconds:     queue_wait_slots as f64 * slot_dur,
            final_wait_slots:       queue_wait_slots,
            final_wait_seconds:     queue_wait_slots as f64 * slot_dur,
            flavour_name:           a.flavour_name.clone(),
            error:                  a.error,
            carbon_cost:            a.carbon_cost,
            assignment_solver_mode:   mode_name.to_string(),
            assignment_solver_status: "ok".to_string(),
            assigned_with_greedy_fallback: false,
            assigned_with_relaxed_retry:   false,
            assigned_with_rollback:        false,
            lateness_slots:                0,
        });
        batch_timings.push(BatchTiming {
            batch_sequence:       seq,
            batch_size_n:         0,
            effective_batch_size: 1,
            slot:                 a.arrival_slot,
            pending_before:       1,
            solver_elapsed_ms:    0.0,
            scheduled:            true,
            flush_partial_batch:  false,
        });
    }

    let summary = compute_summary(
        mode_name,
        0,
        realtime_slots,
        realtime_speed_scale,
        &per_req,
        &batch_timings,
        scenario.requests.len(),
        &best_flav.name,
        best_flav.duration,
        best_flav.error,
        0, 0, 0, 0, 0.0,
    );
    (per_req, batch_timings, summary)
}

fn run_bandit_strategy(
    scenario: &Scenario,
    cfg: &Config,
    realtime_slots: bool,
    realtime_speed_scale: f64,
) -> (Vec<PerRequest>, Vec<BatchTiming>, RunSummary) {
    let params = swarm::BanditParams::default();
    let mut requests: Vec<_> = scenario.requests.clone();
    requests.sort_by_key(|r| (r.arrival_slot, r.request_id));
    let raw = swarm::run_bandit(&requests, &scenario.carbon_forecast, cfg, &params);
    convert_swarm_to_outputs(raw, "bandit", scenario, cfg, realtime_slots, realtime_speed_scale)
}

fn run_ant_colony_strategy(
    scenario: &Scenario,
    cfg: &Config,
    realtime_slots: bool,
    realtime_speed_scale: f64,
) -> (Vec<PerRequest>, Vec<BatchTiming>, RunSummary) {
    let params = swarm::AcoParams::default();
    let mut requests: Vec<_> = scenario.requests.clone();
    requests.sort_by_key(|r| (r.arrival_slot, r.request_id));
    let raw = swarm::run_ant_colony(&requests, &scenario.carbon_forecast, cfg, &params);
    convert_swarm_to_outputs(raw, "ant_colony", scenario, cfg, realtime_slots, realtime_speed_scale)
}

/// Dispatch an additional strategy by name.
fn run_strategy(
    name: &str,
    scenario: &Scenario,
    cfg: &Config,
    realtime_slots: bool,
    realtime_speed_scale: f64,
) -> (Vec<PerRequest>, Vec<BatchTiming>, RunSummary) {
    match name {
        "greedy_cheapest" => run_greedy_cheapest(scenario, cfg, realtime_slots, realtime_speed_scale),
        "bandit"          => run_bandit_strategy(scenario, cfg, realtime_slots, realtime_speed_scale),
        "ant_colony"      => run_ant_colony_strategy(scenario, cfg, realtime_slots, realtime_speed_scale),
        other             => panic!("Unknown strategy: '{other}'"),
    }
}

// ─── output helpers ───────────────────────────────────────────────────────────

const SUMMARY_CSV_HEADER: &[&str] = &[
    "execution_mode", "batch_size", "realtime_slots", "realtime_speed_scale",
    "baseline_flavour_name", "baseline_flavour_duration", "baseline_flavour_error",
    "requests_total", "requests_scheduled", "requests_unscheduled",
    "requests_late", "max_lateness_slots",
    "batches_executed",
    "carbon_cost", "global_average_error", "global_average_error_real",
    "global_average_error_modeled", "global_average_error_real_skip_first_k",
    "global_average_error_modeled_skip_first_k",
    "requests_assigned_with_greedy_fallback", "requests_assigned_with_relaxed_retry",
    "total_rollbacks", "peak_consecutive_rollbacks", "requests_assigned_with_rollback",
    "peak_concurrent_workers", "avg_concurrent_workers",
    "solver_time_ms_min", "solver_time_ms_max", "solver_time_ms_avg",
    "queue_wait_seconds_min", "queue_wait_seconds_max", "queue_wait_seconds_avg",
    "final_wait_seconds_min", "final_wait_seconds_max", "final_wait_seconds_avg",
    "baseline_carbon_cost", "carbon_cost_saving_vs_baseline",
    "carbon_saving",
    "run_elapsed_seconds",
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

fn summary_csv_row(s: &RunSummary) -> Vec<String> {
    vec![
        s.execution_mode.clone(),       s.batch_size.to_string(),
        s.realtime_slots.to_string(),   s.realtime_speed_scale.to_string(),
        s.baseline_flavour_name.clone(),s.baseline_flavour_duration.to_string(),
        s.baseline_flavour_error.to_string(),
        s.requests_total.to_string(),   s.requests_scheduled.to_string(),
        s.requests_unscheduled.to_string(),
        s.requests_late.to_string(),    s.max_lateness_slots.to_string(),
        s.batches_executed.to_string(),
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
        s.run_elapsed_seconds.to_string(),
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
            "lateness_slots",
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
                r.lateness_slots.to_string(),
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
                "lateness_slots":r.lateness_slots,
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

/// Greedy late scheduling: assign pending requests that remained unprocessed
/// after the run ended.
///
/// `scheduled_slot` is set to `deadline_slot` (clamped to [arrival_slot, total_slots-1])
/// so that the carbon cost is computed at the slot the request *should* have occupied.
///
/// `lateness_slots` = (total_slots-1) - deadline_slot, i.e. how far past the deadline
/// the request was still sitting unprocessed when the run ended.  This is always >= 0
/// for drained requests because their deadline has already passed by end-of-run.
fn schedule_late_requests(
    remaining: Vec<carbonshift_rs::types::Request>,
    carbon_forecast: &[f64],
    cfg: &Config,
) -> Vec<PerRequest> {
    if remaining.is_empty() { return Vec::new(); }

    let slot_dur   = cfg.slot_duration_seconds;
    let tiers      = &cfg.capacity_tiers;
    let scale      = cfg.carbon_cost_duration_scale;
    let total_slots = cfg.total_slots;
    let fallback_flav = cfg.flavours
        .iter()
        .min_by(|a, b| a.error.partial_cmp(&b.error).unwrap())
        .expect("no flavours");

    let mut slot_count: HashMap<i32, i32> = HashMap::new();
    let mut out = Vec::with_capacity(remaining.len());

    let mut sorted = remaining;
    sorted.sort_by_key(|r| (r.deadline_slot, r.arrival_slot, r.id));

    for req in sorted {
        let sched_slot = req.deadline_slot
            .max(req.arrival_slot)
            .min(total_slots - 1);
        let position = *slot_count.get(&sched_slot).unwrap_or(&0) + 1;
        let ci   = carbon_forecast.get(sched_slot as usize).copied().unwrap_or(1.0);
        let mult = get_capacity_multiplier(tiers, position as i64);
        let cost = ci * mult * fallback_flav.duration as f64 * scale;
        // Lateness = how many slots past the deadline the request sat unprocessed.
        // We measure from the end of the run (total_slots-1), not from sched_slot,
        // because all drained requests were unprocessed until the run ended.
        let lateness = ((total_slots - 1) - req.deadline_slot).max(0);
        let queue_wait = (sched_slot - req.arrival_slot).max(0);

        *slot_count.entry(sched_slot).or_insert(0) += 1;

        out.push(PerRequest {
            request_id:             req.id,
            arrival_time:           req.arrival_slot as f64 * slot_dur,
            arrival_slot:           req.arrival_slot,
            deadline_slot:          req.deadline_slot,
            included_in_batch_slot: sched_slot,
            batch_sequence:         0,
            scheduled_slot:         sched_slot,
            queue_wait_slots:       queue_wait,
            queue_wait_seconds:     queue_wait as f64 * slot_dur,
            final_wait_slots:       queue_wait,
            final_wait_seconds:     queue_wait as f64 * slot_dur,
            flavour_name:           fallback_flav.name.clone(),
            error:                  fallback_flav.error,
            carbon_cost:            cost,
            assignment_solver_mode:   "late_fallback".to_string(),
            assignment_solver_status: "late".to_string(),
            assigned_with_greedy_fallback: false,
            assigned_with_relaxed_retry:   false,
            assigned_with_rollback:        false,
            lateness_slots:         lateness,
        });
    }
    out
}

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
    cfg.total_requests              = scenario.requests.len();

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

    // Phase 1: wait until the scenario's virtual time is exhausted (generator stops emitting).
    loop {
        std::thread::sleep(std::time::Duration::from_millis(50));
        if shared_state.virtual_elapsed_secs() >= total_dur {
            break;
        }
    }

    // Phase 2: let the scheduler drain the remaining pending queue.
    // This is critical in realtime mode where the scheduler can fall behind the
    // request-arrival rate: when the clock reaches total_dur, the generator has
    // already stopped, but many requests may still be waiting in the pending queue.
    // We keep the scheduler running until every request has been dispatched to a
    // DP worker AND every worker has finished (pending==0 AND active==0).
    //
    // Safety timeout: 60 extra seconds to avoid hanging on pathological configs.
    let drain_deadline = std::time::Instant::now() + std::time::Duration::from_secs(60);
    loop {
        let stats   = sched.get_statistics();
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

    let mut per_req  = compute_per_request(&assignment_rows, &runs_by_id, base_cfg.slot_duration_seconds, &rolled_back_ids);
    // Drain any requests that never made it into a batch (e.g., deadline
    // expired while waiting in the pending queue) and schedule them late.
    let remaining = shared_state.drain_pending_requests();
    if !remaining.is_empty() {
        let late = schedule_late_requests(remaining, &scenario.carbon_forecast, base_cfg);
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
    let exec_mode = if cfg.solver_strategy == "dp" || cfg.solver_strategy.is_empty() {
        "nshift_dp".to_string()
    } else {
        format!("nshift_{}", cfg.solver_strategy)
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

    // Print a definitive final progress line overwriting whatever partial
    // line the main_loop left behind.  This runs after late scheduling so
    // the numbers are always 100%.
    if !cfg.verbose {
        let total = scenario.requests.len();
        let scheduled = per_req.len();
        let pct = if total > 0 { scheduled as f64 / total as f64 * 100.0 } else { 0.0 };
        println!(
            "\r  [N={:2}] Scheduled {:>6}/{:<6} ({:5.1}%)  Received: {:>6}",
            batch_size, scheduled, total, pct, total_received
        );
    }

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
    if let Some(mode) = &bcfg.infeasibility_recovery_mode {
        base_cfg.infeasibility_recovery_mode = mode.clone();
    }
    base_cfg.rollback_max_consecutive     = bcfg.rollback_max_consecutive;
    base_cfg.batch_timeout_secs           = bcfg.batch_timeout_secs;
    if let Some(parallelism) = bcfg.max_batch_solver_parallelism {
        base_cfg.max_batch_solver_parallelism = parallelism;
    }
    if let Some(mode) = &bcfg.online_swarm_mode {
        base_cfg.online_swarm_mode = mode.clone();
    }

    println!(
        "Loaded scenario: {} slots, {} requests ({})",
        scenario.metadata.total_slots,
        scenario.requests.len(),
        bcfg.scenario_path.display(),
    );
    println!(
        "Config: batch_sizes={:?}, realtime_slots={}, speed_scale={:.2}, rollback_max_consecutive={} (online_swarm_mode={}), max_batch_solver_parallelism={}, output={}",
        bcfg.batch_sizes,
        realtime_slots,
        speed_scale,
        bcfg.rollback_max_consecutive,
        base_cfg.online_swarm_mode,
        base_cfg.max_batch_solver_parallelism,
        bcfg.output_dir.display(),
    );

    std::fs::create_dir_all(&bcfg.output_dir).expect("Cannot create output dir");

    let mut all_summaries: Vec<RunSummary> = Vec::new();
    let mut baseline_cost: Option<f64>     = bcfg.baseline_total_carbon_cost;

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
        let n_t0 = std::time::Instant::now();

        let (per_req, batch_timings, mut summary) = run_single_n(
            &scenario,
            &base_cfg,
            batch_size,
            realtime_slots,
            speed_scale,
            &run_dir,
            verbose,
        );
        summary.run_elapsed_seconds = n_t0.elapsed().as_secs_f64();

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

    // ── online strategies (generator + scheduler pipeline) ────────────────
    for strategy in &bcfg.online_strategies {
        for &batch_size in &bcfg.batch_sizes {
            let run_dir = bcfg.output_dir.join(format!("online_{strategy}")).join(format!("N{batch_size}"));
            let n_t0 = std::time::Instant::now();

            let mut online_base_cfg = base_cfg.clone();
            online_base_cfg.solver_strategy = strategy.clone();

            let (per_req, batch_timings, mut summary) = run_single_n(
                &scenario,
                &online_base_cfg,
                batch_size,
                realtime_slots,
                speed_scale,
                &run_dir,
                verbose,
            );
            summary.run_elapsed_seconds = n_t0.elapsed().as_secs_f64();

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
                "Completed online_strategy={strategy} N={batch_size}: solver_ms_avg={:.3}, \
                 total_carbon={:.3}, global_error={:.3}, saving_vs_baseline={:.3}",
                summary.solver_time_ms_avg,
                summary.total_carbon_cost,
                summary.global_average_error,
                summary.carbon_cost_saving_vs_baseline,
            );

            all_summaries.push(summary);
        }

        // Write per-strategy summary CSV.
        let strat_dir = bcfg.output_dir.join(format!("online_{strategy}"));
        let strat_summaries: Vec<&RunSummary> = all_summaries
            .iter()
            .rev()
            .take(bcfg.batch_sizes.len())
            .collect();
        if !strat_summaries.is_empty() {
            std::fs::create_dir_all(&strat_dir).ok();
            let mut w = csv::Writer::from_path(strat_dir.join("summary_by_n.csv")).unwrap();
            w.write_record(SUMMARY_CSV_HEADER).unwrap();
            for s in strat_summaries.iter().rev() {
                w.write_record(&summary_csv_row(s)).unwrap();
            }
            w.flush().unwrap();
        }
    }

    // ── additional strategies ──────────────────────────────────────────────
    for strategy in &bcfg.additional_strategies {
        let strat_dir = bcfg.output_dir.join(format!("strategy_{strategy}"));
        let strat_t0 = std::time::Instant::now();
        let (per_req, batch_timings, mut summary) =
            run_strategy(strategy, &scenario, &base_cfg, realtime_slots, speed_scale);
        summary.run_elapsed_seconds = strat_t0.elapsed().as_secs_f64();

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
        write_run_outputs(&strat_dir, &summary, &per_req, &batch_timings, &per_ts);
        println!(
            "Completed strategy={strategy}: total_carbon={:.3}, global_error={:.3}, \
             saving_vs_baseline={:.3}",
            summary.total_carbon_cost,
            summary.global_average_error,
            summary.carbon_cost_saving_vs_baseline,
        );
    }

    // ── aggregate output ───────────────────────────────────────────────────
    // Only DP runs belong in the top-level summary files; online strategy
    // results have their own CSVs under online_{strategy}/.
    let dp_summaries: Vec<&RunSummary> = all_summaries
        .iter()
        .filter(|s| s.execution_mode == "nshift_dp")
        .collect();

    std::fs::write(
        bcfg.output_dir.join("summary_by_n.json"),
        serde_json::to_string_pretty(
            &serde_json::json!({ "rows": dp_summaries.iter().map(|s| summary_to_json(s)).collect::<Vec<_>>() }),
        ).unwrap(),
    ).unwrap();
    if !dp_summaries.is_empty() {
        let mut w = csv::Writer::from_path(bcfg.output_dir.join("summary_by_n.csv")).unwrap();
        w.write_record(SUMMARY_CSV_HEADER).unwrap();
        for s in &dp_summaries {
            w.write_record(&summary_csv_row(s)).unwrap();
        }
        w.flush().unwrap();
    }

    if !dp_summaries.is_empty() {
        println!("\nWrote benchmark output for {} batch sizes to {}.", dp_summaries.len(), bcfg.output_dir.display());
    } else if !bcfg.additional_strategies.is_empty() {
        println!(
            "\nWrote {} additional-strategy summaries to {}.",
            bcfg.additional_strategies.len(), bcfg.output_dir.display(),
        );
    } else if !bcfg.online_strategies.is_empty() {
        println!("\nWrote online-strategy output to {}.", bcfg.output_dir.display());
    } else {
        println!("\nNo DP/strategy output written (no batch sizes or strategies configured).");
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use carbonshift_rs::types::CapacityTier;

    fn make_assignment(id: u64, slot: i32, dur: i32) -> Assignment {
        Assignment::new(id, slot, "Balanced".into(), 0.0, 0.0, dur, None, None)
    }

    fn make_per_req(id: u64, slot: i32) -> PerRequest {
        PerRequest {
            request_id: id, arrival_time: 0.0, arrival_slot: slot, deadline_slot: slot,
            included_in_batch_slot: slot, batch_sequence: 0, scheduled_slot: slot,
            queue_wait_slots: 0, queue_wait_seconds: 0.0, final_wait_slots: 0,
            final_wait_seconds: 0.0, flavour_name: "Balanced".into(), error: 0.0,
            carbon_cost: 999.0,  // deliberately wrong — will be recomputed
            assignment_solver_mode: "dp".into(), assignment_solver_status: "ok".into(),
            assigned_with_greedy_fallback: false, assigned_with_relaxed_retry: false,
            assigned_with_rollback: false, lateness_slots: 0,
        }
    }

    /// Single tier (mult=1.0 up to 20): all 10 requests pay the same.
    /// Under per-request model: each costs carbon * 1.0 * dur * scale.
    #[test]
    fn recompute_corrects_concurrent_baseline_drift() {
        let tiers = vec![CapacityTier { max_requests: Some(20), multiplier: 1.0 }];
        let carbon_forecast = vec![120.0_f64]; // slot 0 only
        let scale = 1.0 / 3600.0;

        // 10 requests in slot 0, all duration 30s
        let final_assignments: HashMap<u64, Assignment> = (1..=10u64)
            .map(|id| (id, make_assignment(id, 0, 30)))
            .collect();

        let mut per_req: Vec<PerRequest> = (1..=10u64).map(|id| make_per_req(id, 0)).collect();

        recompute_carbon_costs(&mut per_req, &final_assignments, &carbon_forecast, &tiers, scale);

        // Per-request model: each request at position 1..10 all in tier 1 (mult=1.0)
        // cost per request = 120 * 1.0 * 30 * scale
        let per_req_expected = 120.0 * 1.0 * 30.0 * scale;
        for r in &per_req {
            assert!((r.carbon_cost - per_req_expected).abs() < 1e-9,
                "request {} cost={} expected={}", r.request_id, r.carbon_cost, per_req_expected);
        }
        let expected_total = per_req_expected * 10.0;
        let computed_total: f64 = per_req.iter().map(|r| r.carbon_cost).sum();
        assert!((computed_total - expected_total).abs() < 1e-9,
            "total={computed_total}, expected={expected_total}");
    }

    /// Tier boundary crossing: 70 requests split across two tiers.
    /// Positions 1-60 use mult=1.0, positions 61-70 use mult=2.0.
    /// Earlier requests are NOT repriced when the boundary is crossed.
    #[test]
    fn recompute_handles_tier_crossing_correctly() {
        let tiers = vec![
            CapacityTier { max_requests: Some(60), multiplier: 1.0 },
            CapacityTier { max_requests: None,     multiplier: 2.0 },
        ];
        let carbon = 100.0_f64;
        let scale = 1.0 / 3600.0;

        let final_assignments: HashMap<u64, Assignment> = (1..=70u64)
            .map(|id| (id, make_assignment(id, 0, 30)))
            .collect();
        let mut per_req: Vec<PerRequest> = (1..=70u64).map(|id| make_per_req(id, 0)).collect();

        recompute_carbon_costs(&mut per_req, &final_assignments, &[carbon], &tiers, scale);

        // Positions 1..60 → mult=1.0; positions 61..70 → mult=2.0
        // IDs are sorted 1..70 so IDs 1..60 are positions 1..60, IDs 61..70 are positions 61..70
        let cost_tier1 = carbon * 1.0 * 30.0 * scale;
        let cost_tier2 = carbon * 2.0 * 30.0 * scale;
        per_req.sort_by_key(|r| r.request_id);
        for r in &per_req[..60] {
            assert!((r.carbon_cost - cost_tier1).abs() < 1e-9,
                "id={}: cost={}, expected={cost_tier1}", r.request_id, r.carbon_cost);
        }
        for r in &per_req[60..] {
            assert!((r.carbon_cost - cost_tier2).abs() < 1e-9,
                "id={}: cost={}, expected={cost_tier2}", r.request_id, r.carbon_cost);
        }
        let expected_total = 60.0 * cost_tier1 + 10.0 * cost_tier2;
        let computed_total: f64 = per_req.iter().map(|r| r.carbon_cost).sum();
        assert!((computed_total - expected_total).abs() < 1e-9,
            "total={computed_total}, expected={expected_total}");
    }
}
