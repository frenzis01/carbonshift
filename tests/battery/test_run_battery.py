"""Unit tests for the pure/pathless helper functions in run_battery.py.

These cover run-output packaging logic, scenario metadata wiring, and the
TOP-3 README tables. They do not invoke the Rust binary — see the module
docstring in run_battery.py for the manual end-to-end smoke test procedure.
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "scenarios"))

import multiple_runs as mr  # noqa: E402
import run_battery as rb  # noqa: E402


def test_generated_scenario_uses_configured_deadline_and_future_window(tmp_path):
    scenario = rb._generate_scenario(
        {"id": "window", "seed": 1, "total_slots": 2, "requests_per_slot": 1.0},
        tmp_path / "window.json",
    )

    assert scenario["metadata"]["total_slots"] == 2
    assert scenario["metadata"]["deadline_max_slack"] == 14
    assert scenario["metadata"]["error_window_future"] == 14
    assert "input_total_slots" not in scenario["metadata"]
    assert len(scenario["carbon_forecast"]) == 2


def test_future_assignment_trimming_extends_horizon_without_extra_arrivals(tmp_path):
    input_slots = 3
    scenario_def = {
        "id": "trim",
        "seed": 1,
        "total_slots": input_slots,
        "requests_per_slot": 20.0,
    }
    original = rb._generate_scenario(
        scenario_def,
        tmp_path / "original.json",
    )
    scenario = rb._generate_scenario(
        scenario_def,
        tmp_path / "trim.json",
        trim_future_assignments=True,
    )

    assert scenario["metadata"]["total_slots"] == input_slots + 14
    assert scenario["metadata"]["input_total_slots"] == input_slots
    assert len(scenario["carbon_forecast"]) == input_slots + 14
    assert all(0 <= request["arrival_slot"] < input_slots for request in scenario["requests"])
    assert any(request["deadline_slot"] >= input_slots for request in scenario["requests"])
    assert scenario["carbon_forecast"][:input_slots] == original["carbon_forecast"]
    assert [
        (request["request_id"], request["arrival_slot"], request["arrival_time"])
        for request in scenario["requests"]
    ] == [
        (request["request_id"], request["arrival_slot"], request["arrival_time"])
        for request in original["requests"]
    ]


def test_trim_rust_outputs_recomputes_request_metrics_from_retained_assignments(tmp_path):
    output_dir = tmp_path / "rust_carryover"
    run_dir = output_dir / "N2"
    run_dir.mkdir(parents=True)
    request_rows = [
        {
            "request_id": "1",
            "scheduled_slot": "0",
            "error": "0.0",
            "carbon_cost": "3.0",
            "queue_wait_seconds": "2.0",
            "final_wait_seconds": "4.0",
            "lateness_slots": "0",
            "assigned_with_greedy_fallback": "false",
            "assigned_with_relaxed_retry": "false",
            "assigned_with_injection": "true",
            "assigned_with_rollback": "false",
        },
        {
            "request_id": "2",
            "scheduled_slot": "2",
            "error": "5.0",
            "carbon_cost": "5.0",
            "queue_wait_seconds": "3.0",
            "final_wait_seconds": "5.0",
            "lateness_slots": "1",
            "assigned_with_greedy_fallback": "true",
            "assigned_with_relaxed_retry": "false",
            "assigned_with_injection": "false",
            "assigned_with_rollback": "true",
        },
        {
            "request_id": "3",
            "scheduled_slot": "1",
            "error": "2.0",
            "carbon_cost": "7.0",
            "queue_wait_seconds": "6.0",
            "final_wait_seconds": "8.0",
            "lateness_slots": "0",
            "assigned_with_greedy_fallback": "false",
            "assigned_with_relaxed_retry": "true",
            "assigned_with_injection": "true",
            "assigned_with_rollback": "false",
        },
    ]
    request_fields = list(request_rows[0])
    with (run_dir / "per_request.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=request_fields)
        writer.writeheader()
        writer.writerows(request_rows)
    (run_dir / "per_request.json").write_text(
        json.dumps({"rows": request_rows}),
        encoding="utf-8",
    )
    with (run_dir / "assignments_runtime.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["request_id", "scheduled_slot"])
        writer.writeheader()
        writer.writerows(
            {"request_id": row["request_id"], "scheduled_slot": row["scheduled_slot"]}
            for row in request_rows
        )

    summary_fields = [
        "batch_size",
        "requests_total",
        "requests_scheduled",
        "requests_unscheduled",
        "requests_late",
        "max_lateness_slots",
        "carbon_cost",
        "global_average_error",
        "global_average_error_real",
        "global_average_error_modeled",
        "requests_assigned_with_greedy_fallback",
        "requests_assigned_with_relaxed_retry",
        "requests_assigned_with_rollback",
        "queue_wait_seconds_min",
        "queue_wait_seconds_max",
        "queue_wait_seconds_avg",
        "final_wait_seconds_min",
        "final_wait_seconds_max",
        "final_wait_seconds_avg",
        "baseline_carbon_cost",
        "carbon_cost_saving_vs_baseline",
        "carbon_saving",
    ]
    initial_summary = {field: "0" for field in summary_fields}
    initial_summary.update({"batch_size": "2", "requests_total": "3"})
    for filename in ("summary.csv",):
        with (run_dir / filename).open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=summary_fields)
            writer.writeheader()
            writer.writerow(initial_summary)
    (run_dir / "summary.json").write_text(json.dumps(initial_summary), encoding="utf-8")
    with (output_dir / "summary_by_n.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=summary_fields)
        writer.writeheader()
        writer.writerow(initial_summary)

    rb._trim_rust_outputs(
        output_dir,
        trim_after_slot=2,
        error_window_past=0,
        error_window_future=0,
        baseline_request_costs={1: 4.0, 2: 6.0, 3: 8.0},
    )

    _, retained = rb._read_csv(run_dir / "per_request.csv")
    assert [int(row["request_id"]) for row in retained] == [1, 3]
    assert [row["assigned_with_injection"] for row in retained] == ["true", "true"]
    _, runtime_assignments = rb._read_csv(run_dir / "assignments_runtime.csv")
    assert len(runtime_assignments) == 2
    _, summary_rows = rb._read_csv(run_dir / "summary.csv")
    assert int(summary_rows[0]["requests_scheduled"]) == 2
    assert int(summary_rows[0]["requests_unscheduled"]) == 1
    assert float(summary_rows[0]["carbon_cost"]) == 10.0
    assert float(summary_rows[0]["global_average_error"]) == 1.0
    assert float(summary_rows[0]["baseline_carbon_cost"]) == 12.0
    assert float(summary_rows[0]["carbon_saving"]) == pytest.approx(100 / 6)
    _, summary_by_n_rows = rb._read_csv(output_dir / "summary_by_n.csv")
    assert int(summary_by_n_rows[0]["requests_scheduled"]) == 2
    _, timeslots = rb._read_csv(run_dir / "per_timeslot.csv")
    assert len(timeslots) == 2
    assert int(timeslots[1]["real_request_count"]) == 1
    retained_json = json.loads((run_dir / "per_request.json").read_text())["rows"]
    assert len(retained_json) == 2
    assert [row["assigned_with_injection"] for row in retained_json] == ["true", "true"]
    assert json.loads((run_dir / "summary.json").read_text())["requests_scheduled"] == 2


def test_rust_invocation_uses_scenario_input_horizon_for_trimming(tmp_path, monkeypatch):
    scenario_path = tmp_path / "scenario.json"
    rb._generate_scenario(
        {"id": "trim", "seed": 1, "total_slots": 3, "requests_per_slot": 1.0},
        scenario_path,
        trim_future_assignments=True,
    )
    output_dir = tmp_path / "rust_carryover"
    baseline_request_path = (
        tmp_path / "rust_baseline" / "baseline_greedy" / "per_request.csv"
    )
    baseline_request_path.parent.mkdir(parents=True)
    baseline_request_path.write_text(
        "request_id,carbon_cost\n0,1.0\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "runner.json"
    config_path.write_text(
        json.dumps({
            "scenario_path": str(scenario_path),
            "output_dir": str(output_dir),
        }),
        encoding="utf-8",
    )
    trimmed = {}
    monkeypatch.setattr(rb.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        rb,
        "_trim_rust_outputs",
        lambda path, cutoff, past, future, costs: trimmed.update({
            "output_dir": path,
            "cutoff": cutoff,
            "past": past,
            "future": future,
            "costs": costs,
        }),
    )

    rb._run_rust_binary(Path("nshift"), config_path)

    assert trimmed == {
        "output_dir": output_dir,
        "cutoff": 3,
        "past": 12,
        "future": 14,
        "costs": {0: 1.0},
    }
    assert not config_path.exists()


# ─── _format_run_folder_name ───────────────────────────────────────────────

def test_folder_name_realtime_disabled_uses_zero_token():
    dt = datetime(2026, 7, 9, 11, 23)
    name = rb._format_run_folder_name("mybattery", dt, 20, False, 0.5, False, "serialized", 0)
    assert name == "mybattery_0709_1123_20_roll0_0_F_S"


def test_folder_name_realtime_enabled_strips_dot_from_scale():
    dt = datetime(2026, 7, 9, 11, 23)
    name = rb._format_run_folder_name("mybattery", dt, 20, True, 0.5, True, "serialized", 0)
    assert name == "mybattery_0709_1123_20_roll0_05_T_S"


def test_run_phase_removes_only_new_incomplete_folder_after_interrupt(
    tmp_path, monkeypatch
):
    output_root = tmp_path / "results"
    output_root.mkdir()
    battery_id = "suite_flat_unit_0"
    previous_run = output_root / f"{battery_id}_previous"
    previous_run.mkdir()
    saved_config = {}

    def save_config(_path, config):
        saved_config.update(config)

    def interrupt_run():
        active_battery_id = saved_config["battery_id"]
        incomplete_run = output_root / f"{active_battery_id}_incomplete"
        incomplete_run.mkdir()
        completed_run = output_root / f"{active_battery_id}_completed"
        completed_run.mkdir()
        for filename in (
            f"battery_results_{active_battery_id}.csv",
            f"battery_timings_{active_battery_id}.csv",
            f"per_n_timings_{active_battery_id}.csv",
            "README.md",
        ):
            (completed_run / filename).touch()
        raise KeyboardInterrupt

    monkeypatch.setattr(mr, "save_config", save_config)
    monkeypatch.setattr(mr, "run_battery", interrupt_run)
    phase = mr.PhaseGrid("unit", {}, [{}])

    with pytest.raises(KeyboardInterrupt):
        mr.run_phase(
            {"output_dir": str(output_root), "scenarios": []},
            phase,
            "suite",
            "flat",
        )

    assert previous_run.is_dir()
    assert not (output_root / f"{battery_id}_incomplete").exists()
    assert (output_root / f"{battery_id}_completed").is_dir()


def test_folder_name_scale_with_leading_zero():
    dt = datetime(2026, 7, 9, 11, 23)
    name = rb._format_run_folder_name("mybattery", dt, 20, True, 0.05, True, "serialized", 0)
    assert name.endswith("_005_T_S")


def test_folder_name_altstrat_false_when_no_strategies():
    dt = datetime(2026, 1, 1, 0, 0)
    name = rb._format_run_folder_name("b", dt, 5, False, 0.05, False, "serialized", 0)
    assert name.endswith("_F_S")


def test_folder_name_merge_swarm_mode_uses_m_token():
    dt = datetime(2026, 1, 1, 0, 0)
    name = rb._format_run_folder_name("b", dt, 5, False, 0.05, False, "merge", 0)
    assert name.endswith("_F_M")


def test_folder_name_includes_rollback_token():
    dt = datetime(2026, 1, 1, 0, 0)
    name = rb._format_run_folder_name("b", dt, 5, False, 0.05, False, "serialized", 4)
    assert "_roll4_" in name


# ─── _parse_rust_config_defaults ───────────────────────────────────────────

def test_parse_rust_config_defaults_matches_known_values():
    values = rb._parse_rust_config_defaults(rb.RUST_CONFIG_RS)
    assert values["error_window_past"] == "12"
    assert values["error_window_future"] == "8"
    assert values["error_window_past_decay_slots"] == "0"
    assert values["dp_pruning_k"] == "1200"
    assert values["max_error_threshold"] == "4.0"
    # String literals should have the Rust `.to_string()` suffix stripped.
    assert values["dp_pruning_method"] == '"beam"'
    assert ".to_string()" not in values["dp_pruning_method"]


def test_parse_rust_config_defaults_skips_multiline_vec_fields():
    values = rb._parse_rust_config_defaults(rb.RUST_CONFIG_RS)
    # flavours/capacity_tiers span multiple lines and use brackets; the
    # line-based scan intentionally skips them rather than mis-parsing.
    assert "flavours" not in values
    assert "capacity_tiers" not in values


def test_parse_rust_config_defaults_missing_file_returns_empty(tmp_path):
    missing = tmp_path / "does_not_exist.rs"
    assert rb._parse_rust_config_defaults(missing) == {}


# ─── _row_label ─────────────────────────────────────────────────────────────

def test_row_label_plain_dp_mode():
    row = {"infeasibility_mode": "min_error_greedy", "batch_size": 4}
    assert rb._row_label(row) == "N=4"


def test_row_label_online_strategy():
    row = {"infeasibility_mode": "online_bandit", "batch_size": 8}
    assert rb._row_label(row) == "bandit (N=8)"


def test_row_label_offline_strategy():
    row = {"infeasibility_mode": "offline_singleton_best", "batch_size": 0}
    assert rb._row_label(row) == "offline_singleton_best"
    accurate_row = {
        "infeasibility_mode": "offline_singleton_best_accurate",
        "batch_size": 0,
    }
    assert rb._row_label(accurate_row) == "offline_singleton_best_accurate"


# ─── _top3_markdown_table / _build_top3_section ────────────────────────────

def _make_row(scenario_id, mode, batch_size, carbon_cost, saving=10.0, solver_ms=1.0):
    return {
        "scenario_id": scenario_id,
        "infeasibility_mode": mode,
        "batch_size": batch_size,
        "carbon_cost": carbon_cost,
        "carbon_saving": saving,
        "solver_time_ms_avg": solver_ms,
    }


def test_top3_table_picks_three_lowest_carbon_cost():
    rows = [
        _make_row("s1", "min_error_greedy", 1, 50.0),
        _make_row("s1", "min_error_greedy", 4, 10.0),
        _make_row("s1", "min_error_greedy", 8, 30.0),
        _make_row("s1", "min_error_greedy", 12, 20.0),
    ]
    table = rb._top3_markdown_table(rows, elapsed_lookup={}, scenario_id="s1")
    # Only the 3 lowest-cost rows (10, 20, 30) should appear, in ascending order.
    lines = [l for l in table.splitlines() if l.startswith("|") and "Config" not in l and "---" not in l]
    assert len(lines) == 3
    assert "N=4" in lines[0] and "10.000" in lines[0]
    assert "N=12" in lines[1] and "20.000" in lines[1]
    assert "N=8" in lines[2] and "30.000" in lines[2]


def test_top3_table_empty_rows_shows_placeholder():
    table = rb._top3_markdown_table([], elapsed_lookup={}, scenario_id="s1")
    assert "no executions" in table


def test_top3_table_includes_elapsed_when_available():
    rows = [_make_row("s1", "min_error_greedy", 4, 10.0)]
    elapsed_lookup = {("s1", "min_error_greedy", 4): 12.345}
    table = rb._top3_markdown_table(rows, elapsed_lookup, "s1")
    assert "12.3" in table


def test_top3_table_elapsed_na_when_missing():
    rows = [_make_row("s1", "min_error_greedy", 4, 10.0)]
    table = rb._top3_markdown_table(rows, elapsed_lookup={}, scenario_id="s1")
    assert "n/a" in table


def test_build_top3_section_groups_by_scenario_and_category():
    all_rows = [
        _make_row("s1", "min_error_greedy", 1, 40.0),
        _make_row("s1", "online_bandit", 1, 35.0),
        _make_row("s1", "offline_singleton_best", 0, 33.0),
        _make_row("s2", "min_error_greedy", 1, 20.0),
    ]
    section = rb._build_top3_section(all_rows, per_n_timing_rows=[])
    assert "### s1" in section
    assert "### s2" in section
    assert "DP recovery modes" in section
    assert "Online alternative strategies" in section
    assert "Offline alternative strategies" in section
    # s2 has no online/offline rows -> placeholder must appear at least once for it.
    assert "no executions in this category" in section
