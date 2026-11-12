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
//!     "realtime_speed_scale":         1.0
//!   }
//! }
//! ```
//!
//! **Key runner fields:**
//! - `online_strategies`: online strategies to run through the scheduler pipeline, e.g.
//!   `["bandit", "ant_colony", "singleton_best", "hazard"]`. Independent of the DP phase:
//!   set the top-level `batch_sizes` to
//!   `[]` to skip DP entirely and only run online strategies.
//! - `online_batch_sizes`: batch sizes to sweep for `online_strategies`, independent of the
//!   top-level `batch_sizes` (DP's sweep). Falls back to `batch_sizes` when absent.
//!   "ant_colony", "bandit", "hazard", and "singleton_best" use the configured sweep.
//! - `additional_strategies`: common-engine strategies to run once per scenario (no batching or
//!   concurrency), e.g. `["offline_singleton_best", "offline_singleton_best_accurate", "hazard"]`.
//!   `hazard` is the canonical stochastic strategy name. These runs are fully independent of `batch_sizes`,
//!   `max_batch_solver_parallelism`.
//!   `offline_singleton_best` scans feasible slot/flavour pairs and commits the cheapest choice;
//!   `offline_singleton_best_accurate` uses the same search with only the lowest-error flavour.
//!   `hazard` samples flavours from the closest-to-uniform distribution whose expected error
//!   equals the scenario threshold when attainable, otherwise the nearest attainable error. It
//!   then samples slots inversely to forecast intensity multiplied by the capacity tier at the
//!   slot's next occupancy position. It is statistical, not a per-run QoS guarantee.
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
//!                      per_timeslot.{json,csv} assignments_runtime.csv }
//!   N<n>/  { same files as above }
//! ```

use carbonshift_rs::config::{AssignmentPolicy, Config};
use carbonshift_rs::engine::qos::QosProfileId;
use carbonshift_rs::engine::solver::{
    CommitReport, ErrorTotals, ErrorWindowInput, PlacementOrigin, QoSProfileInput, SolveInput,
    SolveRequest, SolverError, SolverRegistry, SolverScope,
};
use carbonshift_rs::engine::solver_algorithms::{DpAlgorithmConfig, register_all_solvers};
use carbonshift_rs::generator::{RequestGenerator, RequestGeneratorConfig};
use carbonshift_rs::metrics_logger::write_qos_profile_catalog;
use carbonshift_rs::scenario::{Scenario, ScenarioRequest};
use carbonshift_rs::scheduler::BatchScheduler;
use carbonshift_rs::shared_state::SharedState;
#[cfg(test)]
use carbonshift_rs::types::CapacityTier;
use carbonshift_rs::types::{Assignment, Flavour, get_capacity_multiplier};
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, RwLock};

use serde_json::Value;

mod offline;
use offline::*;
mod reporting;
use reporting::*;
mod simulation;
use simulation::*;

// ─── benchmark config ─────────────────────────────────────────────────────────

struct BenchmarkConfig {
    batch_sizes: Vec<usize>,
    scenario_path: PathBuf,
    output_dir: PathBuf,
    realtime_slots: bool,
    realtime_speed_scale: f64,
    include_greedy_baseline: bool,
    /// Additional common-engine strategies to run (e.g. singleton_best, hazard).
    additional_strategies: Vec<String>,
    /// Online strategies to run through the scheduler pipeline (e.g. "bandit", "ant_colony").
    online_strategies: Vec<String>,
    /// Batch sizes to sweep for `online_strategies`, independent of `batch_sizes` (the DP
    /// sweep). Falls back to `batch_sizes` when absent, so existing configs keep working
    /// unchanged. This lets a battery run online strategies (e.g. bandit/ant_colony) without
    /// also running the DP phase (`batch_sizes: []`), or with a different N sweep than DP's.
    /// Honoured by the supported online strategies; unknown strategies use singleton batches.
    online_batch_sizes: Option<Vec<usize>>,
    /// If > 0: flush a partial batch after this many seconds without a new request.
    batch_timeout_secs: f64,
    /// Overrides `Config::max_batch_solver_parallelism` when present; `None` keeps the default.
    max_batch_solver_parallelism: Option<usize>,
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
        additional_strategies,
        online_strategies,
        online_batch_sizes,
        batch_timeout_secs,
        max_batch_solver_parallelism,
        baseline_total_carbon_cost,
        flavours,
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
#[cfg(test)]
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

// ─── strategy sweep helpers ──────────────────────────────────────────────────

/// These algorithms choose each request's placement sequentially, but can
/// amortize scheduler setup by receiving several requests in one solve.
fn supports_online_batch_size_sweep(strategy: &str) -> bool {
    ["ant_colony", "bandit", "hazard", "singleton_best"]
        .iter()
        .any(|supported| strategy.eq_ignore_ascii_case(supported))
}

fn online_batch_sizes_for_strategy(strategy: &str, configured: &[usize]) -> Vec<usize> {
    if supports_online_batch_size_sweep(strategy) {
        configured.to_vec()
    } else {
        vec![1]
    }
}

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
    base_cfg.solver.batch_timeout_secs = bcfg.batch_timeout_secs;
    if let Some(parallelism) = bcfg.max_batch_solver_parallelism {
        base_cfg.solver.max_batch_solver_parallelism = parallelism;
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
        "Config: batch_sizes={:?}, realtime_slots={}, speed_scale={:.2}, max_batch_solver_parallelism={}, output={}",
        bcfg.batch_sizes,
        realtime_slots,
        speed_scale,
        base_cfg.solver.max_batch_solver_parallelism,
        bcfg.output_dir.display(),
    );

    std::fs::create_dir_all(&bcfg.output_dir).expect("Cannot create output dir");
    write_output_schema_manifest(&bcfg.output_dir);
    let default_profile = default_qos_profile(&base_cfg);
    write_qos_profile_catalog(
        &bcfg.output_dir.join("qos_profiles.csv").to_string_lossy(),
        &bcfg
            .output_dir
            .join("qos_profile_flavours.csv")
            .to_string_lossy(),
        &[default_profile],
    );

    let mut all_summaries: Vec<RunSummary> = Vec::new();
    let mut baseline_cost: Option<f64> = bcfg.baseline_total_carbon_cost;

    // ── greedy baseline ────────────────────────────────────────────────────
    if bcfg.include_greedy_baseline {
        let (per_req, summary) = run_greedy_baseline(
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
        write_run_outputs(&run_dir, &summary, &per_req, &per_ts);
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

        let (mut summary, per_ts) = run_single_n(
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

        write_streamed_run_outputs(&run_dir, &summary, &per_ts);

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
        // Request-level solvers retain their sequential decision policies,
        // while allowing the scheduler to amortize per-batch setup.
        let batch_sizes_for_strategy =
            online_batch_sizes_for_strategy(strategy, &online_batch_sizes_base);
        if !supports_online_batch_size_sweep(strategy) && online_batch_sizes_base.as_slice() != [1]
        {
            println!(
                "[Info] online strategy '{strategy}' does not support batch-size sweeps; \
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

            let (mut summary, per_ts) = run_single_n(
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

            write_streamed_run_outputs(&run_dir, &summary, &per_ts);

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
        let (per_req, mut summary) = run_strategy(
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
        write_run_outputs(&strat_dir, &summary, &per_req, &per_ts);
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
mod tests;
