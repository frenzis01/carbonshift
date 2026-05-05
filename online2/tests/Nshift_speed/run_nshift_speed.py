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
from tests.Nshift_speed.simulator import run_single_batch_size


SUMMARY_JSON_NAME = "summary_by_n.json"
SUMMARY_CSV_NAME = "summary_by_n.csv"


def _write_single_run_outputs(
    output_root: Path,
    batch_size: int,
    result,
) -> None:
    run_dir = output_root / f"N{batch_size}"
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


def run_benchmark_from_config(config_path: Path) -> List[Dict[str, Any]]:
    cfg = load_runner_config(config_path)
    scenario = load_json(cfg["scenario_path"])
    output_root: Path = cfg["output_dir"]
    output_root.mkdir(parents=True, exist_ok=True)

    summaries: List[Dict[str, Any]] = []
    flush_partial_batch = bool(cfg["runner"].get("flush_partial_batch", True))
    for batch_size in cfg["batch_sizes"]:
        result = run_single_batch_size(
            scenario,
            batch_size=batch_size,
            flush_partial_batch=flush_partial_batch,
            output_csv_path=str(output_root / f"N{batch_size}" / "assignments_runtime.csv"),
        )
        _write_single_run_outputs(output_root, batch_size, result)
        summaries.append(flatten_summary_for_csv(result.summary))
        print(
            "Completed N="
            f"{batch_size}: solver_ms_avg={result.summary['solver_time_ms_avg']:.3f}, "
            f"total_carbon={result.summary['total_carbon_cost']:.3f}"
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
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    summaries = run_benchmark_from_config(args.config)
    print(f"Wrote benchmark output for {len(summaries)} batch sizes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
