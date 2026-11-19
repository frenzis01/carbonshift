"""Tests for battery result aggregation and output preservation."""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import save_results  # noqa: E402


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_aggregate_sorts_rows_and_preserves_full_precision_metrics(tmp_path, monkeypatch):
    run_dir = tmp_path / "T11_sample"
    run_dir.mkdir()
    (run_dir / "output_schema.json").write_text(
        json.dumps(
            {
                "schema_version": save_results.OUTPUT_SCHEMA_VERSION,
                "schema_name": "carbonshift.battery-run",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "battery_config.json").write_text(
        json.dumps({"max_batch_solver_parallelism": 4}),
        encoding="utf-8",
    )

    fields = [
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
        "custom_metric",
    ]
    rows = [
        {
            "scenario_id": "scenario_a",
            "batch_size": 8,
            "strategy": "online_ant_colony",
            "solver_time_ms_avg": 0.123456789,
            "carbon_cost": 100.123456789,
            "final_global_error": 3.987654321,
            "reqs_in": 100,
            "reqs_out": 90,
            "reqs_fallback_assigned": 2,
            "baseline_carbon_cost": 200.123456789,
            "carbon_saving": 50.123456789,
            "peak_concurrent_workers": 4,
            "avg_concurrent_workers": 2.5,
            "custom_metric": 9.87654321,
        },
        {
            "scenario_id": "scenario_a",
            "batch_size": 1,
            "strategy": "online_ant_colony",
            "solver_time_ms_avg": 0.987654321,
            "carbon_cost": 120.987654321,
            "final_global_error": 3.123456789,
            "reqs_in": 100,
            "reqs_out": 91,
            "reqs_fallback_assigned": 1,
            "baseline_carbon_cost": 220.987654321,
            "carbon_saving": 45.987654321,
            "peak_concurrent_workers": 4,
            "avg_concurrent_workers": 2.25,
            "custom_metric": 8.7654321,
        },
    ]
    _write_csv(run_dir / "battery_results_T11_sample.csv", fields, rows)
    _write_csv(
        run_dir / "per_n_timings_T11_sample.csv",
        ["scenario_id", "strategy", "batch_size", "elapsed_seconds"],
        [
            {
                "scenario_id": row["scenario_id"],
                "strategy": row["strategy"],
                "batch_size": row["batch_size"],
                "elapsed_seconds": 10.123456789 + row["batch_size"],
            }
            for row in rows
        ],
    )

    output_dir = tmp_path / "aggregate"
    output_dir.mkdir()
    monkeypatch.setattr(save_results, "RESULTS_DIR", output_dir)

    result = save_results.aggregate_and_save(
        run_dirs=[run_dir],
        sort_by=[
            "scenario_id",
            "policy",
            "batch",
            "error",
            "paral",
            "path",
        ],
    )

    assert result["batch"].tolist() == [1, 8]
    assert result["policy"].tolist() == ["online_ant_colony"] * 2
    assert result["paral"].tolist() == [4, 4]
    assert result["elapsed_s"].iloc[1] == pytest.approx(18.123456789)
    assert result["carb_cost"].iloc[1] == pytest.approx(100.123456789)
    assert result["carb_saving"].iloc[1] == pytest.approx(50.123456789)
    assert result["solver_ms"].iloc[1] == pytest.approx(0.123456789)
    assert result["error"].iloc[1] == pytest.approx(3.987654321)
    assert result["Thresh"].isna().all()
    assert result["baseline_carb_cost"].iloc[1] == pytest.approx(200.123456789)
    assert result["custom_metric"].iloc[1] == pytest.approx(9.87654321)
    assert result.columns[:18].tolist() == [
        "run_id",
        "scenario_id",
        "reqs_in",
        "reqs_out",
        "batch",
        "policy",
        "carb_saving",
        "elapsed_s",
        "error",
        "Thresh",
        "carb_cost",
        "reqs_fallback_assigned",
        "paral",
        "solver_ms",
        "peak_concurrent_workers",
        "avg_concurrent_workers",
        "baseline_carb_cost",
        "path",
    ]
    assert not {
        "batch_size",
        "strategy",
        "parallelism",
        "solver_time_ms_avg",
        "carbon_cost",
        "final_global_error",
        "baseline_carbon_cost",
        "carbon_saving",
        "elapsed_seconds",
    }.intersection(result.columns)

    saved = pd.read_csv(output_dir / "aggregated_battery_results.csv")
    assert saved["solver_ms"].iloc[1] == pytest.approx(0.123456789)
    assert saved["carb_cost"].iloc[1] == pytest.approx(100.123456789)
    assert saved["custom_metric"].iloc[1] == pytest.approx(9.87654321)


def test_load_run_adds_per_scenario_error_thresholds(tmp_path):
    run_dir = tmp_path / "T40_thresholds"
    run_dir.mkdir()
    (run_dir / "output_schema.json").write_text(
        json.dumps(
            {
                "schema_version": save_results.OUTPUT_SCHEMA_VERSION,
                "schema_name": "carbonshift.battery-run",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "battery_config.json").write_text(
        json.dumps(
            {
                "scenarios": [
                    {"id": "scenario_a", "max_error_threshold": 0.5},
                    {"id": "scenario_b", "max_error_threshold": 4.0},
                ]
            }
        ),
        encoding="utf-8",
    )
    _write_csv(
        run_dir / "battery_results_T40_thresholds.csv",
        ["scenario_id", "batch_size", "strategy"],
        [
            {"scenario_id": "scenario_a", "batch_size": 1, "strategy": "dp"},
            {"scenario_id": "scenario_b", "batch_size": 1, "strategy": "dp"},
        ],
    )

    result = save_results.load_run(run_dir)

    assert result["Thresh"].tolist() == [0.5, 4.0]
