#!/usr/bin/env python3
"""
Battery performance test runner for CarbonShift solver (Rust `nshift` binary only).

NOTE: `nshift` (this script's target) is the CLI scenario-batch benchmark tool,
built from `rust/src/bin/nshift/`. It is unrelated to `carbonshift-service`
(`rust/src/bin/service/`), the Dockerizable REST front-end used by the
carbonshift/executor/client architecture (see `client/`, `PLAN_SERVICE.md`) —
that binary is HTTP/env-var configured and does not accept `--config <path>`
scenario batches, so it can never be used here.

For each scenario in battery_config.json, generates a deterministic scenario and
runs the Rust solver across three independent, individually-toggleable phases:

  1. DP        – sweeps `batch_sizes`. Skipped entirely if the list is empty.
  2. Online    – runs `online_strategies` (e.g. "bandit", "ant_colony",
                 "singleton_best", "hazard", "random_slotflav", "random_flav").
                 ant_colony, bandit, hazard, singleton_best, random_slotflav, and random_flav sweep
                 `online_batch_sizes` (falls back to `batch_sizes` if absent).
                 Skipped if `online_strategies` is empty.
                 Independent of the DP phase/`batch_sizes` — e.g. set
                 `batch_sizes: []` to sweep online strategies without running DP.
  3. Offline   – runs `additional_strategies` (e.g. "offline_singleton_best",
                 "offline_singleton_best_accurate", "hazard", "ant_colony", "bandit") once per scenario, with no batch-size
                 or DP-phase dependency. Skipped if `additional_strategies` is
                 empty.

The greedy baseline is computed once per scenario (a dedicated lightweight Rust
invocation) whenever at least one phase is enabled, and its cost is then reused
by every phase so "carbon_saving" is comparable across all of them.

Usage (from the repository root):
    python tests/battery/run_battery.py [--config tests/battery/battery_config.json]

Config fields
-------------
trim_future_assignments: bool – optional; when true, extends each scenario's
                                     planning horizon by ERROR_WINDOW_FUTURE,
                                     generates requests only in the configured
                                     total_slots, and trims persisted request
                                     outcomes back to that original horizon.
batch_sizes          : list[int]   – DP phase N values to benchmark; [] skips DP.
online_strategies    : list[str]   – online strategies to run (e.g. "bandit", "ant_colony",
                                     "singleton_best", "hazard", "random_slotflav", "random_flav");
                                     [] skips the online phase.
online_batch_sizes   : list[int]   – N sweep for online_strategies; falls back to
                                     `batch_sizes` when omitted. ant_colony, bandit,
                                     hazard, singleton_best, random_slotflav, and random_flav use
                                     this sweep.
additional_strategies: list[str]   – offline strategies to run once per scenario
                                     (e.g. "offline_singleton_best", "offline_singleton_best_accurate",
                                     "offline_random_slotflav", "offline_random_flav", "hazard",
                                     "ant_colony", "bandit"); [] skips the
                                   offline phase.
                                     "hazard" samples flavours toward the configured mean
                                     error and favours slots with lower forecast carbon cost,
                                     including the next capacity-tier multiplier.
flavours             : list        – optional; overrides Config::default()'s 3 built-in
                                     Accurate/Balanced/Fast flavours for the baseline and
                                     every phase/scenario in this run. Each entry:
                                     {"name": str, "error": float (%), "duration_sec": float}.
                                     Omit or set to null to use nshift's built-in defaults
                                     (see config.rs). Printed at the start of every scenario
                                     (Python side) and at every nshift invocation (Rust stdout,
                                     "Flavours in use: [...]").
aco_n_ants           : int         – optional nshift simulation ACO default; default 40.
aco_n_iterations     : int         – optional nshift simulation ACO default; default 10.
rust_binary_path     : str         – path to the nshift binary (relative to repo root)
output_dir           : str         – where to write run artefacts + battery_results.csv
scenarios            : list        – each entry has:
    id                   : str     – unique label used in CSV and directory names
    seed                 : int     – RNG seed for scenario generation
    requests_per_slot    : float
    total_slots          : int
    input_pattern        : str     – "sinusoidal" (default) or "flat"; flat distributes
                                     requests nearly evenly with low count jitter, preserves
                                     the rounded total, and keeps sub-slot jitter
    carbon_pattern      : str     – "sinusoidal" (default) or "flat", independent of
                                     input_pattern. Flat uses a constant midpoint CI baseline
                                     with deterministic bounded ±10% jitter.
    carbon_request_alignment: str  – sinusoidal-only relationship to the carbon wave:
                                     "in-phase", "phase-opposition", "phase-shifted"
    carbon_phase_shift_fraction: float – shifted-wave offset in [0.0, 0.5]
    request_pattern_start: str     – sinusoidal-only request orientation: "low" or "high"
    capacity_tiers       : list    – optional; falls back to scenarios/scenario_config.py defaults
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ─── path setup ───────────────────────────────────────────────────────────────
CARBONSHIFT_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_DIR = CARBONSHIFT_ROOT / "tests" / "battery" / "scenarios"
SCENARIOS_JSON_DIR = SCENARIOS_DIR / "json"
RUST_CONFIG_RS = CARBONSHIFT_ROOT / "rust" / "src" / "engine" / "config.rs"

sys.path.insert(0, str(SCENARIOS_DIR))

import scenario_config  # noqa: E402  (scenario-generation defaults; after sys.path update)
from scenario_io import generate_scenario_data, load_json, save_json  # noqa: E402

# ─── constants ────────────────────────────────────────────────────────────────
# Fallback defaults for the Rust runtime knobs below, mirroring
# rust/src/engine/config.rs's defaults. battery_config.json can override
# each of these; they also feed the run-output folder name (see
# _format_run_folder_name) and the generated README.md.
DEFAULT_MAX_BATCH_SOLVER_PARALLELISM = 20
DEFAULT_REALTIME_SLOTS = False
DEFAULT_REALTIME_SPEED_SCALE = 0.05
DEFAULT_ACO_N_ANTS = 40
DEFAULT_ACO_N_ITERATIONS = 10
OUTPUT_SCHEMA_VERSION = 2

RESULT_COLUMNS = [
    "scenario_id",
    "batch_size",
    "strategy",
    "solver_time_ms_avg",
    "carbon_cost",
    "final_global_error",
    "reqs_in",
    "reqs_out",
    "reqs_fallback_assigned",
    "baseline_carbon_cost",
    "carbon_saving",
    "peak_concurrent_workers",
    "avg_concurrent_workers",
]

TIMING_COLUMNS = ["scenario_id", "elapsed_seconds"]

PER_N_TIMING_COLUMNS = ["scenario_id", "strategy", "batch_size", "elapsed_seconds"]


# ─── helpers ──────────────────────────────────────────────────────────────────

def _generate_scenario(
    scenario_def: Dict[str, Any],
    output_path: Path,
    trim_future_assignments: bool = False,
) -> Dict[str, Any]:
    """Generate a deterministic scenario, optionally separating input and planning horizons."""
    tiers = scenario_def.get("capacity_tiers", list(scenario_config.CAPACITY_TIERS))
    input_slots = int(scenario_def.get("total_slots", scenario_config.TOTAL_SLOTS))
    total_slots = input_slots
    if trim_future_assignments:
        total_slots += int(scenario_config.ERROR_WINDOW_FUTURE)

    data = generate_scenario_data(
        seed=int(scenario_def.get("seed", 42)),
        total_slots=total_slots,
        request_slots=input_slots if trim_future_assignments else None,
        slot_duration_seconds=float(
            scenario_def.get("slot_duration_seconds", scenario_config.SLOT_DURATION_SECONDS)
        ),
        requests_per_slot=float(
            scenario_def.get("requests_per_slot", scenario_config.PREDICTED_REQUESTS_PER_SLOT)
        ),
        carbon_intensity_cycle_slots=int(scenario_config.CARBON_INTENSITY_CYCLE_SLOTS),
        request_rate_std_factor=float(scenario_config.REQUEST_RATE_STD_FACTOR),
        deadline_min_slack=int(scenario_config.DEADLINE_MIN_SLACK),
        deadline_max_slack=int(scenario_config.DEADLINE_MAX_SLACK),
        error_window_past=int(scenario_config.ERROR_WINDOW_PAST),
        error_window_future=int(scenario_config.ERROR_WINDOW_FUTURE),
        max_error_threshold=float(
            scenario_def.get("max_error_threshold", scenario_config.MAX_ERROR_THRESHOLD),
        ),
        capacity_tiers=tiers,
        carbon_request_alignment=str(
            scenario_def.get(
                "carbon_request_alignment",
                scenario_config.CARBON_REQUEST_ALIGNMENT,
            )
        ),
        carbon_phase_shift_fraction=float(
            scenario_def.get(
                "carbon_phase_shift_fraction",
                scenario_config.CARBON_PHASE_SHIFT_FRACTION,
            )
        ),
        request_pattern_start=str(
            scenario_def.get(
                "request_pattern_start",
                scenario_config.REQUEST_PATTERN_START,
            )
        ),
        input_pattern=str(scenario_def.get("input_pattern", scenario_config.INPUT_PATTERN)),
        carbon_pattern=str(scenario_def.get("carbon_pattern", scenario_config.CARBON_PATTERN)),
    )
    save_json(output_path, data)
    return data


# ─── Rust backend ──────────────────────────────────────────────────────────────

def _write_rust_config(
    scenario_path: Path,
    batch_sizes: List[int],
    output_dir: Path,
    include_baseline: bool,
    additional_strategies: Optional[List[str]] = None,
    online_strategies: Optional[List[str]] = None,
    online_batch_sizes: Optional[List[int]] = None,
    realtime_slots: bool = DEFAULT_REALTIME_SLOTS,
    realtime_speed_scale: float = DEFAULT_REALTIME_SPEED_SCALE,
    max_batch_solver_parallelism: int = DEFAULT_MAX_BATCH_SOLVER_PARALLELISM,
    baseline_total_carbon_cost: Optional[float] = None,
    flavours: Optional[List[Dict[str, Any]]] = None,
    aco_n_ants: int = DEFAULT_ACO_N_ANTS,
    aco_n_iterations: int = DEFAULT_ACO_N_ITERATIONS,
) -> Path:
    """Write a temporary nshift config.json; caller is responsible for deletion."""
    fd, tmp = tempfile.mkstemp(suffix=".json")
    runner: Dict[str, Any] = {
        "flush_partial_batch": True,
        "include_greedy_baseline": include_baseline,
        "realtime_slots": realtime_slots,
        "realtime_speed_scale": realtime_speed_scale,
        "max_batch_solver_parallelism": max_batch_solver_parallelism,
        "aco_n_ants": aco_n_ants,
        "aco_n_iterations": aco_n_iterations,
    }
    if flavours:
        runner["flavours"] = flavours
    if not include_baseline and baseline_total_carbon_cost is not None:
        # Reuse a baseline computed by an earlier invocation (the dedicated
        # baseline-only run — see _run_baseline_only) so this run's own
        # "saving_vs_baseline" stdout/CSV fields are populated instead of
        # silently staying at 0 (this run never recomputes the baseline).
        runner["baseline_total_carbon_cost"] = baseline_total_carbon_cost
    if additional_strategies:
        runner["additional_strategies"] = additional_strategies
    if online_strategies:
        runner["online_strategies"] = online_strategies
    if online_batch_sizes:
        runner["online_batch_sizes"] = online_batch_sizes
    with os.fdopen(fd, "w") as f:
        json.dump(
            {
                "batch_sizes": batch_sizes,
                "scenario_path": str(scenario_path),
                "output_dir": str(output_dir),
                "rust_output_dir": str(output_dir),
                "runner": runner,
            },
            f,
        )
    return Path(tmp)


def _run_rust_binary(rust_binary: Path, tmp_config: Path) -> None:
    """Run nshift and normalize outputs when the scenario records a trim horizon."""
    try:
        with open(tmp_config, encoding="utf-8") as f:
            run_config = json.load(f)
        subprocess.run([str(rust_binary), "--config", str(tmp_config)], check=True)
        scenario = load_json(Path(run_config["scenario_path"]))
        metadata = scenario["metadata"]
        trim_after_slot = metadata.get("input_total_slots")
        if trim_after_slot is not None:
            output_dir = Path(run_config["output_dir"])
            baseline_dir = (
                output_dir
                if output_dir.name == "rust_baseline"
                else output_dir.parent / "rust_baseline"
            )
            baseline_request_path = baseline_dir / "baseline_greedy" / "per_request.csv"
            baseline_request_costs = _read_request_costs(baseline_request_path)
            flavour_errors = _read_flavour_errors(output_dir)
            _trim_rust_outputs(
                output_dir,
                int(trim_after_slot),
                int(metadata["error_window_past"]),
                int(metadata["error_window_future"]),
                baseline_request_costs,
                flavour_errors,
            )
    finally:
        tmp_config.unlink(missing_ok=True)


def _read_csv(path: Path) -> tuple[List[str], List[Dict[str, str]]]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return reader.fieldnames, list(reader)


def _write_csv(path: Path, fieldnames: List[str], rows: List[Dict[str, Any]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_output_schema_manifest(output_dir: Path, schema_name: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "schema_name": schema_name,
    }
    (output_dir / "output_schema.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_request_costs(path: Path) -> Dict[int, float]:
    if not path.is_file():
        raise FileNotFoundError(f"Baseline per-request costs are required for horizon trimming: {path}")
    _, rows = _read_csv(path)
    return {
        int(row["request_id"]): float(row["carbon_cost"])
        for row in rows
    }


def _read_flavour_errors(path: Path) -> Dict[tuple[str, str], float]:
    catalog_path = path / "qos_profile_flavours.csv"
    if not catalog_path.is_file():
        raise FileNotFoundError(f"QoS profile flavour catalog is required: {catalog_path}")
    _, rows = _read_csv(catalog_path)
    return {
        (row["profile_id"], row["flavour_name"]): float(row["error"])
        for row in rows
    }


def _is_true(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true"}


def _average(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _request_summary_metrics(
    rows: List[Dict[str, str]],
    summary: Dict[str, str],
    baseline_request_costs: Dict[int, float],
    flavour_errors: Dict[tuple[str, str], float],
) -> Dict[str, Any]:
    request_ids = [int(row["request_id"]) for row in rows]
    if len(request_ids) != len(set(request_ids)):
        raise ValueError("Per-request output contains duplicate request IDs")
    missing_baselines = set(request_ids) - baseline_request_costs.keys()
    if missing_baselines:
        raise ValueError(
            f"Baseline per-request output is missing retained request IDs: "
            f"{sorted(missing_baselines)[:10]}"
        )

    count = len(rows)
    costs = [float(row["carbon_cost"]) for row in rows]
    errors = [
        flavour_errors[(row["qos_profile_id"], row["flavour_name"])]
        for row in rows
    ]
    queue_waits = [float(row["queue_wait_seconds"]) for row in rows]
    final_waits = [float(row["final_wait_seconds"]) for row in rows]
    lateness = [
        max(0, int(row["scheduled_slot"]) - int(row["deadline_slot"]))
        for row in rows
    ]
    carbon_cost = sum(costs)
    baseline_cost = sum(baseline_request_costs[request_id] for request_id in request_ids)

    def wait_metrics(name: str, values: List[float]) -> Dict[str, float]:
        return {
            f"{name}_min": min(values) if values else 0.0,
            f"{name}_max": max(values) if values else 0.0,
            f"{name}_avg": _average(values),
        }

    return {
        "reqs_scheduled": count,
        "reqs_unscheduled": max(0, int(summary["reqs_total"]) - count),
        "reqs_late": sum(_is_true(row["late_assigned"]) for row in rows),
        "max_lateness_slots": max(lateness, default=0),
        "carbon_cost": carbon_cost,
        "global_average_error": _average(errors),
        "reqs_fallback_assigned": sum(
            _is_true(row["fallback_assigned"]) for row in rows
        ),
        **wait_metrics("queue_wait_seconds", queue_waits),
        **wait_metrics("final_wait_seconds", final_waits),
        "baseline_carbon_cost": baseline_cost,
        "carbon_cost_saving_vs_baseline": baseline_cost - carbon_cost,
        "carbon_saving": (
            (baseline_cost - carbon_cost) / baseline_cost * 100.0
            if baseline_cost > 0.0
            else 0.0
        ),
    }


def _write_summary(path: Path, metrics: Dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        summary = json.loads(path.read_text(encoding="utf-8"))
        summary.update(metrics)
        path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        return

    fieldnames, rows = _read_csv(path)
    for row in rows:
        row.update({key: str(value) for key, value in metrics.items() if key in fieldnames})
    _write_csv(path, fieldnames, rows)


def _trim_rust_outputs(
    output_dir: Path,
    trim_after_slot: int,
    error_window_past: int,
    error_window_future: int,
    baseline_request_costs: Dict[int, float],
    flavour_errors: Dict[tuple[str, str], float],
) -> None:
    """Keep request outcomes and derived metrics within the original input horizon."""
    for request_path in output_dir.rglob("per_request.csv"):
        run_dir = request_path.parent
        _, request_rows = _read_csv(request_path)
        kept_rows = [
            row for row in request_rows
            if 0 <= int(row["scheduled_slot"]) < trim_after_slot
        ]
        summary_path = run_dir / "summary.csv"
        _, summary_rows = _read_csv(summary_path)
        if len(summary_rows) != 1:
            raise ValueError(f"Expected one summary row in {summary_path}")
        metrics = _request_summary_metrics(
            kept_rows,
            summary_rows[0],
            baseline_request_costs,
            flavour_errors,
        )
        request_fields, _ = _read_csv(request_path)
        _write_csv(request_path, request_fields, kept_rows)

        request_json_path = run_dir / "per_request.json"
        request_json = json.loads(request_json_path.read_text(encoding="utf-8"))
        request_json["rows"] = [
            row for row in request_json["rows"]
            if 0 <= int(row["scheduled_slot"]) < trim_after_slot
        ]
        if len(request_json["rows"]) != len(kept_rows):
            raise ValueError(f"CSV/JSON per-request outputs disagree in {run_dir}")
        request_json_path.write_text(
            json.dumps(request_json, indent=2) + "\n",
            encoding="utf-8",
        )

        for assignment_path in run_dir.glob("assignments_runtime.csv"):
            assignment_fields, assignment_rows = _read_csv(assignment_path)
            _write_csv(
                assignment_path,
                assignment_fields,
                [
                    row for row in assignment_rows
                    if 0 <= int(row["scheduled_slot"]) < trim_after_slot
                ],
            )

        errors_by_slot: Dict[int, List[float]] = {}
        for row in kept_rows:
            error = flavour_errors[(row["qos_profile_id"], row["flavour_name"])]
            errors_by_slot.setdefault(int(row["scheduled_slot"]), []).append(error)
        per_timeslot_rows = []
        for slot in range(trim_after_slot):
            window_start = slot - error_window_past
            window_end = slot + error_window_future
            window_errors = [
                error
                for scheduled_slot, errors in errors_by_slot.items()
                if window_start <= scheduled_slot <= window_end
                for error in errors
            ]
            average_error = _average(window_errors)
            per_timeslot_rows.append({
                "timeslot": slot,
                "window_start": window_start,
                "window_end": window_end,
                "reqs_count": len(window_errors),
                "window_avg_error": average_error,
            })
        per_timeslot_fields = [
            "timeslot",
            "window_start",
            "window_end",
            "reqs_count",
            "window_avg_error",
        ]
        _write_csv(run_dir / "per_timeslot.csv", per_timeslot_fields, per_timeslot_rows)
        (run_dir / "per_timeslot.json").write_text(
            json.dumps({"rows": per_timeslot_rows}, indent=2) + "\n",
            encoding="utf-8",
        )
        _write_summary(summary_path, metrics, json_output=False)
        _write_summary(run_dir / "summary.json", metrics, json_output=True)

    for summary_path in output_dir.rglob("summary_by_n.csv"):
        fieldnames, rows = _read_csv(summary_path)
        for row in rows:
            n_summary_path = summary_path.parent / f"N{int(row['batch_size'])}" / "summary.csv"
            _, n_rows = _read_csv(n_summary_path)
            if len(n_rows) != 1:
                raise ValueError(f"Expected one summary row in {n_summary_path}")
            row.update({key: n_rows[0][key] for key in fieldnames if key in n_rows[0]})
        _write_csv(summary_path, fieldnames, rows)

    baseline_summary_path = output_dir / "baseline_summary.csv"
    baseline_run_dir = output_dir / "baseline_greedy"
    if baseline_summary_path.is_file() and (baseline_run_dir / "summary.csv").is_file():
        fieldnames, rows = _read_csv(baseline_summary_path)
        _, source_rows = _read_csv(baseline_run_dir / "summary.csv")
        if len(source_rows) != 1:
            raise ValueError(f"Expected one summary row in {baseline_run_dir / 'summary.csv'}")
        for row in rows:
            row.update({key: source_rows[0][key] for key in fieldnames if key in source_rows[0]})
        _write_csv(baseline_summary_path, fieldnames, rows)
        (output_dir / "baseline_summary.json").write_text(
            (baseline_run_dir / "summary.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )


def _make_result_row(
    scenario_id: str,
    batch_size: int,
    strategy: str,
    row: Dict[str, Any],
    baseline_cost: float,
) -> Dict[str, Any]:
    """Build one `RESULT_COLUMNS`-shaped row from a parsed Rust summary CSV row.

    Shared by the DP/online/offline phases so each only supplies what differs
    (strategy label, batch size, and summary row).
    """
    carbon = float(row.get("carbon_cost", 0.0))
    baseline_value = row.get("baseline_carbon_cost")
    row_baseline_cost = (
        float(baseline_value)
        if baseline_value not in (None, "")
        else baseline_cost
    )
    savings_pct = (
        (row_baseline_cost - carbon) / row_baseline_cost * 100.0
        if row_baseline_cost > 0.0
        else 0.0
    )
    return {
        "scenario_id": scenario_id,
        "batch_size": batch_size,
        "strategy": strategy,
        "solver_time_ms_avg": float(row.get("solver_time_ms_avg", 0.0)),
        "carbon_cost": carbon,
        "final_global_error": float(row.get("global_average_error", 0.0)),
        "reqs_in": int(row.get("reqs_total", 0)),
        "reqs_out": int(row.get("reqs_scheduled", row.get("reqs_total", 0))),
        "reqs_fallback_assigned": int(row.get("reqs_fallback_assigned", 0)),
        "baseline_carbon_cost": row_baseline_cost,
        "carbon_saving": savings_pct,
        "peak_concurrent_workers": int(row.get("peak_concurrent_workers", 0)),
        "avg_concurrent_workers": float(row.get("avg_concurrent_workers", 0.0)),
    }


def _format_carbon_saving(result_row: Dict[str, Any]) -> str:
    saving = result_row["baseline_carbon_cost"] - result_row["carbon_cost"]
    return f"{saving:.3f} gCO2 ({result_row['carbon_saving']:.1f}%)"


def _run_baseline_only(
    scenario_path: Path,
    output_dir: Path,
    rust_binary: Path,
    realtime_slots: bool = DEFAULT_REALTIME_SLOTS,
    realtime_speed_scale: float = DEFAULT_REALTIME_SPEED_SCALE,
    max_batch_solver_parallelism: int = DEFAULT_MAX_BATCH_SOLVER_PARALLELISM,
    flavours: Optional[List[Dict[str, Any]]] = None,
    aco_n_ants: int = DEFAULT_ACO_N_ANTS,
    aco_n_iterations: int = DEFAULT_ACO_N_ITERATIONS,
) -> float:
    """Compute the greedy baseline once via a dedicated, lightweight Rust
    invocation (no DP/online/offline work) so every phase can reuse the same
    cost for its "carbon_saving" field, independent of which phases run."""
    baseline_dir = output_dir / "rust_baseline"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    tmp_config = _write_rust_config(
        scenario_path, [], baseline_dir, True,
        realtime_slots=realtime_slots,
        realtime_speed_scale=realtime_speed_scale,
        max_batch_solver_parallelism=max_batch_solver_parallelism,
        flavours=flavours,
        aco_n_ants=aco_n_ants,
        aco_n_iterations=aco_n_iterations,
    )
    _run_rust_binary(rust_binary, tmp_config)

    baseline_csv = baseline_dir / "baseline_summary.csv"
    if not baseline_csv.exists():
        print(f"    WARNING: {baseline_csv} not found; baseline cost defaults to 0.0")
        return 0.0
    with open(baseline_csv, newline="") as f:
        rows_csv = list(csv.DictReader(f))
    baseline_cost = float(rows_csv[0].get("carbon_cost", 0.0)) if rows_csv else 0.0
    print(f"    [rust/baseline]: carbon={baseline_cost:.3f}")
    return baseline_cost


def _run_dp_phase(
    scenario_path: Path,
    scenario_id: str,
    batch_sizes: List[int],
    output_dir: Path,
    rust_binary: Path,
    baseline_cost: float,
    realtime_slots: bool = DEFAULT_REALTIME_SLOTS,
    realtime_speed_scale: float = DEFAULT_REALTIME_SPEED_SCALE,
    max_batch_solver_parallelism: int = DEFAULT_MAX_BATCH_SOLVER_PARALLELISM,
    flavours: Optional[List[Dict[str, Any]]] = None,
    aco_n_ants: int = DEFAULT_ACO_N_ANTS,
    aco_n_iterations: int = DEFAULT_ACO_N_ITERATIONS,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Sweep DP batch sizes. The baseline is precomputed and passed in."""
    all_rows: List[Dict[str, Any]] = []
    per_n_timing_rows: List[Dict[str, Any]] = []

    dp_dir = output_dir / "rust_dp"
    dp_dir.mkdir(parents=True, exist_ok=True)
    tmp_config = _write_rust_config(
        scenario_path,
        batch_sizes,
        dp_dir,
        False,
        realtime_slots=realtime_slots,
        realtime_speed_scale=realtime_speed_scale,
        max_batch_solver_parallelism=max_batch_solver_parallelism,
        baseline_total_carbon_cost=baseline_cost,
        flavours=flavours,
        aco_n_ants=aco_n_ants,
        aco_n_iterations=aco_n_iterations,
    )
    _run_rust_binary(rust_binary, tmp_config)

    summary_csv = dp_dir / "summary_by_n.csv"
    if not summary_csv.exists():
        print(f"    WARNING: {summary_csv} not found; skipping DP results")
        return all_rows, per_n_timing_rows
    with open(summary_csv, newline="") as f:
        for row in csv.DictReader(f):
            n = int(row["batch_size"])
            result_row = _make_result_row(scenario_id, n, "dp", row, baseline_cost)
            all_rows.append(result_row)
            run_elapsed = float(row.get("run_elapsed_seconds", 0.0))
            per_n_timing_rows.append({
                "scenario_id": scenario_id, "strategy": "dp", "batch_size": n,
                "elapsed_seconds": round(run_elapsed, 3),
            })
            print(
                f"    [rust/dp] N={n}: "
                f"solver_ms={result_row['solver_time_ms_avg']:.1f}, "
                f"carbon={result_row['carbon_cost']:.3f}, "
                f"error={result_row['final_global_error']:.3f}, "
                f"saving={_format_carbon_saving(result_row)}, "
                f"elapsed={run_elapsed:.1f}s"
            )
    return all_rows, per_n_timing_rows


