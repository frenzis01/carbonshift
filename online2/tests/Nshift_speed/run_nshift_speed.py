"""Run reproducible multi-N speed benchmark over a static scenario."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional

ONLINE2_ROOT = Path(__file__).resolve().parents[2]
if str(ONLINE2_ROOT) not in sys.path:
    sys.path.insert(0, str(ONLINE2_ROOT))

from tests.Nshift_speed.metrics import flatten_summary_for_csv
from tests.Nshift_speed.scenario_io import (
    load_json,
    load_runner_config,
    save_json,
    save_rows_as_csv,
)
from tests.Nshift_speed.simulator import run_greedy_baseline, run_single_batch_size


SUMMARY_JSON_NAME = "summary_by_n.json"
SUMMARY_CSV_NAME = "summary_by_n.csv"
BASELINE_SUMMARY_JSON_NAME = "baseline_summary.json"
BASELINE_SUMMARY_CSV_NAME = "baseline_summary.csv"


def _write_run_outputs(
    output_root: Path,
    run_name: str,
    result,
) -> None:
    run_dir = output_root / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    save_json(run_dir / "summary.json", result.summary)
    save_rows_as_csv(run_dir / "summary.csv", [flatten_summary_for_csv(result.summary)], list(flatten_summary_for_csv(result.summary).keys()))

    save_json(run_dir / "per_request.json", {"rows": result.per_request})
    save_rows_as_csv(
        run_dir / "per_request.csv",
        result.per_request,
        [
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
            "strategy_name",
            "error",
            "carbon_cost",
            "assignment_solver_mode",
            "assignment_solver_status",
            "assigned_with_greedy_fallback",
            "assigned_with_relaxed_retry",
        ],
    )

    save_json(run_dir / "per_timeslot.json", {"rows": result.per_timeslot})
    save_rows_as_csv(
        run_dir / "per_timeslot.csv",
        result.per_timeslot,
        [
            "timeslot",
            "window_start",
            "window_end",
            "real_request_count",
            "modeled_request_count",
            "window_avg_error_real",
            "window_avg_error_modeled",
        ],
    )

    save_json(run_dir / "batch_timings.json", {"rows": result.batch_timings})
    save_rows_as_csv(
        run_dir / "batch_timings.csv",
        result.batch_timings,
        [
            "batch_sequence",
            "batch_size_n",
            "effective_batch_size",
            "slot",
            "pending_before",
            "solver_elapsed_ms",
            "scheduled",
            "flush_partial_batch",
        ],
    )


def _write_single_run_outputs(
    output_root: Path,
    batch_size: int,
    result,
) -> None:
    _write_run_outputs(output_root, f"N{batch_size}", result)


def _validate_realtime_speed_scale(value: float) -> float:
    numeric = float(value)
    if numeric < 0.0 or numeric > 1.0:
        raise ValueError("realtime_speed_scale must be in [0.0, 1.0].")
    return numeric


def run_benchmark_from_config(
    config_path: Path,
    *,
    realtime_slots_override: Optional[bool] = None,
    realtime_speed_scale_override: Optional[float] = None,
) -> List[Dict[str, Any]]:
    cfg = load_runner_config(config_path)
    scenario = load_json(cfg["scenario_path"])
    output_root: Path = cfg["output_dir"]
    output_root.mkdir(parents=True, exist_ok=True)

    summaries: List[Dict[str, Any]] = []
    flush_partial_batch = bool(cfg["runner"].get("flush_partial_batch", True))
    include_greedy_baseline = bool(cfg["runner"].get("include_greedy_baseline", True))
    realtime_slots = bool(cfg["runner"].get("realtime_slots", False))
    realtime_speed_scale = _validate_realtime_speed_scale(
        cfg["runner"].get("realtime_speed_scale", 1.0)
    )
    if realtime_slots_override is not None:
        realtime_slots = bool(realtime_slots_override)
    if realtime_speed_scale_override is not None:
        realtime_speed_scale = _validate_realtime_speed_scale(realtime_speed_scale_override)

    baseline_cost: Optional[float] = None
    if include_greedy_baseline:
        baseline_result = run_greedy_baseline(
            scenario,
            realtime_slots=realtime_slots,
            realtime_speed_scale=realtime_speed_scale,
        )
        _write_run_outputs(output_root, "baseline_greedy", baseline_result)
        baseline_summary = flatten_summary_for_csv(baseline_result.summary)
        save_json(output_root / BASELINE_SUMMARY_JSON_NAME, baseline_summary)
        save_rows_as_csv(
            output_root / BASELINE_SUMMARY_CSV_NAME,
            [baseline_summary],
            list(baseline_summary.keys()),
        )
        baseline_cost = float(baseline_result.summary["total_carbon_cost"])
        print(
            "Completed baseline: "
            f"mode={baseline_result.summary.get('execution_mode', 'greedy_baseline_immediate')}, "
            f"total_carbon={baseline_cost:.3f}, "
            f"strategy={baseline_result.summary.get('baseline_strategy_name', 'Accurate')}"
        )

    for batch_size in cfg["batch_sizes"]:
        result = run_single_batch_size(
            scenario,
            batch_size=batch_size,
            flush_partial_batch=flush_partial_batch,
            realtime_slots=realtime_slots,
            realtime_speed_scale=realtime_speed_scale,
            output_csv_path=str(output_root / f"N{batch_size}" / "assignments_runtime.csv"),
        )
        _write_single_run_outputs(output_root, batch_size, result)
        summary_row = flatten_summary_for_csv(result.summary)
        if baseline_cost is not None:
            savings = baseline_cost - float(summary_row["total_carbon_cost"])
            savings_pct = (savings / baseline_cost * 100.0) if baseline_cost > 0.0 else 0.0
            summary_row["baseline_total_carbon_cost"] = baseline_cost
            summary_row["carbon_cost_saving_vs_baseline"] = savings
            summary_row["carbon_cost_saving_vs_baseline_pct"] = savings_pct
        summaries.append(summary_row)
        print(
            "Completed N="
            f"{batch_size}: solver_ms_avg={result.summary['solver_time_ms_avg']:.3f}, "
            f"total_carbon={result.summary['total_carbon_cost']:.3f}, "
            f"saving_vs_baseline={(summary_row.get('carbon_cost_saving_vs_baseline', 0.0)):.3f}, "
            f"realtime_slots={str(realtime_slots).lower()}, scale={realtime_speed_scale:.2f}"
        )

    save_json(output_root / SUMMARY_JSON_NAME, {"rows": summaries})
    if summaries:
        save_rows_as_csv(output_root / SUMMARY_CSV_NAME, summaries, list(summaries[0].keys()))
    else:
        save_rows_as_csv(output_root / SUMMARY_CSV_NAME, [], ["batch_size"])
    return summaries


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run multi-N benchmark against a static scenario.")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent / "config.json",
        help="Path to N-shift runner config JSON.",
    )
    parser.add_argument(
        "--realtime-slots",
        action="store_true",
        help="Override config and run with wall-clock slot progression.",
    )
    parser.add_argument(
        "--no-realtime-slots",
        action="store_true",
        help="Override config and force fast simulation without wall-clock waiting.",
    )
    parser.add_argument(
        "--realtime-speed-scale",
        type=_validate_realtime_speed_scale,
        default=None,
        help="Override realtime speed scale in [0.0, 1.0] (1.0=full slot duration).",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.realtime_slots and args.no_realtime_slots:
        parser.error("Use at most one of --realtime-slots and --no-realtime-slots.")

    realtime_slots_override: Optional[bool] = None
    if args.realtime_slots:
        realtime_slots_override = True
    elif args.no_realtime_slots:
        realtime_slots_override = False

    summaries = run_benchmark_from_config(
        args.config,
        realtime_slots_override=realtime_slots_override,
        realtime_speed_scale_override=args.realtime_speed_scale,
    )
    print(f"Wrote benchmark output for {len(summaries)} batch sizes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
