#!/usr/bin/env python3
"""
Battery performance test runner for CarbonShift solver.

For each scenario in battery_config.json, generates a deterministic scenario
and runs the solver (Python, Rust, or both) across all configured N values and
infeasibility modes.  Results are appended to battery_results.csv.

Usage (from the repository root):
    python tests/battery/run_battery.py [--config tests/battery/battery_config.json]

Config fields
-------------
batch_sizes          : list[int]   – N values to benchmark
infeasibility_modes  : list[str]   – "greedy_fallback" | "relaxed_retry"
backend              : str         – "python" | "rust" | "both"
rust_binary_path     : str         – path to the nshift binary (relative to repo root)
output_dir           : str         – where to write run artefacts + battery_results.csv
scenarios            : list        – each entry has:
    id                   : str     – unique label used in CSV and directory names
    seed                 : int     – RNG seed for scenario generation
    requests_per_slot    : float
    total_slots          : int
    capacity_tiers       : list    – optional; falls back to online2/config.py defaults
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# ─── path setup ───────────────────────────────────────────────────────────────
CARBONSHIFT_ROOT = Path(__file__).resolve().parents[2]
ONLINE2_ROOT = CARBONSHIFT_ROOT / "online2"
sys.path.insert(0, str(ONLINE2_ROOT))

import config as online2_config  # noqa: E402  (after sys.path update)
from tests.Nshift_speed.scenario_io import generate_scenario_data, load_json, save_json  # noqa: E402
from tests.Nshift_speed.simulator import run_greedy_baseline, run_single_batch_size  # noqa: E402

# ─── constants ────────────────────────────────────────────────────────────────
RESULT_COLUMNS = [
    "scenario_id",
    "backend",
    "batch_size",
    "infeasibility_mode",
    "solver_time_ms_avg",
    "total_carbon_cost",
    "final_global_error",
    "avg_global_error_per_slot",
    "requests_assigned_with_greedy_fallback",
    "requests_assigned_with_relaxed_retry",
    "total_rollbacks",
    "max_consecutive_rollbacks",
    "baseline_total_carbon_cost",
    "carbon_cost_saving_vs_baseline_pct",
    "peak_concurrent_workers",
    "avg_concurrent_workers",
]

TIMING_COLUMNS = ["scenario_id", "elapsed_seconds"]


# ─── helpers ──────────────────────────────────────────────────────────────────

def _avg_slot_error_from_list(per_timeslot: List[Dict[str, Any]]) -> float:
    """Mean of window_avg_error_real across all timeslot entries."""
    errors = [
        float(r["window_avg_error_real"])
        for r in per_timeslot
        if r.get("window_avg_error_real") is not None
    ]
    return sum(errors) / len(errors) if errors else 0.0


def _avg_slot_error_from_csv(csv_path: Path) -> float:
    """Parse per_timeslot.csv and return mean of window_avg_error_real."""
    if not csv_path.exists():
        return 0.0
    errors: List[float] = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                errors.append(float(row["window_avg_error_real"]))
            except (KeyError, ValueError, TypeError):
                pass
    return sum(errors) / len(errors) if errors else 0.0


def _generate_scenario(scenario_def: Dict[str, Any], output_path: Path) -> Dict[str, Any]:
    """Generate a deterministic scenario JSON and return the loaded dict."""
    tiers = scenario_def.get("capacity_tiers", list(online2_config.CAPACITY_TIERS))
    data = generate_scenario_data(
        seed=int(scenario_def.get("seed", 42)),
        total_slots=int(scenario_def.get("total_slots", online2_config.TOTAL_SLOTS)),
        slot_duration_seconds=float(
            scenario_def.get("slot_duration_seconds", online2_config.SLOT_DURATION_SECONDS)
        ),
        requests_per_slot=float(
            scenario_def.get("requests_per_slot", online2_config.PREDICTED_REQUESTS_PER_SLOT)
        ),
        carbon_intensity_cycle_slots=24,
        request_rate_std_factor=float(online2_config.REQUEST_RATE_STD_FACTOR),
        deadline_min_slack=int(online2_config.DEADLINE_MIN_SLACK),
        deadline_max_slack=int(online2_config.DEADLINE_MAX_SLACK),
        error_window_past=int(online2_config.ERROR_WINDOW_PAST),
        error_window_future=int(online2_config.ERROR_WINDOW_FUTURE),
        max_error_threshold=float(online2_config.MAX_ERROR_THRESHOLD),
        prehistory_error_ratio=float(online2_config.PREHISTORY_ERROR_RATIO_OF_THRESHOLD),
        capacity_tiers=tiers,
    )
    save_json(output_path, data)
    return data


# ─── Python backend ────────────────────────────────────────────────────────────

def _run_python_baseline(scenario: Dict[str, Any]) -> float:
    """Run the Python greedy baseline; return total carbon cost."""
    result = run_greedy_baseline(scenario)
    return float(result.summary["total_carbon_cost"])


def _run_python_mode(
    scenario: Dict[str, Any],
    scenario_id: str,
    batch_sizes: List[int],
    mode: str,
    output_dir: Path,
    baseline_cost: float,
) -> List[Dict[str, Any]]:
    """Run the Python solver for all batch_sizes under one infeasibility mode."""
    dp_relaxed = mode == "relaxed_retry"
    original_relaxed = online2_config.DP_ALLOW_RELAXED_ERROR_RETRY
    online2_config.DP_ALLOW_RELAXED_ERROR_RETRY = dp_relaxed
    rows: List[Dict[str, Any]] = []
    try:
        for n in batch_sizes:
            n_dir = output_dir / f"N{n}"
            n_dir.mkdir(parents=True, exist_ok=True)
            result = run_single_batch_size(
                scenario, n,
                output_csv_path=str(n_dir / "assignments_runtime.csv"),
            )
            s = result.summary
            carbon = float(s["total_carbon_cost"])
            savings_pct = (
                (baseline_cost - carbon) / baseline_cost * 100.0
                if baseline_cost > 0.0
                else 0.0
            )
            rows.append({
                "scenario_id": scenario_id,
                "backend": "python",
                "batch_size": n,
                "infeasibility_mode": mode,
                "solver_time_ms_avg": float(s["solver_time_ms_avg"]),
                "total_carbon_cost": carbon,
                "final_global_error": float(s["global_average_error"]),
                "avg_global_error_per_slot": _avg_slot_error_from_list(result.per_timeslot),
                "requests_assigned_with_greedy_fallback": int(
                    s.get("requests_assigned_with_greedy_fallback", 0)
                ),
                "requests_assigned_with_relaxed_retry": int(
                    s.get("requests_assigned_with_relaxed_retry", 0)
                ),
                "total_rollbacks": 0,
                "max_consecutive_rollbacks": 0,
                "baseline_total_carbon_cost": baseline_cost,
                "carbon_cost_saving_vs_baseline_pct": savings_pct,
                "peak_concurrent_workers": 0,
                "avg_concurrent_workers": 0.0,
            })
            print(
                f"    [python/{mode}] N={n}: "
                f"solver_ms={float(s['solver_time_ms_avg']):.1f}, "
                f"carbon={carbon:.3f}, "
                f"error={float(s['global_average_error']):.3f}, "
                f"saving={savings_pct:.1f}%"
            )
    finally:
        online2_config.DP_ALLOW_RELAXED_ERROR_RETRY = original_relaxed
    return rows


# ─── Rust backend ──────────────────────────────────────────────────────────────

def _write_rust_config(
    scenario_path: Path,
    batch_sizes: List[int],
    output_dir: Path,
    dp_allow_relaxed: bool,
    include_baseline: bool,
) -> Path:
    """Write a temporary nshift config.json; caller is responsible for deletion."""
    fd, tmp = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(
            {
                "batch_sizes": batch_sizes,
                "scenario_path": str(scenario_path),
                "output_dir": str(output_dir),
                "rust_output_dir": str(output_dir),
                "runner": {
                    "flush_partial_batch": True,
                    "include_greedy_baseline": include_baseline,
                    "realtime_slots": True,
                    "realtime_speed_scale": 0.05,
                    "dp_allow_relaxed_error_retry": dp_allow_relaxed,
                    "rollback_max_consecutive": 0,
                    # TODO betterify param setting to avoid duplicating and hardcoding defaults
                },
            },
            f,
        )
    return Path(tmp)


def _run_rust_scenario(
    scenario_path: Path,
    scenario_id: str,
    batch_sizes: List[int],
    modes: List[str],
    output_dir: Path,
    rust_binary: Path,
) -> List[Dict[str, Any]]:
    """Run the Rust nshift binary for all modes of a single scenario."""
    baseline_cost: Optional[float] = None
    all_rows: List[Dict[str, Any]] = []

    for mode in modes:
        mode_dir = output_dir / f"rust_{mode}"
        mode_dir.mkdir(parents=True, exist_ok=True)
        dp_relaxed = mode == "relaxed_retry"
        include_baseline = baseline_cost is None  # compute baseline only on the first mode

        tmp_config = _write_rust_config(
            scenario_path, batch_sizes, mode_dir, dp_relaxed, include_baseline
        )
        try:
            subprocess.run([str(rust_binary), "--config", str(tmp_config)], check=True)
        finally:
            tmp_config.unlink(missing_ok=True)

        # Parse baseline cost from the first mode run
        if include_baseline:
            baseline_csv = mode_dir / "baseline_summary.csv"
            if baseline_csv.exists():
                with open(baseline_csv, newline="") as f:
                    rows_csv = list(csv.DictReader(f))
                if rows_csv:
                    baseline_cost = float(rows_csv[0].get("total_carbon_cost", 0.0))
                    print(f"    [rust/baseline]: carbon={baseline_cost:.3f}")

        bc = baseline_cost or 0.0

        # Parse per-N summary
        summary_csv = mode_dir / "summary_by_n.csv"
        if not summary_csv.exists():
            print(f"    WARNING: {summary_csv} not found; skipping mode={mode}")
            continue
        with open(summary_csv, newline="") as f:
            for row in csv.DictReader(f):
                n = int(row["batch_size"])
                carbon = float(row["total_carbon_cost"])
                savings_pct = (bc - carbon) / bc * 100.0 if bc > 0.0 else 0.0
                avg_slot_err = _avg_slot_error_from_csv(mode_dir / f"N{n}" / "per_timeslot.csv")
                all_rows.append({
                    "scenario_id": scenario_id,
                    "backend": "rust",
                    "batch_size": n,
                    "infeasibility_mode": mode,
                    "solver_time_ms_avg": float(row["solver_time_ms_avg"]),
                    "total_carbon_cost": carbon,
                    "final_global_error": float(row["global_average_error"]),
                    "avg_global_error_per_slot": avg_slot_err,
                    "requests_assigned_with_greedy_fallback": int(
                        row.get("requests_assigned_with_greedy_fallback", 0)
                    ),
                    "requests_assigned_with_relaxed_retry": int(
                        row.get("requests_assigned_with_relaxed_retry", 0)
                    ),
                    "total_rollbacks": int(row.get("total_rollbacks", 0)),
                    "max_consecutive_rollbacks": int(row.get("max_consecutive_rollbacks", 0)),
                    "baseline_total_carbon_cost": bc,
                    "carbon_cost_saving_vs_baseline_pct": savings_pct,
                    "peak_concurrent_workers": int(row.get("peak_concurrent_workers", 0)),
                    "avg_concurrent_workers": float(row.get("avg_concurrent_workers", 0.0)),
                })
                print(
                    f"    [rust/{mode}] N={n}: "
                    f"solver_ms={float(row['solver_time_ms_avg']):.1f}, "
                    f"carbon={carbon:.3f}, "
                    f"error={float(row['global_average_error']):.3f}, "
                    f"saving={savings_pct:.1f}%, ",
                    f"total_rollbacks={row.get('total_rollbacks', 0)}, "
                )
    return all_rows


# ─── Main orchestrator ─────────────────────────────────────────────────────────

def run_battery(config_path: Path) -> None:
    with open(config_path) as f:
        cfg = json.load(f)

    battery_dir = config_path.parent
    battery_id: str = cfg.get("battery_id", "run")
    output_dir = Path(cfg.get("output_dir", "results"))
    if not output_dir.is_absolute():
        output_dir = (CARBONSHIFT_ROOT / output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    batch_sizes: List[int] = [int(n) for n in cfg["batch_sizes"]]
    modes: List[str] = cfg.get("infeasibility_modes", ["greedy_fallback", "relaxed_retry"])
    # modes: List[str] = cfg.get("infeasibility_modes", ["greedy_fallback"])
    backend: str = cfg.get("backend", "python")

    rust_binary: Optional[Path] = None
    if backend in ("rust", "both"):
        raw = cfg.get("rust_binary_path", "rust/target/release/nshift")
        rust_binary = Path(raw) if Path(raw).is_absolute() else (CARBONSHIFT_ROOT / raw).resolve()
        if not rust_binary.exists():
            print(f"WARNING: Rust binary not found at {rust_binary}.")
            print("         Build it first:  cd rust && cargo build --release --bin nshift")
            if backend == "rust":
                sys.exit(1)
            rust_binary = None

    all_rows: List[Dict[str, Any]] = []
    timing_rows: List[Dict[str, Any]] = []
    battery_t0 = time.monotonic()

    for scenario_def in cfg["scenarios"]:
        sid: str = scenario_def["id"]
        seed: int = int(scenario_def.get("seed", 42))
        total_slots: int = int(scenario_def.get("total_slots", 24))
        req_per_slot: float = float(scenario_def.get("requests_per_slot", online2_config.PREDICTED_REQUESTS_PER_SLOT))

        print(f"\n{'='*60}")
        print(f"Scenario: {sid}  (seed={seed}, slots={total_slots}, req/slot={req_per_slot})")
        print("=" * 60)
        

        scenario_t0 = time.monotonic()
        scenario_dir = output_dir / sid
        scenario_dir.mkdir(parents=True, exist_ok=True)
        scenario_path = scenario_dir / f"scenario_seed_{seed}.json"
        scenario = _generate_scenario(scenario_def, scenario_path)

        # print first 24 values of carbon forecast for sanity check
        # print("  Carbon intensity forecast (first 24 slots):")
        # print("  ", scenario["carbon_forecast"][:24])

        if backend in ("python", "both"):
            print("  Computing Python greedy baseline …")
            py_baseline = _run_python_baseline(scenario)
            print(f"  Python baseline: {py_baseline:.3f}")
            for mode in modes:
                mode_dir = scenario_dir / f"python_{mode}"
                mode_dir.mkdir(parents=True, exist_ok=True)
                rows = _run_python_mode(scenario, sid, batch_sizes, mode, mode_dir, py_baseline)
                all_rows.extend(rows)

        if backend in ("rust", "both") and rust_binary is not None:
            rows = _run_rust_scenario(scenario_path, sid, batch_sizes, modes, scenario_dir, rust_binary)
            all_rows.extend(rows)

        scenario_elapsed = time.monotonic() - scenario_t0
        timing_rows.append({"scenario_id": sid, "elapsed_seconds": round(scenario_elapsed, 3)})
        print(f"  Scenario {sid} completed in {scenario_elapsed:.1f}s")

    battery_elapsed = time.monotonic() - battery_t0
    timing_rows.append({"scenario_id": "__battery_total__", "elapsed_seconds": round(battery_elapsed, 3)})

    results_csv = output_dir / f"battery_results_{battery_id}.csv"
    with open(results_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=RESULT_COLUMNS)
        writer.writeheader()
        writer.writerows(all_rows)

    timings_csv = output_dir / f"battery_timings_{battery_id}.csv"
    with open(timings_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TIMING_COLUMNS)
        writer.writeheader()
        writer.writerows(timing_rows)

    print(f"\n{'='*60}")
    print(f"Battery complete in {battery_elapsed:.1f}s: {len(all_rows)} rows written to {results_csv}")
    print(f"Timings written to {timings_csv}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run CarbonShift solver battery tests.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).parent / "battery_config.json",
        help="Path to battery configuration JSON (default: battery_config.json next to this script).",
    )
    args = parser.parse_args(argv)
    if not args.config.exists():
        parser.error(f"Config not found: {args.config}")
    run_battery(args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
