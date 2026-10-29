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
//! - `infeasibility_recovery_mode` (default: `Config::infeasibility.recovery_mode`, "carryover"):
//!   one of "min_error_greedy" | "carryover" | "forecast". The error-window constraint is never
//!   relaxed/removed; on infeasibility the scheduler always falls back directly to
//!   `greedy_fallback` (accurate flavour, cheapest feasible slot). This setting only controls
//!   whether/how synthetic mock requests dilute the error baseline used by the primary DP solve.
//! - `online_strategies`: online strategies to run through the scheduler pipeline, e.g.
//!   `["bandit", "ant_colony", "singleton_best", "random_flavour", "random_slot",
//!   "random_full"]`. Independent of the DP phase: set the top-level `batch_sizes` to
//!   `[]` to skip DP entirely and only run online strategies.
//! - `online_batch_sizes`: batch sizes to sweep for `online_strategies`, independent of the
//!   top-level `batch_sizes` (DP's sweep). Falls back to `batch_sizes` when absent. Only
//!   "ant_colony" uses the configured sweep; all other online strategies use batch size 1.
//!   `rollback_max_consecutive` affects `singleton_best` and the random modes because they
//!   share DP's rollback-checked commit path. `online_swarm_mode` affects bandit/ACO only.
//! - `additional_strategies`: common-engine strategies to run once per scenario (no batching or
//!   concurrency), e.g. `["offline_singleton_best", "offline_singleton_best_accurate", "hazard"]`.
//!   `hazard` is canonical. The old `random` input label remains temporarily for existing
//!   configurations and is scheduled for removal at the Step 5B online-scheduler cutover; new
//!   configurations should use `hazard`. These runs are fully independent of `batch_sizes`,
//!   `rollback_max_consecutive`, and `max_batch_solver_parallelism`.
//!   `offline_singleton_best` scans feasible slot/flavour pairs and commits the cheapest choice;
//!   `offline_singleton_best_accurate` uses the same search with only the lowest-error flavour.
//!   `hazard` samples flavours from the closest-to-uniform distribution whose expected error
//!   equals the scenario threshold, then samples slots inversely to forecast intensity multiplied
//!   by the capacity tier at the slot's next occupancy position. It is statistical, not a per-run
//!   QoS guarantee.
//! - `flavours`: overrides `Config::flavours` (the 3 built-in Accurate/Balanced/Fast
//!   defaults) with `[{"name": "...", "error": <f64>, "duration_sec": <f64>}, ...]`.
//!   Printed at startup alongside the other config knobs so it's clear which flavours
//!   a given run actually used.
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

use carbonshift_rs::config::{AssignmentPolicy, Config};
use carbonshift_rs::engine::qos::QosProfileId;
use carbonshift_rs::engine::solver::{
    CommitReport, ErrorTotals, ErrorWindowInput, QoSProfileInput, SolveInput, SolveRequest,
    SolverError, SolverRegistry, SolverScope,
};
use carbonshift_rs::engine::solver_algorithms::{DpAlgorithmConfig, register_all_solvers};
use carbonshift_rs::generator::{RequestGenerator, RequestGeneratorConfig};
use carbonshift_rs::metrics_logger::MetricsLogger;
use carbonshift_rs::scenario::{Scenario, ScenarioRequest};
use carbonshift_rs::scheduler::BatchScheduler;
use carbonshift_rs::shared_state::SharedState;
use carbonshift_rs::types::{Assignment, CapacityTier, Flavour, get_capacity_multiplier};
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, RwLock};

use serde_json::Value;

// ─── benchmark config ─────────────────────────────────────────────────────────

struct BenchmarkConfig {
    batch_sizes: Vec<usize>,
    scenario_path: PathBuf,
    output_dir: PathBuf,
    realtime_slots: bool,
    realtime_speed_scale: f64,
    include_greedy_baseline: bool,
    /// Overrides `Config::infeasibility.recovery_mode` when present; `None`
    /// keeps the default. One of "min_error_greedy" | "carryover" | "forecast".
    infeasibility_recovery_mode: Option<String>,
    /// Max consecutive rollbacks before force-committing (0 = rollback disabled).
    rollback_max_consecutive: usize,
    /// Additional common-engine strategies to run (e.g. singleton_best, hazard).
    additional_strategies: Vec<String>,
    /// Online strategies to run through the scheduler pipeline (e.g. "bandit", "ant_colony").
    online_strategies: Vec<String>,
    /// Batch sizes to sweep for `online_strategies`, independent of `batch_sizes` (the DP
    /// sweep). Falls back to `batch_sizes` when absent, so existing configs keep working
    /// unchanged. This lets a battery run online strategies (e.g. bandit/ant_colony) without
    /// also running the DP phase (`batch_sizes: []`), or with a different N sweep than DP's.
    /// Ignored for "singleton_best", which always forces `[1]` (its only supported batch size).
    online_batch_sizes: Option<Vec<usize>>,
    /// If > 0: flush a partial batch after this many seconds without a new request.
    batch_timeout_secs: f64,
    /// Overrides `Config::max_batch_solver_parallelism` when present; `None` keeps the default.
    max_batch_solver_parallelism: Option<usize>,
    /// Overrides `Config::swarm.mode` when present; `None` keeps the default ("serialized").
    online_swarm_mode: Option<String>,
    /// Precomputed baseline carbon cost to reuse when `include_greedy_baseline` is false
    /// (e.g. a second nshift invocation for additional/offline strategies that shouldn't
    /// recompute the greedy baseline already produced by an earlier invocation of the same
    /// scenario). When `None` and `include_greedy_baseline` is false, saving-vs-baseline
    /// fields simply stay at 0 for this run.
    baseline_total_carbon_cost: Option<f64>,
    /// Overrides `Config::flavours` when present; `None` keeps the 3 built-in
    /// Accurate/Balanced/Fast defaults from `Config::default()`.
    flavours: Option<Vec<Flavour>>,
}

