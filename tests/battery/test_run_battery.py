"""Unit tests for the pure/pathless helper functions in run_battery.py.

These cover the run-output packaging logic (folder naming, config.rs scalar
parsing, and the TOP-3 README tables) added for the per-run results folder
feature. They do not invoke the Rust binary or generate real scenarios —
see the module docstring in run_battery.py for the manual end-to-end smoke
test procedure used to validate the full pipeline.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "scenarios"))

import run_battery as rb  # noqa: E402


# ─── _format_run_folder_name ───────────────────────────────────────────────

def test_folder_name_realtime_disabled_uses_zero_token():
    dt = datetime(2026, 7, 9, 11, 23)
    name = rb._format_run_folder_name("mybattery", dt, 20, False, 0.5, False, "serialized", 0)
    assert name == "mybattery_0709_1123_20_roll0_0_F_S"


def test_folder_name_realtime_enabled_strips_dot_from_scale():
    dt = datetime(2026, 7, 9, 11, 23)
    name = rb._format_run_folder_name("mybattery", dt, 20, True, 0.5, True, "serialized", 0)
    assert name == "mybattery_0709_1123_20_roll0_05_T_S"


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
    row = {"infeasibility_mode": "offline_greedy_cheapest", "batch_size": 0}
    assert rb._row_label(row) == "greedy_cheapest"


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
        _make_row("s1", "offline_greedy_cheapest", 0, 33.0),
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
