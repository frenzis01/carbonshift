"""Tests for battery notebook result discovery."""
from __future__ import annotations

import json
import sys
from pathlib import Path

BATTERY_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BATTERY_DIR))

from nb_utils import discover_assignment_runs  # noqa: E402


def _write_assignment(directory: Path, *, schema_name: str = "carbonshift.nshift") -> None:
    directory.mkdir(parents=True)
    (directory / "per_request.csv").write_text("request_id\n1\n", encoding="utf-8")
    (directory / "output_schema.json").write_text(
        json.dumps({"schema_name": schema_name, "schema_version": 2}),
        encoding="utf-8",
    )


def test_discover_assignment_runs_includes_online_per_request_results(tmp_path: Path):
    results_root = tmp_path / "results"
    scenario_dir = results_root / "T30_perrequest" / "scen_100_20_sine"

    _write_assignment(scenario_dir / "rust_dp" / "N1")
    _write_assignment(scenario_dir / "rust_online" / "online_bandit" / "N1")
    _write_assignment(scenario_dir / "rust_online" / "online_bandit" / "N8")
    _write_assignment(scenario_dir / "rust_offline" / "strategy_arrival_slot")
    _write_assignment(
        scenario_dir / "rust_baseline" / "baseline_greedy",
        schema_name="carbonshift.baseline",
    )

    runs = discover_assignment_runs(results_root)

    assert len(runs) == 3
    by_type = {run.run_type: run for run in runs}

    assert by_type["batch"].assignment_dir == scenario_dir / "rust_dp"
    assert by_type["batch"].batch_sizes == (1,)
    assert by_type["online"].assignment_dir == (
        scenario_dir / "rust_online" / "online_bandit"
    )
    assert by_type["online"].mode_label == "online_bandit"
    assert by_type["online"].batch_sizes == (1, 8)
    assert by_type["online"].scenario_dir == scenario_dir
    assert by_type["online"].run_folder_id == "T30_perrequest"
    assert by_type["offline"].assignment_dir == (
        scenario_dir / "rust_offline" / "strategy_arrival_slot"
    )
