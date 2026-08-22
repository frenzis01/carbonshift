#!/usr/bin/env python3
"""
Battery performance test runner for CarbonShift solver (Rust `nshift` binary only).

For each scenario in battery_config.json, generates a deterministic scenario and
runs the Rust solver across three independent, individually-toggleable phases:

  1. DP        – sweeps `batch_sizes` × `infeasibility_modes`. Skipped entirely
                 if either list is empty.
  2. Online    – runs `online_strategies` (e.g. "bandit", "ant_colony",
                 "greedy_singleton") over `online_batch_sizes` (falls back to
                 `batch_sizes` if absent). Skipped if `online_strategies` is empty.
                 Independent of the DP phase/`batch_sizes` — e.g. set
                 `batch_sizes: []` to sweep online strategies without running DP.
  3. Offline   – runs `additional_strategies` (e.g. "greedy_cheapest",
                 "ant_colony", "bandit") once per scenario, with no batch-size
                 or rollback dependency. Skipped if `additional_strategies` is
                 empty.

The greedy baseline is computed once per scenario (a dedicated lightweight Rust
invocation) whenever at least one phase is enabled, and its cost is then reused
by every phase so "carbon_saving" is comparable across all of them.

Usage (from the repository root):
    python tests/battery/run_battery.py [--config tests/battery/battery_config.json]

Config fields
-------------
batch_sizes          : list[int]   – DP phase N values to benchmark; [] skips DP.
infeasibility_modes  : list[str]   – values of the DP solver's `infeasibility_recovery_mode`
                                     knob to benchmark: "min_error_greedy" | "carryover" | "forecast".
                                     The error-window constraint is never relaxed/removed for any
                                     of these; they only differ in whether/how mock requests dilute
                                     the error baseline before the primary DP solve. Infeasibility
                                     always resolves via direct greedy fallback.
online_strategies    : list[str]   – online strategies to run (e.g. "bandit", "ant_colony",
                                     "greedy_singleton"); [] skips the online phase.
online_batch_sizes   : list[int]   – N sweep for online_strategies; falls back to
                                     `batch_sizes` when omitted. "greedy_singleton" always
                                     forces N=1 regardless of this list.
additional_strategies: list[str]   – offline strategies to run once per scenario
                                     (e.g. "greedy_cheapest", "ant_colony", "bandit");
                                     [] skips the offline phase.
rust_binary_path     : str         – path to the nshift binary (relative to repo root)
output_dir           : str         – where to write run artefacts + battery_results.csv
scenarios            : list        – each entry has:
    id                   : str     – unique label used in CSV and directory names
    seed                 : int     – RNG seed for scenario generation
    requests_per_slot    : float
    total_slots          : int
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
RUST_CONFIG_RS = CARBONSHIFT_ROOT / "rust" / "src" / "config.rs"

sys.path.insert(0, str(SCENARIOS_DIR))

import scenario_config  # noqa: E402  (scenario-generation defaults; after sys.path update)
from scenario_io import generate_scenario_data, load_json, save_json  # noqa: E402

# ─── constants ────────────────────────────────────────────────────────────────
# Fallback defaults for the Rust runtime knobs below, mirroring
# rust/src/config.rs's Config::default(). battery_config.json can override
# each of these; they also feed the run-output folder name (see
# _format_run_folder_name) and the generated README.md.
DEFAULT_MAX_BATCH_SOLVER_PARALLELISM = 20
DEFAULT_REALTIME_SLOTS = False
DEFAULT_REALTIME_SPEED_SCALE = 0.05
DEFAULT_ROLLBACK_MAX_CONSECUTIVE = 0
# Mirrors rust/src/config.rs's Config::online_swarm_mode default ("serialized").
DEFAULT_ONLINE_SWARM_MODE = "serialized"

RESULT_COLUMNS = [
    "scenario_id",
    "batch_size",
    "infeasibility_mode",
    "solver_time_ms_avg",
    "carbon_cost",
    "final_global_error",
    "avg_global_error_per_slot",
    "requests_in",
    "requests_assigned_with_greedy_fallback",
    "requests_assigned_with_relaxed_retry",
    "total_rollbacks",
    "peak_consecutive_rollbacks",
    "baseline_carbon_cost",
    "carbon_saving",
    "peak_concurrent_workers",
    "avg_concurrent_workers",
]

TIMING_COLUMNS = ["scenario_id", "elapsed_seconds"]

PER_N_TIMING_COLUMNS = ["scenario_id", "mode", "batch_size", "elapsed_seconds"]


# ─── helpers ──────────────────────────────────────────────────────────────────

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
    tiers = scenario_def.get("capacity_tiers", list(scenario_config.CAPACITY_TIERS))
    data = generate_scenario_data(
        seed=int(scenario_def.get("seed", 42)),
        total_slots=int(scenario_def.get("total_slots", scenario_config.TOTAL_SLOTS)),
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
        prehistory_error_ratio=float(scenario_config.PREHISTORY_ERROR_RATIO_OF_THRESHOLD),
        capacity_tiers=tiers,
        carbon_inverted=bool(scenario_config.CARBON_INVERTED),
    )
    save_json(output_path, data)
    return data


# ─── Rust backend ──────────────────────────────────────────────────────────────

def _write_rust_config(
    scenario_path: Path,
    batch_sizes: List[int],
    output_dir: Path,
    infeasibility_recovery_mode: str,
    include_baseline: bool,
    additional_strategies: Optional[List[str]] = None,
    online_strategies: Optional[List[str]] = None,
    online_batch_sizes: Optional[List[int]] = None,
    realtime_slots: bool = DEFAULT_REALTIME_SLOTS,
    realtime_speed_scale: float = DEFAULT_REALTIME_SPEED_SCALE,
    max_batch_solver_parallelism: int = DEFAULT_MAX_BATCH_SOLVER_PARALLELISM,
    rollback_max_consecutive: int = DEFAULT_ROLLBACK_MAX_CONSECUTIVE,
    online_swarm_mode: str = DEFAULT_ONLINE_SWARM_MODE,
    baseline_total_carbon_cost: Optional[float] = None,
) -> Path:
    """Write a temporary nshift config.json; caller is responsible for deletion."""
    fd, tmp = tempfile.mkstemp(suffix=".json")
    runner: Dict[str, Any] = {
        "flush_partial_batch": True,
        "include_greedy_baseline": include_baseline,
        "realtime_slots": realtime_slots,
        "realtime_speed_scale": realtime_speed_scale,
        "infeasibility_recovery_mode": infeasibility_recovery_mode,
        "rollback_max_consecutive": rollback_max_consecutive,
        "max_batch_solver_parallelism": max_batch_solver_parallelism,
        "online_swarm_mode": online_swarm_mode,
    }
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
    """Invoke the nshift binary with a temporary config, always cleaning it up."""
    try:
        subprocess.run([str(rust_binary), "--config", str(tmp_config)], check=True)
    finally:
        tmp_config.unlink(missing_ok=True)


def _make_result_row(
    scenario_id: str,
    batch_size: int,
    infeasibility_mode: str,
    row: Dict[str, Any],
    baseline_cost: float,
    avg_slot_err: float,
) -> Dict[str, Any]:
    """Build one `RESULT_COLUMNS`-shaped row from a parsed Rust summary CSV row.

    Shared by the DP/online/offline phases so each only supplies what differs
    (label, batch_size, the summary row itself); missing columns in strategy
    summaries (e.g. no rollback/concurrency stats for offline runs) default to 0.
    """
    carbon = float(row.get("carbon_cost", 0.0))
    savings_pct = (baseline_cost - carbon) / baseline_cost * 100.0 if baseline_cost > 0.0 else 0.0
    return {
        "scenario_id": scenario_id,
        "batch_size": batch_size,
        "infeasibility_mode": infeasibility_mode,
        "solver_time_ms_avg": float(row.get("solver_time_ms_avg", 0.0)),
        "carbon_cost": carbon,
        "final_global_error": float(row.get("global_average_error", 0.0)),
        "avg_global_error_per_slot": avg_slot_err,
        "requests_in": int(row.get("requests_total", 0)),
        "requests_assigned_with_greedy_fallback": int(
            row.get("requests_assigned_with_greedy_fallback", 0)
        ),
        "requests_assigned_with_relaxed_retry": int(
            row.get("requests_assigned_with_relaxed_retry", 0)
        ),
        "total_rollbacks": int(row.get("total_rollbacks", 0)),
        "peak_consecutive_rollbacks": int(row.get("peak_consecutive_rollbacks", 0)),
        "baseline_carbon_cost": baseline_cost,
        "carbon_saving": savings_pct,
        "peak_concurrent_workers": int(row.get("peak_concurrent_workers", 0)),
        "avg_concurrent_workers": float(row.get("avg_concurrent_workers", 0.0)),
    }


def _run_baseline_only(
    scenario_path: Path,
    output_dir: Path,
    rust_binary: Path,
    realtime_slots: bool = DEFAULT_REALTIME_SLOTS,
    realtime_speed_scale: float = DEFAULT_REALTIME_SPEED_SCALE,
    max_batch_solver_parallelism: int = DEFAULT_MAX_BATCH_SOLVER_PARALLELISM,
    rollback_max_consecutive: int = DEFAULT_ROLLBACK_MAX_CONSECUTIVE,
    online_swarm_mode: str = DEFAULT_ONLINE_SWARM_MODE,
) -> float:
    """Compute the greedy baseline once via a dedicated, lightweight Rust
    invocation (no DP/online/offline work) so every phase can reuse the same
    cost for its "carbon_saving" field, independent of which phases run."""
    baseline_dir = output_dir / "rust_baseline"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    tmp_config = _write_rust_config(
        scenario_path, [], baseline_dir, "min_error_greedy", True,
        realtime_slots=realtime_slots,
        realtime_speed_scale=realtime_speed_scale,
        max_batch_solver_parallelism=max_batch_solver_parallelism,
        rollback_max_consecutive=rollback_max_consecutive,
        online_swarm_mode=online_swarm_mode,
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
    modes: List[str],
    output_dir: Path,
    rust_binary: Path,
    baseline_cost: float,
    realtime_slots: bool = DEFAULT_REALTIME_SLOTS,
    realtime_speed_scale: float = DEFAULT_REALTIME_SPEED_SCALE,
    max_batch_solver_parallelism: int = DEFAULT_MAX_BATCH_SOLVER_PARALLELISM,
    rollback_max_consecutive: int = DEFAULT_ROLLBACK_MAX_CONSECUTIVE,
    online_swarm_mode: str = DEFAULT_ONLINE_SWARM_MODE,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Sweep `batch_sizes` × `infeasibility_modes` (the DP phase). Caller
    ensures both lists are non-empty; the baseline is precomputed and passed
    in, never recomputed here."""
    all_rows: List[Dict[str, Any]] = []
    per_n_timing_rows: List[Dict[str, Any]] = []

    for mode in modes:
        mode_dir = output_dir / f"rust_{mode}"
        mode_dir.mkdir(parents=True, exist_ok=True)
        tmp_config = _write_rust_config(
            scenario_path, batch_sizes, mode_dir, mode, False,
            realtime_slots=realtime_slots,
            realtime_speed_scale=realtime_speed_scale,
            max_batch_solver_parallelism=max_batch_solver_parallelism,
            rollback_max_consecutive=rollback_max_consecutive,
            online_swarm_mode=online_swarm_mode,
            baseline_total_carbon_cost=baseline_cost,
        )
        _run_rust_binary(rust_binary, tmp_config)

        summary_csv = mode_dir / "summary_by_n.csv"
        if not summary_csv.exists():
            print(f"    WARNING: {summary_csv} not found; skipping mode={mode}")
            continue
        with open(summary_csv, newline="") as f:
            for row in csv.DictReader(f):
                n = int(row["batch_size"])
                avg_slot_err = _avg_slot_error_from_csv(mode_dir / f"N{n}" / "per_timeslot.csv")
                result_row = _make_result_row(scenario_id, n, mode, row, baseline_cost, avg_slot_err)
                all_rows.append(result_row)
                run_elapsed = float(row.get("run_elapsed_seconds", 0.0))
                per_n_timing_rows.append({
                    "scenario_id": scenario_id, "mode": mode, "batch_size": n,
                    "elapsed_seconds": round(run_elapsed, 3),
                })
                print(
                    f"    [rust/{mode}] N={n}: "
                    f"solver_ms={result_row['solver_time_ms_avg']:.1f}, "
                    f"carbon={result_row['carbon_cost']:.3f}, "
                    f"error={result_row['final_global_error']:.3f}, "
                    f"saving={result_row['carbon_saving']:.1f}%, "
                    f"elapsed={run_elapsed:.1f}s, "
                    f"total_rollbacks={result_row['total_rollbacks']}"
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
    rollback_max_consecutive: int = DEFAULT_ROLLBACK_MAX_CONSECUTIVE,
    online_swarm_mode: str = DEFAULT_ONLINE_SWARM_MODE,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Run `online_strategies` once, independent of the DP phase (`batch_sizes`
    is always `[]` for this invocation — see `online_batch_sizes` instead).
    Caller ensures `online_strategies` is non-empty."""
    all_rows: List[Dict[str, Any]] = []
    per_n_timing_rows: List[Dict[str, Any]] = []

    online_dir = output_dir / "rust_online"
    online_dir.mkdir(parents=True, exist_ok=True)
    tmp_config = _write_rust_config(
        scenario_path, [], online_dir, "min_error_greedy", False,
        online_strategies=online_strategies,
        online_batch_sizes=online_batch_sizes,
        realtime_slots=realtime_slots,
        realtime_speed_scale=realtime_speed_scale,
        max_batch_solver_parallelism=max_batch_solver_parallelism,
        rollback_max_consecutive=rollback_max_consecutive,
        online_swarm_mode=online_swarm_mode,
        baseline_total_carbon_cost=baseline_cost,
    )
    _run_rust_binary(rust_binary, tmp_config)

    for strategy in online_strategies:
        summary_csv = online_dir / f"online_{strategy}" / "summary_by_n.csv"
        if not summary_csv.exists():
            print(f"    WARNING: {summary_csv} not found; skipping online strategy={strategy}")
            continue
        mode_label = f"online_{strategy}"
        with open(summary_csv, newline="") as f:
            rows_csv = list(csv.DictReader(f))
        for row in rows_csv:
            n = int(row["batch_size"])
            per_ts_csv = online_dir / f"online_{strategy}" / f"N{n}" / "per_timeslot.csv"
            avg_slot_err = _avg_slot_error_from_csv(per_ts_csv)
            result_row = _make_result_row(scenario_id, n, mode_label, row, baseline_cost, avg_slot_err)
            all_rows.append(result_row)
            run_elapsed = float(row.get("run_elapsed_seconds", 0.0))
            per_n_timing_rows.append({
                "scenario_id": scenario_id, "mode": mode_label, "batch_size": n,
                "elapsed_seconds": round(run_elapsed, 3),
            })
            print(
                f"    [rust/online_{strategy}] N={n}: "
                f"solver_ms={result_row['solver_time_ms_avg']:.1f}, "
                f"carbon={result_row['carbon_cost']:.3f}, "
                f"error={result_row['final_global_error']:.3f}, "
                f"saving={result_row['carbon_saving']:.1f}%, "
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
    rollback_max_consecutive: int = DEFAULT_ROLLBACK_MAX_CONSECUTIVE,
    online_swarm_mode: str = DEFAULT_ONLINE_SWARM_MODE,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Run `additional_strategies` once per scenario — no batch-size,
    rollback, or DP-mode dependency. Caller ensures the list is non-empty."""
    all_rows: List[Dict[str, Any]] = []
    per_n_timing_rows: List[Dict[str, Any]] = []

    strat_dir = output_dir / "rust_offline"
    strat_dir.mkdir(parents=True, exist_ok=True)
    tmp_config = _write_rust_config(
        scenario_path, [], strat_dir, "min_error_greedy", False,
        additional_strategies=additional_strategies,
        realtime_slots=realtime_slots,
        realtime_speed_scale=realtime_speed_scale,
        max_batch_solver_parallelism=max_batch_solver_parallelism,
        rollback_max_consecutive=rollback_max_consecutive,
        online_swarm_mode=online_swarm_mode,
        baseline_total_carbon_cost=baseline_cost,
    )
    _run_rust_binary(rust_binary, tmp_config)

    for strategy in additional_strategies:
        summary_csv = strat_dir / f"strategy_{strategy}" / "summary.csv"
        per_ts_csv = strat_dir / f"strategy_{strategy}" / "per_timeslot.csv"
        if not summary_csv.exists():
            print(f"    WARNING: {summary_csv} not found; skipping strategy={strategy}")
            continue
        mode_label = f"offline_{strategy}"
        avg_slot_err = _avg_slot_error_from_csv(per_ts_csv)
        with open(summary_csv, newline="") as f:
            rows_csv = list(csv.DictReader(f))
        for row in rows_csv:
            result_row = _make_result_row(scenario_id, 0, mode_label, row, baseline_cost, avg_slot_err)
            all_rows.append(result_row)
            run_elapsed = float(row.get("run_elapsed_seconds", 0.0))
            per_n_timing_rows.append({
                "scenario_id": scenario_id, "mode": mode_label, "batch_size": 0,
                "elapsed_seconds": round(run_elapsed, 3),
            })
            print(
                f"    [rust/offline_{strategy}]: "
                f"carbon={result_row['carbon_cost']:.3f}, "
                f"error={result_row['final_global_error']:.3f}, "
                f"saving={result_row['carbon_saving']:.1f}%, "
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
    alt_strategies_enabled: bool,
    online_swarm_mode: str,
    rollback_max_consecutive: int,
) -> str:
    """Build the per-run results subfolder name.

    Format: <battery_id>_<mmdd>_<hhmm>_<parallelism>_roll<X>_<realtime_scale>_<altstratT|F>_<S|M>
    - realtime_scale is "0" when realtime_slots is disabled, otherwise the
      configured speed scale with its decimal point stripped (0.5 -> "05").
    - rollX is the configured rollback_max_consecutive (0 = rollback disabled).
    - altstrat is "T" if any online/additional alternative strategy is enabled.
    - the trailing token is "S" for online_swarm_mode="serialized" (default)
      or "M" for "merge" — see Config::online_swarm_mode in rust/src/config.rs.
    """
    mmdd = start_dt.strftime("%m%d")
    hhmm = start_dt.strftime("%H%M")
    scale_token = str(realtime_speed_scale).replace(".", "") if realtime_slots else "0"
    altstrat_token = "T" if alt_strategies_enabled else "F"
    swarm_mode_token = "M" if online_swarm_mode == "merge" else "S"
    return (
        f"{battery_id}_{mmdd}_{hhmm}_{max_batch_solver_parallelism}_roll{rollback_max_consecutive}_"
        f"{scale_token}_{altstrat_token}_{swarm_mode_token}"
    )


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
    ("dp_lock_future_assignments", "DP locks future assignments"),
    ("infeasibility_recovery_mode", "Infeasibility recovery mode"),
    ("prehistory_use_virtual_past", "Virtual prehistory enabled (default)"),
    ("queue_timeout", "Queue timeout (s)"),
]