def _run_online_phase(
    scenario_path: Path,
    scenario_id: str,
    online_strategies: List[str],
    output_dir: Path,
    rust_binary: Path,
    baseline_cost: float,
    online_batch_sizes: Optional[List[int]] = None,
    realtime_slots: bool = DEFAULT_REALTIME_SLOTS,
    realtime_speed_scale: float = DEFAULT_REALTIME_SPEED_SCALE,
    max_batch_solver_parallelism: int = DEFAULT_MAX_BATCH_SOLVER_PARALLELISM,
    flavours: Optional[List[Dict[str, Any]]] = None,
    aco_n_ants: int = DEFAULT_ACO_N_ANTS,
    aco_n_iterations: int = DEFAULT_ACO_N_ITERATIONS,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Run `online_strategies` once, independent of the DP phase (`batch_sizes`
    is always `[]` for this invocation — see `online_batch_sizes` instead).
    Caller ensures `online_strategies` is non-empty."""
    all_rows: List[Dict[str, Any]] = []
    per_n_timing_rows: List[Dict[str, Any]] = []

    online_dir = output_dir / "rust_online"
    online_dir.mkdir(parents=True, exist_ok=True)
    tmp_config = _write_rust_config(
        scenario_path, [], online_dir, False,
        online_strategies=online_strategies,
        online_batch_sizes=online_batch_sizes,
        realtime_slots=realtime_slots,
        realtime_speed_scale=realtime_speed_scale,
        max_batch_solver_parallelism=max_batch_solver_parallelism,
        baseline_total_carbon_cost=baseline_cost,
        flavours=flavours,
        aco_n_ants=aco_n_ants,
        aco_n_iterations=aco_n_iterations,
    )
    _run_rust_binary(rust_binary, tmp_config)

    for strategy in online_strategies:
        summary_csv = online_dir / f"online_{strategy}" / "summary_by_n.csv"
        if not summary_csv.exists():
            print(f"    WARNING: {summary_csv} not found; skipping online strategy={strategy}")
            continue
        strategy_label = f"online_{strategy}"
        with open(summary_csv, newline="") as f:
            rows_csv = list(csv.DictReader(f))
        for row in rows_csv:
            n = int(row["batch_size"])
            result_row = _make_result_row(scenario_id, n, strategy_label, row, baseline_cost)
            all_rows.append(result_row)
            run_elapsed = float(row.get("run_elapsed_seconds", 0.0))
            per_n_timing_rows.append({
                "scenario_id": scenario_id, "strategy": strategy_label, "batch_size": n,
                "elapsed_seconds": round(run_elapsed, 3),
            })
            print(
                f"    [rust/online_{strategy}] N={n}: "
                f"solver_ms={result_row['solver_time_ms_avg']:.1f}, "
                f"carbon={result_row['carbon_cost']:.3f}, "
                f"error={result_row['final_global_error']:.3f}, "
                f"saving={_format_carbon_saving(result_row)}, "
                f"elapsed={run_elapsed:.1f}s"
            )
    return all_rows, per_n_timing_rows


def _run_offline_phase(
    scenario_path: Path,
    scenario_id: str,
    additional_strategies: List[str],
    output_dir: Path,
    rust_binary: Path,
    baseline_cost: float,
    realtime_slots: bool = DEFAULT_REALTIME_SLOTS,
    realtime_speed_scale: float = DEFAULT_REALTIME_SPEED_SCALE,
    max_batch_solver_parallelism: int = DEFAULT_MAX_BATCH_SOLVER_PARALLELISM,
    flavours: Optional[List[Dict[str, Any]]] = None,
    aco_n_ants: int = DEFAULT_ACO_N_ANTS,
    aco_n_iterations: int = DEFAULT_ACO_N_ITERATIONS,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Run `additional_strategies` once per scenario, independently of DP batches."""
    all_rows: List[Dict[str, Any]] = []
    per_n_timing_rows: List[Dict[str, Any]] = []

    strat_dir = output_dir / "rust_offline"
    strat_dir.mkdir(parents=True, exist_ok=True)
    tmp_config = _write_rust_config(
        scenario_path, [], strat_dir, False,
        additional_strategies=additional_strategies,
        realtime_slots=realtime_slots,
        realtime_speed_scale=realtime_speed_scale,
        max_batch_solver_parallelism=max_batch_solver_parallelism,
        baseline_total_carbon_cost=baseline_cost,
        flavours=flavours,
        aco_n_ants=aco_n_ants,
        aco_n_iterations=aco_n_iterations,
    )
    _run_rust_binary(rust_binary, tmp_config)

    for strategy in additional_strategies:
        output_strategy_name = strategy.removeprefix("offline_")
        strategy_dir = strat_dir / f"strategy_{output_strategy_name}"
        summary_csv = strategy_dir / "summary.csv"
        if not summary_csv.exists():
            print(f"    WARNING: {summary_csv} not found; skipping strategy={strategy}")
            continue
        strategy_label = (
            strategy if strategy.startswith("offline_") else f"offline_{strategy}"
        )
        with open(summary_csv, newline="") as f:
            rows_csv = list(csv.DictReader(f))
        for row in rows_csv:
            result_row = _make_result_row(scenario_id, 0, strategy_label, row, baseline_cost)
            all_rows.append(result_row)
            run_elapsed = float(row.get("run_elapsed_seconds", 0.0))
            per_n_timing_rows.append({
                "scenario_id": scenario_id, "strategy": strategy_label, "batch_size": 0,
                "elapsed_seconds": round(run_elapsed, 3),
            })
            print(
                f"    [rust/{strategy_label}]: "
                f"carbon={result_row['carbon_cost']:.3f}, "
                f"error={result_row['final_global_error']:.3f}, "
                f"saving={_format_carbon_saving(result_row)}, "
                f"elapsed={run_elapsed:.1f}s"
            )
    return all_rows, per_n_timing_rows


# ─── run-output packaging (folder name, config snapshot, README) ──────────────

def _format_run_folder_name(
    battery_id: str,
    start_dt: datetime,
    max_batch_solver_parallelism: int,
    realtime_slots: bool,
    realtime_speed_scale: float,
) -> str:
    """Build the per-run results subfolder name.

    Format: <battery_id>_<mmdd>_<hhmm>_<parallelism>[_<realtime_scale>]
    The configured realtime scale (with its decimal point stripped) is included
    only when realtime slot pacing is enabled.
    """
    mmdd = start_dt.strftime("%m%d")
    hhmm = start_dt.strftime("%H%M")
    parts = [battery_id, mmdd, hhmm, str(max_batch_solver_parallelism)]
    if realtime_slots:
        parts.append(str(realtime_speed_scale).replace(".", ""))
    return "_".join(parts)


# Rust scalar Config fields worth surfacing in the run README, mapped to a
# human-readable label. Only single-line `field: value,` entries inside
# `impl Default for Config` are matched (see _parse_rust_config_defaults),
# so multi-line fields like `flavours`/`capacity_tiers` are intentionally
# left out here.
_RUST_CONFIG_README_FIELDS: List[tuple[str, str]] = [
    ("batch_size", "DP batch size (N)"),
    ("max_error_threshold", "Max error threshold (%)"),
    ("error_window_past", "Error window – past slots"),
    ("error_window_future", "Error window – future slots"),
    ("error_window_past_decay_slots", "Error window – past decay slots"),
    ("dp_pruning_method", "DP pruning method"),
    ("dp_pruning_min_batch_size", "DP pruning min batch size"),
    ("dp_pruning_k", "DP pruning k"),
    ("dp_timeout", "DP solver timeout (s)"),
]


def _parse_rust_config_defaults(config_rs_path: Path) -> Dict[str, str]:
    """Extract scalar defaults from Config and its concern-specific groups.

    This is intentionally a simple line-based scan (not a Rust parser): it only
    captures top-level single-line `name: value,` assignments from the `Self`
    initializer in each selected Default implementation. Nested defaults are
    merged after `Config` so simulation and solver settings are also available
    to the run README.
    """
    if not config_rs_path.exists():
        return {}
    text = config_rs_path.read_text()
    values: Dict[str, str] = {}

    for type_name in ("Config", "SolverConfig", "SimulationConfig"):
        start = text.find(f"impl Default for {type_name}")
        if start == -1:
            continue
        initializer = re.search(r"(?m)^\s*Self\s*\{\s*$", text[start:])
        if initializer is None:
            continue

        body = text[start + initializer.end() :]
        brace_depth = 1
        for line in body.splitlines():
            stripped = line.strip()
            if brace_depth == 1 and stripped and "[" not in stripped and "]" not in stripped:
                match = re.match(r"^(\w+):\s*(.+?),\s*(?://.*)?$", stripped)
                if match:
                    value = match.group(2).strip().removesuffix(".to_string()")
                    values[match.group(1)] = value
            brace_depth += stripped.count("{") - stripped.count("}")
            if brace_depth == 0:
                break
    return values


def _row_label(row: Dict[str, Any]) -> str:
    """Human-readable identifier for a results row: 'N=<n>' or an offline/online strategy name."""
    mode = row["strategy"]
    if mode.startswith("offline_singleton_"):
        return mode
    if mode.startswith("offline_"):
        return mode[len("offline_"):]
    if mode.startswith("online_"):
        return f"{mode[len('online_'):]} (N={row['batch_size']})"
    return f"N={row['batch_size']}"


def _top3_markdown_table(
    rows: List[Dict[str, Any]],
    elapsed_lookup: Dict[tuple, float],
    scenario_id: str,
) -> str:
    """Markdown table of the 3 lowest-carbon-cost rows, or a placeholder if empty."""
    if not rows:
        return "_no executions in this category_\n"
    top3 = sorted(rows, key=lambda r: r["carbon_cost"])[:3]
    lines = [
        "| Config | Carbon saving (%) | Carbon cost | Solver ms (avg) | Elapsed (s) |",
        "|---|---|---|---|---|",
    ]
    for r in top3:
        key = (scenario_id, r["strategy"], r["batch_size"])
        elapsed = elapsed_lookup.get(key)
        elapsed_str = f"{elapsed:.1f}" if elapsed is not None else "n/a"
        lines.append(
            f"| {_row_label(r)} | {r['carbon_saving']:.2f} | "
            f"{r['carbon_cost']:.3f} | {r['solver_time_ms_avg']:.1f} | {elapsed_str} |"
        )
    return "\n".join(lines) + "\n"


def _build_top3_section(
    all_rows: List[Dict[str, Any]], per_n_timing_rows: List[Dict[str, Any]]
) -> str:
    """Per-scenario TOP-3 (lowest carbon cost) tables for relaxed-retry, online and
    offline alternative-strategy executions."""
    elapsed_lookup: Dict[tuple, float] = {
        (r["scenario_id"], r["strategy"], r["batch_size"]): r["elapsed_seconds"]
        for r in per_n_timing_rows
    }
    scenario_ids = sorted({r["scenario_id"] for r in all_rows})
    categories = [
        (
            "DP scheduling runs — top 3 by carbon cost",
            lambda m: not m.startswith("online_") and not m.startswith("offline_"),
        ),
        ("Online alternative strategies — top 3 by carbon cost", lambda m: m.startswith("online_")),
        ("Offline alternative strategies — top 3 by carbon cost", lambda m: m.startswith("offline_")),
    ]
    sections = ["## Top executions per scenario\n"]
    for sid in scenario_ids:
        sections.append(f"### {sid}\n")
        rows_for_scenario = [r for r in all_rows if r["scenario_id"] == sid]
        for title, predicate in categories:
            matched = [r for r in rows_for_scenario if predicate(r["strategy"])]
            sections.append(f"**{title}**\n")
            sections.append(_top3_markdown_table(matched, elapsed_lookup, sid))
    return "\n".join(sections)


def _write_run_readme(
    run_dir: Path,
    battery_id: str,
    start_dt: datetime,
    max_batch_solver_parallelism: int,
    realtime_slots: bool,
    realtime_speed_scale: float,
    alt_strategies_enabled: bool,
    batch_sizes: List[int],
    additional_strategies: List[str],
    online_strategies: List[str],
    online_batch_sizes: Optional[List[int]],
    input_patterns: List[str],
    carbon_patterns: List[str],
    all_rows: List[Dict[str, Any]],
    per_n_timing_rows: List[Dict[str, Any]],
    battery_elapsed: float,
    trim_future_assignments: bool = False,
) -> None:
    rust_defaults = _parse_rust_config_defaults(RUST_CONFIG_RS)
    decay_slots = rust_defaults.get("error_window_past_decay_slots", "0")
    decay_desc = "disabled" if decay_slots == "0" else f"enabled ({decay_slots} additional slots)"

    lines: List[str] = [
        f"# Battery run: {battery_id}",
        "",
        f"- Started: {start_dt.isoformat(timespec='seconds')}",
        f"- Total elapsed: {battery_elapsed:.1f}s",
        "- Solver: Rust (nshift)",
        f"- Simulation input pattern(s): {', '.join(input_patterns)}",
        f"- Carbon-intensity pattern(s): {', '.join(carbon_patterns)}",
        "",
        "## Run-identifying parameters (also encoded in the folder name)",
        "",
        f"- Battery id: `{battery_id}`",
        f"- Start date: {start_dt.strftime('%Y-%m-%d')}",
        f"- Start time: {start_dt.strftime('%H:%M')}",
        (
            f"- Future-slot trimming: enabled; planning horizon extended by "
            f"{scenario_config.ERROR_WINDOW_FUTURE} slots, with request outcomes "
            "and request-level metrics retained only for assignments in the original horizon."
            if trim_future_assignments
            else "- Future-slot trimming: disabled (scenario planning horizon unchanged)."
        ),
        "- `reqs_in` is the input request count; `reqs_out` is the count assigned "
        "within the original horizon.",
        f"- Max batch solver parallelism (max concurrent DP workers): {max_batch_solver_parallelism}",
        (
            f"- Realtime slot pacing: enabled, speed scale={realtime_speed_scale}"
            if realtime_slots else
            "- Realtime slot pacing: disabled (slots advance instantly)"
        ),
        f"- Alternative strategies enabled: {'yes' if alt_strategies_enabled else 'no'}",
        "",
        "## Other key runtime parameters",
        "",
        f"- Error window: {rust_defaults.get('error_window_past', '?')} past / "
        f"{rust_defaults.get('error_window_future', '?')} future slots",
        f"- Error window past decay: {decay_desc}",
        f"- DP pruning method: {rust_defaults.get('dp_pruning_method', '?')}",
        f"- DP pruning k: {rust_defaults.get('dp_pruning_k', '?')}",
        f"- DP pruning min batch size: {rust_defaults.get('dp_pruning_min_batch_size', '?')}",
        f"- Max error threshold: {rust_defaults.get('max_error_threshold', '?')}%",
        f"- DP solver timeout: {rust_defaults.get('dp_timeout', '?')}s",
        "",
        "## Enabled phases",
        "",
        f"- DP phase: {'enabled' if batch_sizes else 'disabled'} "
        f"— batch sizes (N): {batch_sizes or 'none'}",
        f"- Online phase: {'enabled' if online_strategies else 'disabled'} "
        f"— strategies: {online_strategies or 'none'}, "
        f"batch sizes: {online_batch_sizes or ('falls back to DP batch_sizes' if online_strategies else 'n/a')}",
        f"- Offline phase: {'enabled' if additional_strategies else 'disabled'} "
        f"— strategies: {additional_strategies or 'none'} (run once per scenario)",
        "",
        "_Full Rust defaults are in the `config.rs` snapshot copied into this folder; "
        "values above reflect `Config::default()` possibly overridden by this run's "
        "battery_config.json / scenario metadata._",
        "",
    ]
    lines.append(_build_top3_section(all_rows, per_n_timing_rows))

    (run_dir / "README.md").write_text("\n".join(lines) + "\n")


# ─── Main orchestrator ─────────────────────────────────────────────────────────

def run_battery(config_path: Path) -> None:
    with open(config_path) as f:
        cfg = json.load(f)

    start_dt = datetime.now()
    battery_id: str = cfg.get("battery_id", "run")
    base_output_dir = Path(cfg.get("output_dir", "results"))
    if not base_output_dir.is_absolute():
        base_output_dir = (CARBONSHIFT_ROOT / base_output_dir).resolve()

    # batch_sizes=[] skips the DP phase entirely (see module docstring).
    batch_sizes: List[int] = [int(n) for n in cfg.get("batch_sizes", [])]
    additional_strategies: List[str] = cfg.get("additional_strategies", [])
    online_strategies: List[str] = cfg.get("online_strategies", [])
    online_batch_sizes_raw = cfg.get("online_batch_sizes")
    online_batch_sizes: Optional[List[int]] = (
        [int(n) for n in online_batch_sizes_raw] if online_batch_sizes_raw else None
    )

    run_dp = bool(batch_sizes)
    run_online = bool(online_strategies)
    run_offline = bool(additional_strategies)

    # Rust runtime knobs (also drive the run folder name below).
    max_batch_solver_parallelism = int(
        cfg.get("max_batch_solver_parallelism", DEFAULT_MAX_BATCH_SOLVER_PARALLELISM)
    )
    aco_n_ants = int(cfg.get("aco_n_ants", DEFAULT_ACO_N_ANTS))
    aco_n_iterations = int(cfg.get("aco_n_iterations", DEFAULT_ACO_N_ITERATIONS))
    realtime_slots = bool(cfg.get("realtime_slots", DEFAULT_REALTIME_SLOTS))
    realtime_speed_scale = float(cfg.get("realtime_speed_scale", DEFAULT_REALTIME_SPEED_SCALE))
    trim_future_assignments = bool(cfg.get("trim_future_assignments", False))
    alt_strategies_enabled = run_online or run_offline

    # Optional override of Config::default()'s 3 built-in flavours (Accurate/
    # Balanced/Fast) — a list of {"name", "error", "duration"} dicts. None
    # (the field is simply absent) keeps nshift's own built-in defaults.
    flavours: Optional[List[Dict[str, Any]]] = cfg.get("flavours")

    run_folder_name = _format_run_folder_name(
        battery_id, start_dt, max_batch_solver_parallelism,
        realtime_slots, realtime_speed_scale,
    )
    output_dir = base_output_dir / run_folder_name
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_output_schema_manifest(output_dir, "carbonshift.battery-run")

    # Snapshot the exact battery config and Rust config used for this run.
    shutil.copy2(config_path, output_dir / config_path.name)
    if RUST_CONFIG_RS.exists():
        shutil.copy2(RUST_CONFIG_RS, output_dir / "config.rs")
    else:
        print(f"WARNING: {RUST_CONFIG_RS} not found; skipping config.rs snapshot.")

    raw = cfg.get("rust_binary_path", "rust/target/release/nshift")
    rust_binary = Path(raw) if Path(raw).is_absolute() else (CARBONSHIFT_ROOT / raw).resolve()
    if not rust_binary.exists():
        print(f"WARNING: Rust binary not found at {rust_binary}.")
        print("         Build it first:  cd rust && cargo build --release --bin nshift")
        sys.exit(1)

    all_rows: List[Dict[str, Any]] = []
    timing_rows: List[Dict[str, Any]] = []
    per_n_timing_rows: List[Dict[str, Any]] = []
    battery_t0 = time.monotonic()

    for scenario_def in cfg["scenarios"]:
        sid: str = scenario_def["id"]
        seed: int = int(scenario_def.get("seed", 42))
        total_slots: int = int(scenario_def.get("total_slots", 24))
        req_per_slot: float = float(scenario_def.get("requests_per_slot", scenario_config.PREDICTED_REQUESTS_PER_SLOT))
        input_pattern = str(scenario_def.get("input_pattern", scenario_config.INPUT_PATTERN))
        carbon_pattern = str(
            scenario_def.get("carbon_pattern", scenario_config.CARBON_PATTERN)
        )
        planning_slots = total_slots + (
            int(scenario_config.ERROR_WINDOW_FUTURE) if trim_future_assignments else 0
        )

        print(f"\n{'='*60}")
        print(
            f"Scenario: {sid}  (seed={seed}, slots={total_slots}, planning_slots={planning_slots}, "
            f"req/slot={req_per_slot}, input_pattern={input_pattern}, "
            f"carbon_pattern={carbon_pattern})"
        )
        print("=" * 60)
        if flavours:
            flavour_desc = ", ".join(
                f"{f['name']}(error={f['error']}%, duration={f['duration_sec']}s)"
                for f in flavours
            )
            print(f"  Flavours (override): {flavour_desc}")
        else:
            print("  Flavours: nshift built-in defaults (Accurate/Balanced/Fast — "
                  "see config.rs snapshot in this run's output; set battery.json's "
                  "top-level \"flavours\" to override)")

        scenario_t0 = time.monotonic()
        scenario_dir = output_dir / sid
        scenario_dir.mkdir(parents=True, exist_ok=True)
        SCENARIOS_JSON_DIR.mkdir(parents=True, exist_ok=True)
        scenario_path = SCENARIOS_JSON_DIR / f"{sid}.json"
        scenario_data = _generate_scenario(
            scenario_def,
            scenario_path,
            trim_future_assignments=trim_future_assignments,
        )
        save_json(scenario_dir / f"{sid}.json", scenario_data)

        if not (run_dp or run_online or run_offline):
            print("  WARNING: no phase enabled (batch_sizes, "
                  "online_strategies and additional_strategies are all empty); skipping.")
            continue

        # Baseline is computed once per scenario and reused by every enabled
        # phase below, so "carbon_saving" is comparable regardless of which
        # phases run (and no phase ever needs to recompute it).
        print("  Computing greedy baseline …")
        baseline_cost = _run_baseline_only(
            scenario_path, scenario_dir, rust_binary,
            realtime_slots=realtime_slots,
            realtime_speed_scale=realtime_speed_scale,
            max_batch_solver_parallelism=max_batch_solver_parallelism,
            flavours=flavours,
            aco_n_ants=aco_n_ants,
            aco_n_iterations=aco_n_iterations,
        )

        if run_dp:
            rows, n_timings = _run_dp_phase(
                scenario_path, sid, batch_sizes, scenario_dir, rust_binary,
                baseline_cost,
                realtime_slots=realtime_slots,
                realtime_speed_scale=realtime_speed_scale,
                max_batch_solver_parallelism=max_batch_solver_parallelism,
                flavours=flavours,
                aco_n_ants=aco_n_ants,
                aco_n_iterations=aco_n_iterations,
            )
            all_rows.extend(rows)
            per_n_timing_rows.extend(n_timings)

        if run_online:
            rows, n_timings = _run_online_phase(
                scenario_path, sid, online_strategies, scenario_dir, rust_binary,
                baseline_cost,
                online_batch_sizes=online_batch_sizes,
                realtime_slots=realtime_slots,
                realtime_speed_scale=realtime_speed_scale,
                max_batch_solver_parallelism=max_batch_solver_parallelism,
                flavours=flavours,
                aco_n_ants=aco_n_ants,
                aco_n_iterations=aco_n_iterations,
            )
            all_rows.extend(rows)
            per_n_timing_rows.extend(n_timings)

        if run_offline:
            rows, n_timings = _run_offline_phase(
                scenario_path, sid, additional_strategies, scenario_dir, rust_binary,
                baseline_cost,
                realtime_slots=realtime_slots,
                realtime_speed_scale=realtime_speed_scale,
                max_batch_solver_parallelism=max_batch_solver_parallelism,
                flavours=flavours,
                aco_n_ants=aco_n_ants,
                aco_n_iterations=aco_n_iterations,
            )
            all_rows.extend(rows)
            per_n_timing_rows.extend(n_timings)

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

    per_n_timings_csv = output_dir / f"per_n_timings_{battery_id}.csv"
    with open(per_n_timings_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=PER_N_TIMING_COLUMNS)
        writer.writeheader()
        writer.writerows(per_n_timing_rows)

    input_patterns = sorted({
        str(scenario.get("input_pattern", scenario_config.INPUT_PATTERN))
        for scenario in cfg.get("scenarios", [])
    })
    carbon_patterns = sorted({
        str(scenario.get("carbon_pattern", scenario_config.CARBON_PATTERN))
        for scenario in cfg.get("scenarios", [])
    })
    _write_run_readme(
        output_dir,
        battery_id,
        start_dt,
        max_batch_solver_parallelism,
        realtime_slots,
        realtime_speed_scale,
        alt_strategies_enabled,
        batch_sizes,
        additional_strategies,
        online_strategies,
        online_batch_sizes,
        input_patterns,
        carbon_patterns,
        all_rows,
        per_n_timing_rows,
        battery_elapsed,
        trim_future_assignments,
    )

    print(f"\n{'='*60}")
    print(f"Battery complete in {battery_elapsed:.1f}s: {len(all_rows)} rows written to {results_csv}")
    print(f"Timings written to {timings_csv}")
    print(f"Per-N timings written to {per_n_timings_csv}")
    print(f"Run folder: {output_dir}")
    print(f"README + battery_config.json + config.rs snapshot written to {output_dir}")


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
