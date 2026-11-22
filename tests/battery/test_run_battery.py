"""Unit tests for the pure/pathless helper functions in run_battery.py.

These cover run-output packaging logic, scenario metadata wiring, and the
TOP-3 README tables. They do not invoke the Rust binary — see the module
docstring in run_battery.py for the manual end-to-end smoke test procedure.
"""
from __future__ import annotations

import csv
import json
import signal
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "scenarios"))

import multiple_runs as mr  # noqa: E402
import run_battery as rb  # noqa: E402
import scenario_config  # noqa: E402
from nb_utils import require_output_schema  # noqa: E402


def test_battery_runner_does_not_emit_obsolete_runtime_controls(tmp_path):
    config_path = rb._write_rust_config(
        tmp_path / "scenario.json",
        [],
        tmp_path / "output",
        False,
    )
    try:
        config = json.loads(config_path.read_text())
    finally:
        config_path.unlink(missing_ok=True)

    assert "online_swarm_mode" not in config["runner"]
    assert "rollback_max_consecutive" not in config["runner"]
    assert (
        config["runner"]["max_batch_solver_parallelism"]
        == rb.DEFAULT_MAX_BATCH_SOLVER_PARALLELISM
    )
    assert config["runner"]["aco_n_ants"] == rb.DEFAULT_ACO_N_ANTS
    assert config["runner"]["aco_n_iterations"] == rb.DEFAULT_ACO_N_ITERATIONS
    assert "infeasibility_recovery_mode" not in config["runner"]


def test_battery_runner_forwards_flavour_override_to_nshift(tmp_path):
    flavours = [{"name": "Accurate", "error": 0.0, "duration_sec": 60.0}]
    config_path = rb._write_rust_config(
        tmp_path / "scenario.json",
        [1],
        tmp_path / "output",
        False,
        flavours=flavours,
    )
    try:
        config = json.loads(config_path.read_text())
    finally:
        config_path.unlink(missing_ok=True)

    assert config["runner"]["flavours"] == flavours


def test_battery_runner_forwards_aco_defaults_to_nshift(tmp_path):
    config_path = rb._write_rust_config(
        tmp_path / "scenario.json",
        [],
        tmp_path / "output",
        False,
        aco_n_ants=24,
        aco_n_iterations=6,
    )
    try:
        config = json.loads(config_path.read_text())
    finally:
        config_path.unlink(missing_ok=True)

    assert config["runner"]["aco_n_ants"] == 24
    assert config["runner"]["aco_n_iterations"] == 6


def test_carbon_saving_formatter_reports_amount_and_percentage():
    result_row = {
        "baseline_carbon_cost": 1000.0,
        "carbon_cost": 500.0,
        "carbon_saving": 50.0,
    }

    assert rb._format_carbon_saving(result_row) == "500.000 gCO2 (50.0%)"