fn load_benchmark_config(config_path: &Path) -> BenchmarkConfig {
    let text = std::fs::read_to_string(config_path)
        .unwrap_or_else(|e| panic!("Cannot read config {}: {e}", config_path.display()));
    let v: Value =
        serde_json::from_str(&text).unwrap_or_else(|e| panic!("Invalid JSON in config: {e}"));

    let batch_sizes: Vec<usize> = v["batch_sizes"]
        .as_array()
        .expect("batch_sizes must be an array")
        .iter()
        .map(|x| x.as_u64().expect("batch_sizes elements must be integers") as usize)
        .collect();

    let config_dir = config_path.parent().unwrap_or(Path::new("."));
    let scenario_path = config_dir.join(
        v["scenario_path"]
            .as_str()
            .expect("scenario_path must be a string"),
    );
    // Prefer rust_output_dir (so Python and Rust outputs don't overwrite each other).
    // Fall back to output_dir if rust_output_dir is absent.
    let out_key = if v.get("rust_output_dir").and_then(|x| x.as_str()).is_some() {
        "rust_output_dir"
    } else {
        "output_dir"
    };
    let output_dir = config_dir.join(
        v[out_key]
            .as_str()
            .expect("output_dir (or rust_output_dir) must be a string"),
    );

    let runner = &v["runner"];
    let realtime_slots = runner
        .get("realtime_slots")
        .and_then(|x| x.as_bool())
        .unwrap_or(false);
    let realtime_speed_scale = runner
        .get("realtime_speed_scale")
        .and_then(|x| x.as_f64())
        .unwrap_or(1.0);
    let include_greedy_baseline = runner
        .get("include_greedy_baseline")
        .and_then(|x| x.as_bool())
        .unwrap_or(true);
    let infeasibility_recovery_mode = runner
        .get("infeasibility_recovery_mode")
        .and_then(|x| x.as_str())
        .map(String::from);
    // TODO in run_battery this is set in a tmp config. ideally unify with the same param in config.rs
    let rollback_max_consecutive = runner
        .get("rollback_max_consecutive")
        .and_then(|x| x.as_u64())
        .unwrap_or(3) as usize;

    let additional_strategies: Vec<String> = runner
        .get("additional_strategies")
        .and_then(|x| x.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|x| x.as_str().map(String::from))
                .collect()
        })
        .unwrap_or_default();

    let online_strategies: Vec<String> = runner
        .get("online_strategies")
        .and_then(|x| x.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|x| x.as_str().map(String::from))
                .collect()
        })
        .unwrap_or_default();

    let online_batch_sizes: Option<Vec<usize>> = runner
        .get("online_batch_sizes")
        .and_then(|x| x.as_array())
        .map(|arr| {
            arr.iter()
                .map(|x| {
                    x.as_u64()
                        .expect("online_batch_sizes elements must be integers")
                        as usize
                })
                .collect()
        });

    let batch_timeout_secs = runner
        .get("batch_timeout_secs")
        .and_then(|x| x.as_f64())
        .unwrap_or(0.0);

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

    let flavours: Option<Vec<Flavour>> = runner.get("flavours").map(|x| {
        serde_json::from_value(x.clone())
            .expect("runner.flavours must be an array of {name, error, duration_sec}")
    });

    BenchmarkConfig {
        batch_sizes,
        scenario_path,
        output_dir,
        realtime_slots,
        realtime_speed_scale,
        include_greedy_baseline,
        infeasibility_recovery_mode,
        rollback_max_consecutive,
        additional_strategies,
        online_strategies,
        online_batch_sizes,
        batch_timeout_secs,
        max_batch_solver_parallelism,
        online_swarm_mode,
        baseline_total_carbon_cost,
        flavours,
    }
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
    mock_recovery_injected_count: i32,
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
    /// The request's assigning solve had a nonempty mock recovery pool.
    assigned_with_injection: bool,
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
    baseline_flavour_duration: f64,
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
    let i_pending = idx("pending_batch_size");
    let i_new = idx("new_assignments");
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