def _parse_rust_config_defaults(config_rs_path: Path) -> Dict[str, str]:
    """Extract scalar field values from `impl Default for Config` in config.rs.

    This is intentionally a simple line-based scan (not a Rust parser): it only
    captures single-line `name: value,` assignments and skips any line
    containing brackets (`{`, `}`, `[`, `]`), which safely excludes multi-line
    literals such as `flavours: vec![...]` or `capacity_tiers: vec![...]`.
    Used only to populate the run README with human-readable defaults, so
    approximate parsing is acceptable. Returns an empty dict if the file is
    missing, so callers can render "?" placeholders instead of crashing.
    """
    if not config_rs_path.exists():
        return {}
    text = config_rs_path.read_text()
    marker = "impl Default for Config"
    start = text.find(marker)
    if start == -1:
        return {}
    body = text[start:]
    values: Dict[str, str] = {}
    for line in body.splitlines():
        line = line.strip()
        if not line or "{" in line or "}" in line or "[" in line or "]" in line:
            continue
        m = re.match(r"^(\w+):\s*(.+?),\s*(?://.*)?$", line)
        if m:
            value = m.group(2).strip().removesuffix(".to_string()")
            values[m.group(1)] = value
    return values


def _row_label(row: Dict[str, Any]) -> str:
    """Human-readable identifier for a results row: 'N=<n>' or an offline/online strategy name."""
    mode = row["infeasibility_mode"]
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
        key = (scenario_id, r["infeasibility_mode"], r["batch_size"])
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
        (r["scenario_id"], r["mode"], r["batch_size"]): r["elapsed_seconds"]
        for r in per_n_timing_rows
    }
    scenario_ids = sorted({r["scenario_id"] for r in all_rows})
    categories = [
        (
            "DP recovery modes — top 3 by carbon cost",
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
            matched = [r for r in rows_for_scenario if predicate(r["infeasibility_mode"])]
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
    rollback_max_consecutive: int,
    alt_strategies_enabled: bool,
    batch_sizes: List[int],
    modes: List[str],
    additional_strategies: List[str],
    online_strategies: List[str],
    online_batch_sizes: Optional[List[int]],
    all_rows: List[Dict[str, Any]],
    per_n_timing_rows: List[Dict[str, Any]],
    battery_elapsed: float,
    online_swarm_mode: str = DEFAULT_ONLINE_SWARM_MODE,
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
        "",
        "## Run-identifying parameters (also encoded in the folder name)",
        "",
        f"- Battery id: `{battery_id}`",
        f"- Start date: {start_dt.strftime('%Y-%m-%d')}",
        f"- Start time: {start_dt.strftime('%H:%M')}",
        f"- Max batch solver parallelism (max concurrent DP workers): {max_batch_solver_parallelism}",
        (
            f"- Realtime slot pacing: enabled, speed scale={realtime_speed_scale}"
            if realtime_slots else
            "- Realtime slot pacing: disabled (slots advance instantly)"
        ),
        f"- Alternative strategies enabled: {'yes' if alt_strategies_enabled else 'no'}",
        (
            f"- Online swarm concurrency mode: {online_swarm_mode} "
            f"({'S' if online_swarm_mode != 'merge' else 'M'} token in folder name; "
            "see Config::online_swarm_mode in rust/src/config.rs)"
        ),
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
        f"- Rollback max consecutive (this run): {rollback_max_consecutive} "
        f"({'disabled' if rollback_max_consecutive == 0 else 'enabled'}; "
        "affects DP and greedy_singleton only, not bandit/ant_colony)",
        f"- Infeasibility recovery mode: {rust_defaults.get('infeasibility_recovery_mode', '?')}",
        "",
        "## Enabled phases",
        "",
        f"- DP phase: {'enabled' if (batch_sizes and modes) else 'disabled'} "
        f"— batch sizes (N): {batch_sizes or 'none'}, infeasibility modes: {modes or 'none'}",
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
    modes: List[str] = cfg.get("infeasibility_modes", ["min_error_greedy", "carryover", "forecast"])
    additional_strategies: List[str] = cfg.get("additional_strategies", [])
    online_strategies: List[str] = cfg.get("online_strategies", [])
    online_batch_sizes_raw = cfg.get("online_batch_sizes")
    online_batch_sizes: Optional[List[int]] = (
        [int(n) for n in online_batch_sizes_raw] if online_batch_sizes_raw else None
    )

    run_dp = bool(batch_sizes) and bool(modes)
    run_online = bool(online_strategies)
    run_offline = bool(additional_strategies)

    # Rust runtime knobs (also drive the run folder name below).
    max_batch_solver_parallelism = int(
        cfg.get("max_batch_solver_parallelism", DEFAULT_MAX_BATCH_SOLVER_PARALLELISM)
    )
    realtime_slots = bool(cfg.get("realtime_slots", DEFAULT_REALTIME_SLOTS))
    realtime_speed_scale = float(cfg.get("realtime_speed_scale", DEFAULT_REALTIME_SPEED_SCALE))
    rollback_max_consecutive = int(
        cfg.get("rollback_max_consecutive", DEFAULT_ROLLBACK_MAX_CONSECUTIVE)
    )
    online_swarm_mode = str(cfg.get("online_swarm_mode", DEFAULT_ONLINE_SWARM_MODE))
    alt_strategies_enabled = run_online or run_offline

    run_folder_name = _format_run_folder_name(
        battery_id, start_dt, max_batch_solver_parallelism,
        realtime_slots, realtime_speed_scale, alt_strategies_enabled,
        online_swarm_mode, rollback_max_consecutive,
    )
    output_dir = base_output_dir / run_folder_name
    output_dir.mkdir(parents=True, exist_ok=True)

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

        print(f"\n{'='*60}")
        print(f"Scenario: {sid}  (seed={seed}, slots={total_slots}, req/slot={req_per_slot})")
        print("=" * 60)

        scenario_t0 = time.monotonic()
        scenario_dir = output_dir / sid
        scenario_dir.mkdir(parents=True, exist_ok=True)
        SCENARIOS_JSON_DIR.mkdir(parents=True, exist_ok=True)
        scenario_path = SCENARIOS_JSON_DIR / f"{sid}.json"
        _generate_scenario(scenario_def, scenario_path)

        if not (run_dp or run_online or run_offline):
            print("  WARNING: no phase enabled (batch_sizes/infeasibility_modes, "
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
            rollback_max_consecutive=rollback_max_consecutive,
            online_swarm_mode=online_swarm_mode,
        )

        if run_dp:
            rows, n_timings = _run_dp_phase(
                scenario_path, sid, batch_sizes, modes, scenario_dir, rust_binary,
                baseline_cost,
                realtime_slots=realtime_slots,
                realtime_speed_scale=realtime_speed_scale,
                max_batch_solver_parallelism=max_batch_solver_parallelism,
                rollback_max_consecutive=rollback_max_consecutive,
                online_swarm_mode=online_swarm_mode,
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
                rollback_max_consecutive=rollback_max_consecutive,
                online_swarm_mode=online_swarm_mode,
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
                rollback_max_consecutive=rollback_max_consecutive,
                online_swarm_mode=online_swarm_mode,
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

    _write_run_readme(
        output_dir,
        battery_id,
        start_dt,
        max_batch_solver_parallelism,
        realtime_slots,
        realtime_speed_scale,
        rollback_max_consecutive,
        alt_strategies_enabled,
        batch_sizes,
        modes,
        additional_strategies,
        online_strategies,
        online_batch_sizes,
        all_rows,
        per_n_timing_rows,
        battery_elapsed,
        online_swarm_mode,
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