def test_output_schema_guard_accepts_only_the_declared_version(tmp_path):
    manifest_path = tmp_path / "output_schema.json"
    manifest_path.write_text(
        json.dumps({"schema_version": 2, "schema_name": "carbonshift.nshift"}),
        encoding="utf-8",
    )
    manifest = require_output_schema(tmp_path, schema_name="carbonshift.nshift")
    assert manifest["schema_version"] == 2

    manifest_path.write_text(
        json.dumps({"schema_version": 1, "schema_name": "carbonshift.nshift"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Unsupported output schema version"):
        require_output_schema(tmp_path, schema_name="carbonshift.nshift")


def test_multiple_runs_keeps_request_strategies_in_per_request_batch_sweep():
    assert mr.PER_REQUEST_ONLINE_CONTENT["online_strategies"] == [
        "bandit",
        "singleton_best",
        "hazard",
        "random_slotflav",
        "random_flav",
    ]
    assert mr.PER_REQUEST_ONLINE_CONTENT["online_batch_sizes"] == [
        1,
        8,
        96,
        256,
        1024,
    ]
    assert mr.SWARM_ONLINE_CONTENT["online_strategies"] == ["ant_colony"]


def test_multiple_runs_includes_random_reference_policies():
    assert {"random_slotflav", "random_flav"}.issubset(
        mr.PER_REQUEST_ONLINE_CONTENT["online_strategies"]
    )
    assert {"offline_random_slotflav", "offline_random_flav"}.issubset(
        mr.OFFLINE_CONTENT["additional_strategies"]
    )


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


def test_generated_scenario_uses_flat_carbon_pattern_override(tmp_path):
    scenario = rb._generate_scenario(
        {
            "id": "flat-ci",
            "seed": 1,
            "total_slots": 32,
            "requests_per_slot": 1.0,
            "input_pattern": "flat",
            "carbon_pattern": "flat",
        },
        tmp_path / "flat-ci.json",
    )

    center = (
        scenario_config.CARBON_NIGHT_MAX + scenario_config.CARBON_DAY_MIN
    ) / 2.0
    assert scenario["metadata"]["input_pattern"] == "flat"
    assert scenario["metadata"]["carbon_pattern"] == "flat"
    assert all(0.9 * center <= value <= 1.1 * center for value in scenario["carbon_forecast"])


def test_generated_scenario_uses_sinusoidal_wave_overrides(tmp_path):
    scenario = rb._generate_scenario(
        {
            "id": "wave",
            "seed": 3,
            "total_slots": 24,
            "requests_per_slot": 4.0,
            "input_pattern": "sinusoidal",
            "carbon_request_alignment": "phase-opposition",
            "carbon_phase_shift_fraction": 0.4,
            "request_pattern_start": "high",
        },
        tmp_path / "wave.json",
    )

    assert scenario["metadata"]["carbon_request_alignment"] == "phase-opposition"
    assert scenario["metadata"]["carbon_phase_shift_fraction"] == 0.4
    assert scenario["metadata"]["request_pattern_start"] == "high"


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
            "qos_profile_id": "default-text-generation",
            "deadline_slot": "1",
            "scheduled_slot": "0",
            "carbon_cost": "3.0",
            "queue_wait_seconds": "2.0",
            "final_wait_seconds": "4.0",
            "flavour_name": "Accurate",
            "fallback_assigned": "false",
            "late_assigned": "false",
        },
        {
            "request_id": "2",
            "qos_profile_id": "default-text-generation",
            "deadline_slot": "1",
            "scheduled_slot": "2",
            "carbon_cost": "5.0",
            "queue_wait_seconds": "3.0",
            "final_wait_seconds": "5.0",
            "flavour_name": "Balanced",
            "fallback_assigned": "true",
            "late_assigned": "true",
        },
        {
            "request_id": "3",
            "qos_profile_id": "default-text-generation",
            "deadline_slot": "1",
            "scheduled_slot": "1",
            "carbon_cost": "7.0",
            "queue_wait_seconds": "6.0",
            "final_wait_seconds": "8.0",
            "flavour_name": "Fast",
            "fallback_assigned": "false",
            "late_assigned": "false",
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
        "reqs_total",
        "reqs_scheduled",
        "reqs_unscheduled",
        "reqs_late",
        "max_lateness_slots",
        "carbon_cost",
        "global_average_error",
        "reqs_fallback_assigned",
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
    initial_summary.update({"batch_size": "2", "reqs_total": "3"})
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
        flavour_errors={
            ("default-text-generation", "Accurate"): 0.0,
            ("default-text-generation", "Balanced"): 5.0,
            ("default-text-generation", "Fast"): 2.0,
        },
    )

    _, retained = rb._read_csv(run_dir / "per_request.csv")
    assert [int(row["request_id"]) for row in retained] == [1, 3]
    assert [row["fallback_assigned"] for row in retained] == ["false", "false"]
    _, runtime_assignments = rb._read_csv(run_dir / "assignments_runtime.csv")
    assert len(runtime_assignments) == 2
    _, summary_rows = rb._read_csv(run_dir / "summary.csv")
    assert int(summary_rows[0]["reqs_scheduled"]) == 2
    assert int(summary_rows[0]["reqs_unscheduled"]) == 1
    assert float(summary_rows[0]["carbon_cost"]) == 10.0
    assert float(summary_rows[0]["global_average_error"]) == 1.0
    assert int(summary_rows[0]["reqs_fallback_assigned"]) == 0
    assert float(summary_rows[0]["baseline_carbon_cost"]) == 12.0
    assert float(summary_rows[0]["carbon_saving"]) == pytest.approx(100 / 6)
    _, summary_by_n_rows = rb._read_csv(output_dir / "summary_by_n.csv")
    assert int(summary_by_n_rows[0]["reqs_scheduled"]) == 2
    _, timeslots = rb._read_csv(run_dir / "per_timeslot.csv")
    assert len(timeslots) == 2
    assert int(timeslots[1]["reqs_count"]) == 1
    retained_json = json.loads((run_dir / "per_request.json").read_text())["rows"]
    assert len(retained_json) == 2
    assert [row["fallback_assigned"] for row in retained_json] == ["false", "false"]
    assert json.loads((run_dir / "summary.json").read_text())["reqs_scheduled"] == 2


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
        lambda path, cutoff, past, future, costs, flavour_errors: trimmed.update({
            "output_dir": path,
            "cutoff": cutoff,
            "past": past,
            "future": future,
            "costs": costs,
            "flavour_errors": flavour_errors,
        }),
    )
    monkeypatch.setattr(
        rb,
        "_read_flavour_errors",
        lambda _path: {("default-text-generation", "Accurate"): 0.0},
    )

    rb._run_rust_binary(Path("nshift"), config_path)

    assert trimmed == {
        "output_dir": output_dir,
        "cutoff": 3,
        "past": 12,
        "future": 14,
        "costs": {0: 1.0},
        "flavour_errors": {("default-text-generation", "Accurate"): 0.0},
    }
    assert not config_path.exists()


# ─── _format_run_folder_name ───────────────────────────────────────────────

def test_folder_name_realtime_disabled_omits_scale_token():
    dt = datetime(2026, 7, 9, 11, 23)
    name = rb._format_run_folder_name("mybattery", dt, 20, False, 0.5)
    assert name == "mybattery_0709_1123_20"


def test_folder_name_realtime_enabled_strips_dot_from_scale():
    dt = datetime(2026, 7, 9, 11, 23)
    name = rb._format_run_folder_name("mybattery", dt, 20, True, 0.5)
    assert name == "mybattery_0709_1123_20_05"


def test_run_phase_removes_only_new_incomplete_folder_after_interrupt(
    tmp_path, monkeypatch
):
    output_root = tmp_path / "results"
    output_root.mkdir()
    battery_id = "suite_FLAT_perrequest_0"
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

    with pytest.raises(KeyboardInterrupt):
        mr.run_phase(
            {"output_dir": str(output_root), "scenarios": []},
            mr.PhaseGrid("per_request", {}, [{}]),
            "suite",
            "flat",
            "sinusoidal",
        )

    assert saved_config["battery_id"] == battery_id
    assert previous_run.is_dir()
    assert not (output_root / f"{battery_id}_incomplete").exists()
    assert (output_root / f"{battery_id}_completed").is_dir()


@pytest.mark.skipif(mr.os.name != "posix", reason="tests POSIX process-group interrupt")
def test_run_battery_forwards_interrupt_and_waits_for_child(monkeypatch):
    observed = {}

    class FakeProcess:
        pid = 12345

        def __init__(self):
            self.wait_calls = 0

        def wait(self, timeout=None):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise KeyboardInterrupt
            observed["wait_timeout"] = timeout
            return -signal.SIGINT

        def poll(self):
            return None

    process = FakeProcess()

    def fake_popen(command, *, cwd, start_new_session):
        observed["command"] = command
        observed["cwd"] = cwd
        observed["start_new_session"] = start_new_session
        return process

    monkeypatch.setattr(mr.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        mr.os,
        "killpg",
        lambda pid, sig: observed.update(killpg=(pid, sig)),
    )

    with pytest.raises(KeyboardInterrupt):
        mr.run_battery()

    assert observed["start_new_session"] is (mr.os.name == "posix")
    if mr.os.name == "posix":
        assert observed["killpg"] == (process.pid, signal.SIGINT)
    assert observed["wait_timeout"] == 10
    assert process.wait_calls == 2


def test_multiple_runs_default_patterns_is_a_single_pattern_not_characters():
    assert len(mr.INPUT_PATTERNS) == 1
    assert mr.INPUT_PATTERNS[0] in mr.SUPPORTED_INPUT_PATTERNS
    assert len(mr.CARBON_PATTERNS) == 1
    assert mr.CARBON_PATTERNS[0] in mr.CARBON_INTENSITY_PATTERNS
    patterned = mr._with_input_pattern({"scenarios": [{"id": "tiny"}]}, mr.INPUT_PATTERNS[0])
    assert patterned["scenarios"][0]["input_pattern"] == mr.INPUT_PATTERNS[0]


def test_multiple_runs_uses_pattern_constants_as_a_cartesian_sweep(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "battery_config.json"
    config_path.write_text("{}")
    base_config = {"scenarios": []}
    calls = []

    monkeypatch.setattr(mr, "CONFIG_PATH", config_path)
    monkeypatch.setattr(mr, "load_config", lambda _path: base_config)
    monkeypatch.setattr(mr, "save_config", lambda *_args: None)
    monkeypatch.setattr(mr, "INPUT_PATTERNS", ("flat", "sinusoidal"))
    monkeypatch.setattr(mr, "CARBON_PATTERNS", ("flat", "sinusoidal"))
    monkeypatch.setattr(mr, "run_phase", lambda _cfg, _phase, _prefix, input_pattern, carbon_pattern: (
        calls.append((input_pattern, carbon_pattern)) or 0
    ))

    assert mr.main(["--phase", "dp"]) == 0
    assert calls == [
        ("flat", "flat"),
        ("flat", "sinusoidal"),
        ("sinusoidal", "flat"),
        ("sinusoidal", "sinusoidal"),
    ]


@pytest.mark.parametrize(
    "option",
    ["--input-pattern", "--carbon-pattern"],
)
def test_multiple_runs_rejects_pattern_cli_overrides(option):
    with pytest.raises(SystemExit) as error:
        mr.main([option, "flat"])

    assert error.value.code == 2


def test_multiple_runs_flavours_override_battery_config_for_each_phase(
    tmp_path, monkeypatch
):
    flavours = [{"name": "Accurate", "error": 0.0, "duration_sec": 60.0}]
    monkeypatch.setattr(mr, "FLAVOURS", flavours)
    saved_configs = []
    monkeypatch.setattr(mr, "save_config", lambda _path, cfg: saved_configs.append(cfg))
    monkeypatch.setattr(mr, "run_battery", lambda: 0)

    status = mr.run_phase(
        {
            "output_dir": str(tmp_path),
            "scenarios": [],
            "flavours": [
                {"name": "Accurate", "error": 0.0, "duration_sec": 60.0},
                {"name": "Balanced", "error": 2.5, "duration_sec": 30.0},
            ],
        },
        mr.PhaseGrid("dp", {"batch_sizes": [1]}, [{}]),
        "flavour-test",
        "flat",
        "sinusoidal",
    )

    assert status == 0
    assert saved_configs[0]["flavours"] == flavours


def test_main_reports_interrupt_without_traceback_and_restores_config(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "battery_config.json"
    config_path.write_text("{}")
    base_config = {"battery_id": "original"}
    saved = []

    monkeypatch.setattr(mr, "CONFIG_PATH", config_path)
    monkeypatch.setattr(mr, "load_config", lambda _path: base_config)
    monkeypatch.setattr(mr, "save_config", lambda _path, cfg: saved.append(cfg.copy()))

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(mr, "run_phase", interrupt)

    assert mr.main(["--phase", "dp"]) == 130
    assert saved == [base_config]
    captured = capsys.readouterr()
    assert "Interrupted by user" in captured.err
    assert "Traceback" not in captured.err


def test_folder_name_scale_with_leading_zero():
    dt = datetime(2026, 7, 9, 11, 23)
    name = rb._format_run_folder_name("mybattery", dt, 20, True, 0.05)
    assert name.endswith("_005")


# ─── _parse_rust_config_defaults ───────────────────────────────────────────

def test_parse_rust_config_defaults_matches_known_values():
    values = rb._parse_rust_config_defaults(rb.RUST_CONFIG_RS)
    assert values["error_window_past"] == "12"
    assert values["error_window_future"] == "14"
    assert values["error_window_past_decay_slots"] == "12"
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
    row = {"strategy": "min_error_greedy", "batch_size": 4}
    assert rb._row_label(row) == "N=4"


def test_row_label_online_strategy():
    row = {"strategy": "online_bandit", "batch_size": 8}
    assert rb._row_label(row) == "bandit (N=8)"


def test_row_label_offline_strategy():
    row = {"strategy": "offline_singleton_best", "batch_size": 0}
    assert rb._row_label(row) == "offline_singleton_best"
    accurate_row = {
        "strategy": "offline_singleton_best_accurate",
        "batch_size": 0,
    }
    assert rb._row_label(accurate_row) == "offline_singleton_best_accurate"


# ─── _top3_markdown_table / _build_top3_section ────────────────────────────

def _make_row(scenario_id, strategy, batch_size, carbon_cost, saving=10.0, solver_ms=1.0):
    return {
        "scenario_id": scenario_id,
        "strategy": strategy,
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
    assert "DP scheduling runs" in section
    assert "Online alternative strategies" in section
    assert "Offline alternative strategies" in section
    # s2 has no online/offline rows -> placeholder must appear at least once for it.
    assert "no executions in this category" in section