fn read_solver_assignments(path: &Path) -> Vec<AssignmentRow> {
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
        let is_new_str = g(i_is_new).to_lowercase();
        let is_new = is_new_str == "true" || is_new_str == "1";
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

fn compute_batch_timings(runs: &[SolverRunRow], batch_size_n: usize) -> Vec<BatchTiming> {
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

fn compute_per_timeslot(
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

fn min_max_avg(v: &[f64]) -> (f64, f64, f64) {
    if v.is_empty() {
        return (0.0, 0.0, 0.0);
    }
    let min = v.iter().cloned().fold(f64::INFINITY, f64::min);
    let max = v.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
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

// ─── greedy baseline ──────────────────────────────────────────────────────────

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
    let mut slot_requests: HashMap<i32, Vec<(u64, f64)>> = HashMap::new();
    for a in final_assignments.values() {
        slot_requests
            .entry(a.scheduled_slot)
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

// ─── common offline engine adapter ────────────────────────────────────────────

/// Separate seed salt keeps the hazard stream reproducible without sharing the
/// scenario generator's random sequence.
const HAZARD_SEED_SALT: u64 = 0xA3F1_95C7_4D2B_6801;

/// Keep runner-specific search settings out of the strategy implementations.
/// This offline runner registers the common engine factories; the live
/// scheduler will use the same composition point after its planned Step 5B
/// cutover from the legacy dispatch path.
fn build_solver_registry(config: &Config, hazard_seed: u64) -> Result<SolverRegistry, SolverError> {
    let dp_config = DpAlgorithmConfig {
        pruning_method: config.solver.dp_pruning_method.clone(),
        pruning_min_batch_size: config.solver.dp_pruning_min_batch_size,
        pruning_k: config.solver.dp_pruning_k,
        timeout_secs: config.solver.dp_timeout,
    };
    let mut registry = SolverRegistry::default();
    register_all_solvers(&mut registry, dp_config, &config.swarm, hazard_seed)?;
    Ok(registry)
}

/// Convert one offline scenario into the profile-scoped, runner-independent
/// contract that every common engine consumes.
fn project_scenario_to_solve_input(
    scenario: &Scenario,
    assignment: &AssignmentPolicy<'_>,
) -> Result<(SolverScope, SolveInput), SolverError> {
    let total_slots = usize::try_from(assignment.total_slots).map_err(|_| {
        SolverError::Algorithm("planning horizon must be a positive slot count".to_owned())
    })?;
    if total_slots == 0 {
        return Err(SolverError::Algorithm(
            "planning horizon must contain at least one slot".to_owned(),
        ));
    }
    if scenario.carbon_forecast.len() != total_slots {
        return Err(SolverError::Algorithm(format!(
            "scenario forecast has {} slots but the assignment policy has {total_slots}",
            scenario.carbon_forecast.len()
        )));
    }

    let profile_id = QosProfileId::default_profile();
    let scope = SolverScope {
        profile_id: profile_id.clone(),
        total_slots,
    };

    // A stable order makes seeded algorithms independent of the JSON request
    // array's ordering and gives stateful algorithms a repeatable work sequence.
    let mut requests = scenario.requests.clone();
    requests.sort_by_key(|request| (request.arrival_slot, request.request_id));

    let input = SolveInput {
        current_slot: 0,
        requests: requests
            .iter()
            .map(|request| SolveRequest {
                request_id: request.request_id,
                arrival_slot: request.arrival_slot,
                deadline_slot: request.deadline_slot,
            })
            .collect(),
        carbon_forecast: scenario.carbon_forecast.clone(),
        capacity_tiers: assignment.capacity_tiers.to_vec(),
        baseline_slot_counts: HashMap::new(),
        carbon_cost_duration_scale: assignment.carbon_cost_duration_scale,
        assignment_max_future_slots: assignment.assignment_max_future_slots,
        assignment_max_slot: Some(assignment.total_slots - 1),
        qos: QoSProfileInput {
            profile_id,
            flavours: assignment.flavours.to_vec(),
            cumulative_error_constraint_active: assignment.global_error_constraint_enabled
                && assignment.global_error_constraint_hard,
            cumulative_err: ErrorTotals::default(),
            err_by_slot: HashMap::new(),
            max_average_error: assignment.max_error_threshold,
            error_window: ErrorWindowInput {
                baseline_err: ErrorTotals::default(),
                past_slots: assignment.error_window_past,
                future_slots: assignment.error_window_future,
            },
        },
    };

    Ok((scope, input))
}

/// A final assignment plus the solver's late marker, which the domain's
/// `Assignment` intentionally does not store.
struct OfflineEnginePlacement {
    assignment: Assignment,
    arrival_slot: i32,
    deadline_slot: i32,
    late: bool,
}

/// Information validated once for each proposed placement and then used to
/// create the accepted, priced domain assignment.
struct ValidatedProposedPlacement<'request, 'flavour> {
    request: &'request ScenarioRequest,
    flavour: &'flavour Flavour,
    next_position: i64,
    carbon_cost: f64,
}

/// Check a single engine placement against its request, time bounds, profile,
/// forecast, and next capacity-tier position.
///
/// Keeping these checks together gives the materializer one clear boundary:
/// it either receives all the trusted data needed to build an `Assignment`,
/// or it gets a descriptive solver error and commits nothing.
fn validate_proposed_placement<'request, 'flavour>(
    placement: &carbonshift_rs::engine::solver::ProposedPlacement,
    solve_input: &SolveInput,
    scope: &SolverScope,
    requests_by_id: &HashMap<u64, &'request ScenarioRequest>,
    assignment: &'flavour AssignmentPolicy<'_>,
    slot_counts: &HashMap<i32, i64>,
    seen_request_ids: &mut HashSet<u64>,
) -> Result<ValidatedProposedPlacement<'request, 'flavour>, SolverError> {
    if !seen_request_ids.insert(placement.req_id) {
        return Err(SolverError::Algorithm(format!(
            "engine proposed request {} more than once",
            placement.req_id
        )));
    }

    let request = requests_by_id
        .get(&placement.req_id)
        .copied()
        .ok_or_else(|| {
            SolverError::Algorithm(format!(
                "engine proposed unknown request {}",
                placement.req_id
            ))
        })?;

    // `late` is explicit proposal metadata, not something inferred later from
    // the output row. Late placements must be at the current slot and after
    // their deadline; normal placements must satisfy every upper bound.
    if placement.late {
        if placement.scheduled_slot != solve_input.current_slot
            || placement.scheduled_slot <= request.deadline_slot
        {
            return Err(SolverError::Algorithm(format!(
                "engine marked request {} late at a non-late slot",
                placement.req_id
            )));
        }
    } else {
        let earliest = request.arrival_slot.max(solve_input.current_slot);
        let shift_limit = request
            .arrival_slot
            .checked_add(solve_input.assignment_max_future_slots)
            .ok_or_else(|| {
                SolverError::Algorithm(format!(
                    "request {} assignment window overflowed",
                    placement.req_id
                ))
            })?;
        let forecast_window_end = solve_input
            .current_slot
            .checked_add(solve_input.qos.error_window.future_slots)
            .ok_or_else(|| {
                SolverError::Algorithm("forecast window boundary overflowed".to_owned())
            })?;
        let planning_horizon_end = i32::try_from(scope.total_slots)
            .ok()
            .and_then(|slot_count| slot_count.checked_sub(1))
            .ok_or_else(|| {
                SolverError::Algorithm("planning horizon must contain valid slots".to_owned())
            })?;
        let latest = request
            .deadline_slot
            .min(shift_limit)
            .min(forecast_window_end)
            .min(
                solve_input
                    .assignment_max_slot
                    .unwrap_or(planning_horizon_end),
            )
            .min(planning_horizon_end);

        if placement.scheduled_slot < earliest || placement.scheduled_slot > latest {
            return Err(SolverError::Algorithm(format!(
                "engine placed request {} outside its feasible slot window",
                placement.req_id
            )));
        }
    }

    let flavour = assignment
        .flavours
        .iter()
        .find(|flavour| flavour.name == placement.flavour_name)
        .ok_or_else(|| {
            SolverError::Algorithm(format!(
                "engine selected unknown flavour '{}' for request {}",
                placement.flavour_name, placement.req_id
            ))
        })?;
    let slot_index = usize::try_from(placement.scheduled_slot).map_err(|_| {
        SolverError::Algorithm(format!(
            "engine selected invalid slot {} for request {}",
            placement.scheduled_slot, placement.req_id
        ))
    })?;
    let carbon_intensity = solve_input
        .carbon_forecast
        .get(slot_index)
        .copied()
        .ok_or_else(|| {
            SolverError::Algorithm(format!(
                "engine selected a slot outside the forecast for request {}",
                placement.req_id
            ))
        })?;

    let current_count = slot_counts
        .get(&placement.scheduled_slot)
        .copied()
        .unwrap_or(0);
    if current_count < 0 {
        return Err(SolverError::Algorithm(format!(
            "slot {} occupancy cannot be negative",
            placement.scheduled_slot
        )));
    }
    let next_position = current_count.checked_add(1).ok_or_else(|| {
        SolverError::Algorithm(format!(
            "slot {} occupancy overflowed i64",
            placement.scheduled_slot
        ))
    })?;
    let capacity_multiplier = get_capacity_multiplier(assignment.capacity_tiers, next_position);
    let duration_scale = assignment.carbon_cost_duration_scale;
    if !carbon_intensity.is_finite()
        || carbon_intensity < 0.0
        || !capacity_multiplier.is_finite()
        || capacity_multiplier < 0.0
        || !flavour.error.is_finite()
        || !flavour.duration.is_finite()
        || flavour.duration < 0.0
        || !duration_scale.is_finite()
        || duration_scale < 0.0
    {
        return Err(SolverError::Algorithm(format!(
            "request {} has invalid forecast, capacity, flavour, or cost-scale data",
            placement.req_id
        )));
    }
    let carbon_cost = carbon_intensity * capacity_multiplier * flavour.duration * duration_scale;
    if !carbon_cost.is_finite() {
        return Err(SolverError::Algorithm(format!(
            "request {} produced a non-finite carbon price",
            placement.req_id
        )));
    }

    Ok(ValidatedProposedPlacement {
        request,
        flavour,
        next_position,
        carbon_cost,
    })
}

/// Apply the offline runner's accept-and-price rule to a proposal.
///
/// Proposed placements deliberately have no carbon price. This adapter walks
/// them in proposal order, prices each at the next occupancy position for its
/// slot, then increments that slot's count. The resulting prices are exactly
/// what stateful engines receive in `CommitReport`, so their learned costs are
/// based on accepted, capacity-adjusted assignments rather than estimates.
fn materialize_offline_proposal(
    proposal: &carbonshift_rs::engine::solver::SolverProposal,
    solve_input: &SolveInput,
    scope: &SolverScope,
    scenario: &Scenario,
    assignment: &AssignmentPolicy<'_>,
) -> Result<Vec<OfflineEnginePlacement>, SolverError> {
    let requests_by_id: HashMap<u64, _> = scenario
        .requests
        .iter()
        .map(|request| (request.request_id, request))
        .collect();
    if requests_by_id.len() != scenario.requests.len() {
        return Err(SolverError::Algorithm(
            "scenario contains duplicate request IDs".to_owned(),
        ));
    }

    let mut seen_request_ids = HashSet::with_capacity(proposal.placements.len());
    let mut slot_counts = solve_input.baseline_slot_counts.clone();
    let mut materialized = Vec::with_capacity(proposal.placements.len());

    for placement in &proposal.placements {
        let validated = validate_proposed_placement(
            placement,
            solve_input,
            scope,
            &requests_by_id,
            assignment,
            &slot_counts,
            &mut seen_request_ids,
        )?;
        *slot_counts.entry(placement.scheduled_slot).or_insert(0) = validated.next_position;
        materialized.push(OfflineEnginePlacement {
            assignment: Assignment::new_for_profile(
                placement.req_id,
                placement.scheduled_slot,
                placement.flavour_name.clone(),
                validated.carbon_cost,
                validated.flavour.error,
                validated.flavour.duration,
                Some(validated.request.arrival_slot),
                Some(validated.request.deadline_slot),
                solve_input.qos.profile_id.clone(),
            ),
            arrival_slot: validated.request.arrival_slot,
            deadline_slot: validated.request.deadline_slot,
            late: placement.late,
        });
    }

    if seen_request_ids.len() != scenario.requests.len() {
        return Err(SolverError::Algorithm(format!(
            "engine proposed {} of {} scenario requests",
            seen_request_ids.len(),
            scenario.requests.len()
        )));
    }

    Ok(materialized)
}

/// Convert accepted common-engine assignments to the established `nshift`
/// per-request and summary output records.
fn convert_engine_results_to_outputs(
    mut placements: Vec<OfflineEnginePlacement>,
    strategy_name: &str,
    scenario: &Scenario,
    assignment: &AssignmentPolicy<'_>,
    slot_duration_seconds: f64,
    realtime_slots: bool,
    realtime_speed_scale: f64,
) -> (Vec<PerRequest>, Vec<BatchTiming>, RunSummary) {
    placements.sort_by_key(|placement| (placement.arrival_slot, placement.assignment.request_id));

    let most_accurate = assignment
        .flavours
        .iter()
        .min_by(|left, right| left.error.total_cmp(&right.error))
        .expect("a valid engine input has at least one flavour");
    let mut per_request = Vec::with_capacity(placements.len());
    let mut batch_timings = Vec::with_capacity(placements.len());

    for (index, placement) in placements.iter().enumerate() {
        let assignment_result = &placement.assignment;
        let arrival_slot = placement.arrival_slot;
        let deadline_slot = placement.deadline_slot;
        let sequence = (index + 1) as u64;
        let queue_wait_slots = (assignment_result.scheduled_slot - arrival_slot).max(0);
        let lateness_slots = if placement.late {
            assignment_result.scheduled_slot - deadline_slot
        } else {
            0
        };

        per_request.push(PerRequest {
            request_id: assignment_result.request_id,
            arrival_time: arrival_slot as f64 * slot_duration_seconds,
            arrival_slot,
            deadline_slot,
            included_in_batch_slot: arrival_slot,
            batch_sequence: sequence,
            scheduled_slot: assignment_result.scheduled_slot,
            queue_wait_slots,
            queue_wait_seconds: queue_wait_slots as f64 * slot_duration_seconds,
            final_wait_slots: queue_wait_slots,
            final_wait_seconds: queue_wait_slots as f64 * slot_duration_seconds,
            flavour_name: assignment_result.flavour_name.clone(),
            error: assignment_result.error,
            carbon_cost: assignment_result.carbon_cost,
            assignment_solver_mode: strategy_name.to_owned(),
            assignment_solver_status: "ok".to_owned(),
            assigned_with_greedy_fallback: false,
            assigned_with_relaxed_retry: false,
            assigned_with_injection: false,
            assigned_with_rollback: false,
            lateness_slots,
        });
        batch_timings.push(BatchTiming {
            batch_sequence: sequence,
            batch_size_n: 0,
            effective_batch_size: 1,
            slot: arrival_slot,
            pending_before: 1,
            solver_elapsed_ms: 0.0,
            scheduled: true,
            flush_partial_batch: false,
        });
    }

    let summary = compute_summary(
        strategy_name,
        0,
        realtime_slots,
        realtime_speed_scale,
        &per_request,
        &batch_timings,
        scenario.requests.len(),
        &most_accurate.name,
        most_accurate.duration,
        most_accurate.error,
        0,
        0,
        0,
        0,
        0.0,
    );
    (per_request, batch_timings, summary)
}

/// Normalize offline labels at the runner boundary, then create and run the
/// requested shared engine. The temporary `random` input label routes to
/// `hazard` until Step 5B migrates the online scheduler and removes that label.
fn canonical_offline_engine_name(name: &str) -> Result<(&'static str, &'static str), SolverError> {
    match name.trim().to_ascii_lowercase().as_str() {
        "offline_singleton_best" => Ok(("singleton_best", "offline_singleton_best")),
        "singleton_best" => Ok(("singleton_best", "singleton_best")),
        "offline_singleton_best_accurate" => {
            Ok(("singleton_best_accurate", "offline_singleton_best_accurate"))
        }
        "singleton_best_accurate" => Ok(("singleton_best_accurate", "singleton_best_accurate")),
        // Remove this temporary label at the same time the online scheduler
        // switches its legacy random modes to the common hazard engine.
        "random" | "hazard" => Ok(("hazard", "hazard")),
        "dp" => Ok(("dp", "dp")),
        "bandit" => Ok(("bandit", "bandit")),
        "ant_colony" => Ok(("ant_colony", "ant_colony")),
        _ => Err(SolverError::UnknownEngine(name.to_owned())),
    }
}

/// Run one offline scenario through the common solver registry and lifecycle.
fn run_strategy(
    registry: &SolverRegistry,
    name: &str,
    scenario: &Scenario,
    config: &Config,
    slot_duration_seconds: f64,
    realtime_slots: bool,
    realtime_speed_scale: f64,
) -> Result<(Vec<PerRequest>, Vec<BatchTiming>, RunSummary), SolverError> {
    let (engine_name, output_name) = canonical_offline_engine_name(name)?;
    let assignment = config.assignment_policy();
    let (scope, solve_input) = project_scenario_to_solve_input(scenario, &assignment)?;
    let runtime = registry.create(engine_name, scope.clone())?;
    let pending = runtime.propose(&solve_input)?;

    // If conversion rejects a proposal, settle its pending state delta as
    // discarded before returning the explicit conversion error.
    let materialized = match materialize_offline_proposal(
        &pending.proposal,
        &solve_input,
        &scope,
        scenario,
        &assignment,
    ) {
        Ok(assignments) => assignments,
        Err(error) => {
            runtime.discard(pending.id)?;
            return Err(error);
        }
    };

    let commit_report = CommitReport {
        assignments: materialized
            .iter()
            .map(|placement| placement.assignment.clone())
            .collect(),
    };
    runtime.commit(pending.id, commit_report)?;

    Ok(convert_engine_results_to_outputs(
        materialized,
        output_name,
        scenario,
        &assignment,
        slot_duration_seconds,
        realtime_slots,
        realtime_speed_scale,
    ))
}

/// Only ACO makes meaningful use of offline batch-size sweeps; every other
/// online strategy is deliberately run as a sequence of singleton batches.
fn online_batch_sizes_for_strategy(strategy: &str, configured: &[usize]) -> Vec<usize> {
    if strategy.eq_ignore_ascii_case("ant_colony") {
        configured.to_vec()
    } else {
        vec![1]
    }
}

// ─── output helpers ───────────────────────────────────────────────────────────

const SUMMARY_CSV_HEADER: &[&str] = &[
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

fn summary_to_json(s: &RunSummary) -> Value {
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

fn summary_csv_row(s: &RunSummary) -> Vec<String> {
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
    let mut sched = BatchScheduler::new(
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

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mut config_path: Option<PathBuf> = None;
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
            "--realtime-slots" => {
                realtime_override = Some(true);
            }
            "--no-realtime-slots" => {
                realtime_override = Some(false);
            }
            "--speed-scale" => {
                i += 1;
                let v: f64 = args
                    .get(i)
                    .and_then(|s| s.parse().ok())
                    .expect("--speed-scale requires a numeric value in [0, 1]");
                assert!(
                    (0.0..=1.0).contains(&v),
                    "--speed-scale must be in [0.0, 1.0]"
                );
                speed_scale_override = Some(v);
            }
            "--verbose" => {
                verbose = true;
            }
            _ => {}
        }
        i += 1;
    }

    let config_path = config_path.unwrap_or_else(|| PathBuf::from("config.json"));
    let bcfg = load_benchmark_config(&config_path);
    let realtime_slots = realtime_override.unwrap_or(bcfg.realtime_slots);
    let speed_scale = speed_scale_override.unwrap_or(bcfg.realtime_speed_scale);

    let scenario = Scenario::from_file(bcfg.scenario_path.to_str().unwrap()).unwrap_or_else(|e| {
        panic!(
            "Cannot load scenario '{}': {e}",
            bcfg.scenario_path.display()
        )
    });

    // Build a base config that has all scenario parameters applied.
    let mut base_cfg = Config::default();
    base_cfg.apply_scenario_metadata(&scenario.metadata);
    if let Some(mode) = &bcfg.infeasibility_recovery_mode {
        base_cfg.infeasibility.recovery_mode = mode.clone();
    }
    base_cfg.solver.rollback_max_consecutive = bcfg.rollback_max_consecutive;
    base_cfg.solver.batch_timeout_secs = bcfg.batch_timeout_secs;
    if let Some(parallelism) = bcfg.max_batch_solver_parallelism {
        base_cfg.solver.max_batch_solver_parallelism = parallelism;
    }
    if let Some(mode) = &bcfg.online_swarm_mode {
        base_cfg.swarm.mode = mode.clone();
    }
    if let Some(flavours) = &bcfg.flavours {
        base_cfg.flavours = flavours.clone();
    }

    // Build the registry once for this benchmark run. Each configured strategy
    // then gets a fresh scoped runtime from the same factories.
    let common_engine_registry =
        build_solver_registry(&base_cfg, scenario.metadata.seed ^ HAZARD_SEED_SALT)
            .unwrap_or_else(|error| panic!("Cannot register common solver engines: {error}"));

    println!(
        "Loaded scenario: {} slots, {} requests ({})",
        scenario.metadata.total_slots,
        scenario.requests.len(),
        bcfg.scenario_path.display(),
    );
    let flavour_summary: String = base_cfg
        .flavours
        .iter()
        .map(|f| format!("{}(error={:.1}%,duration={}s)", f.name, f.error, f.duration))
        .collect::<Vec<_>>()
        .join(", ");
    println!("Flavours in use: [{flavour_summary}]");
    println!(
        "Config: batch_sizes={:?}, realtime_slots={}, speed_scale={:.2}, rollback_max_consecutive={} (online_swarm_mode={}), max_batch_solver_parallelism={}, output={}",
        bcfg.batch_sizes,
        realtime_slots,
        speed_scale,
        bcfg.rollback_max_consecutive,
        base_cfg.swarm.mode,
        base_cfg.solver.max_batch_solver_parallelism,
        bcfg.output_dir.display(),
    );

    std::fs::create_dir_all(&bcfg.output_dir).expect("Cannot create output dir");

    let mut all_summaries: Vec<RunSummary> = Vec::new();
    let mut baseline_cost: Option<f64> = bcfg.baseline_total_carbon_cost;

    // ── greedy baseline ────────────────────────────────────────────────────
    if bcfg.include_greedy_baseline {
        let (per_req, batch_timings, summary) = run_greedy_baseline(
            &scenario,
            &base_cfg.assignment_policy(),
            base_cfg.slot_duration_seconds,
            realtime_slots,
            speed_scale,
        );
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
        )
        .unwrap();
        {
            let mut w =
                csv::Writer::from_path(bcfg.output_dir.join("baseline_summary.csv")).unwrap();
            w.write_record(SUMMARY_CSV_HEADER).unwrap();
            w.write_record(&summary_csv_row(&summary)).unwrap();
            w.flush().unwrap();
        }
        println!(
            "Completed baseline: mode={}, total_carbon={:.3}, flavour={}",
            summary.execution_mode, summary.total_carbon_cost, summary.baseline_flavour_name,
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
            let pct = if bc > 0.0 { saving / bc * 100.0 } else { 0.0 };
            summary.baseline_total_carbon_cost = bc;
            summary.carbon_cost_saving_vs_baseline = saving;
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
    // Independent batch-size sweep from DP's `batch_sizes`, so a battery run
    // can enable online strategies (bandit/ant_colony/singleton_best)
    // without also running the DP phase (`batch_sizes: []`), or sweep a
    // different N list for online vs. DP. Falls back to `batch_sizes` when
    // `online_batch_sizes` is absent (keeps old configs behaving unchanged).
    let online_batch_sizes_base: Vec<usize> = bcfg
        .online_batch_sizes
        .clone()
        .unwrap_or_else(|| bcfg.batch_sizes.clone());
    for strategy in &bcfg.online_strategies {
        // Every online strategy except ACO makes one request-level decision at
        // a time, so only ACO has a meaningful batch-size sweep.
        let batch_sizes_for_strategy =
            online_batch_sizes_for_strategy(strategy, &online_batch_sizes_base);
        if !strategy.eq_ignore_ascii_case("ant_colony") && online_batch_sizes_base.as_slice() != [1]
        {
            println!(
                "[Info] online strategy '{strategy}' processes one request at a time; \
                 ignoring configured batch_sizes and using batch_size=1."
            );
        }

        for &batch_size in &batch_sizes_for_strategy {
            let run_dir = bcfg
                .output_dir
                .join(format!("online_{strategy}"))
                .join(format!("N{batch_size}"));
            let n_t0 = std::time::Instant::now();

            let mut online_base_cfg = base_cfg.clone();
            online_base_cfg.solver.solver_strategy = strategy.clone();

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
                let pct = if bc > 0.0 { saving / bc * 100.0 } else { 0.0 };
                summary.baseline_total_carbon_cost = bc;
                summary.carbon_cost_saving_vs_baseline = saving;
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
            .take(batch_sizes_for_strategy.len())
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
        let output_strategy_name = strategy.strip_prefix("offline_").unwrap_or(strategy);
        let strat_dir = bcfg
            .output_dir
            .join(format!("strategy_{output_strategy_name}"));
        let strat_t0 = std::time::Instant::now();
        let (per_req, batch_timings, mut summary) = run_strategy(
            &common_engine_registry,
            strategy,
            &scenario,
            &base_cfg,
            base_cfg.slot_duration_seconds,
            realtime_slots,
            speed_scale,
        )
        .unwrap_or_else(|error| panic!("Cannot run additional strategy '{strategy}': {error}"));
        summary.run_elapsed_seconds = strat_t0.elapsed().as_secs_f64();

        if let Some(bc) = baseline_cost {
            let saving = bc - summary.total_carbon_cost;
            let pct = if bc > 0.0 { saving / bc * 100.0 } else { 0.0 };
            summary.baseline_total_carbon_cost = bc;
            summary.carbon_cost_saving_vs_baseline = saving;
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
        println!(
            "\nWrote benchmark output for {} batch sizes to {}.",
            dp_summaries.len(),
            bcfg.output_dir.display()
        );
    } else if !bcfg.additional_strategies.is_empty() {
        println!(
            "\nWrote {} additional-strategy summaries to {}.",
            bcfg.additional_strategies.len(),
            bcfg.output_dir.display(),
        );
    } else if !bcfg.online_strategies.is_empty() {
        println!(
            "\nWrote online-strategy output to {}.",
            bcfg.output_dir.display()
        );
    } else {
        println!("\nNo DP/strategy output written (no batch sizes or strategies configured).");
    }
}

#[cfg(test)]
mod tests {
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
                prehistory_enabled: false,
                prehistory_error_ratio: 0.0,
                prehistory_mock_influence: 0.0,
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
        for strategy in [
            "bandit",
            "singleton_best",
            "random_flavour",
            "random_slot",
            "random_full",
        ] {
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
        let probabilities =
            carbonshift_rs::random_selection::closest_to_uniform_flavour_probabilities(
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
        let low_boundary =
            carbonshift_rs::random_selection::closest_to_uniform_flavour_probabilities(
                &[0.0, 0.0, 10.0],
                0.0,
            )
            .unwrap();
        assert_eq!(low_boundary, vec![0.5, 0.5, 0.0]);

        let low_target =
            carbonshift_rs::random_selection::closest_to_uniform_flavour_probabilities(
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
        assert_eq!(
            canonical_offline_engine_name("random").unwrap(),
            ("hazard", "hazard")
        );
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
        config.assignment_max_future_slots = 1;
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
    fn offline_common_engine_commit_prices_each_next_capacity_position() {
        let scenario = make_scenario(
            vec![make_request(1, 0, 0), make_request(2, 0, 0)],
            vec![10.0],
        );
        let mut config = Config::default();
        config.total_slots = 1;
        config.assignment_max_future_slots = 0;
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
        config.assignment_max_future_slots = 1;
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
        let (scope, mut solve_input) =
            project_scenario_to_solve_input(&scenario, &assignment).unwrap();
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
        config.assignment_max_future_slots = 1;
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
        config.assignment_max_future_slots = 1;
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
}
