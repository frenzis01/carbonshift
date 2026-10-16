"""Tests for simulation-only scenario input patterns."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BATTERY_DIR = Path(__file__).resolve().parent
SCENARIOS_DIR = BATTERY_DIR / "scenarios"
sys.path.insert(0, str(SCENARIOS_DIR))
sys.path.insert(0, str(BATTERY_DIR))

import multiple_runs  # noqa: E402
from scenario_io import generate_scenario_data  # noqa: E402


def _generate(pattern: str, *, rate: float = 2.5, slots: int = 24):
    return generate_scenario_data(
        seed=173,
        total_slots=slots,
        slot_duration_seconds=60.0,
        requests_per_slot=rate,
        request_rate_std_factor=0.25,
        deadline_min_slack=0,
        deadline_max_slack=4,
        error_window_past=2,
        error_window_future=2,
        max_error_threshold=4.0,
        prehistory_error_ratio=1.0,
        input_pattern=pattern,
    )


def test_flat_pattern_spreads_fractional_average_evenly_and_is_repeatable():
    scenario = _generate("flat", slots=8)
    repeated = _generate("flat", slots=8)

    counts = [0] * 8
    for request in scenario["requests"]:
        counts[request["arrival_slot"]] += 1

    assert counts == [2, 3, 3, 2, 2, 3, 3, 2]
    assert sum(counts) / len(counts) == 2.5
    assert scenario == repeated
    assert scenario["metadata"]["input_pattern"] == "flat"
    offsets = [
        request["arrival_time"] - request["arrival_slot"] * 60.0
        for request in scenario["requests"]
    ]
    assert all(0.0 <= offset < 60.0 for offset in offsets)
    assert len(set(offsets)) > 1


def test_sinusoidal_pattern_varies_counts_and_preserves_arrival_jitter():
    scenario = _generate("sinusoidal")
    flat_scenario = _generate("flat")
    counts = [0] * scenario["metadata"]["total_slots"]
    for request in scenario["requests"]:
        counts[request["arrival_slot"]] += 1
        subslot_offset = request["arrival_time"] - request["arrival_slot"] * 60.0
        assert 0.0 <= subslot_offset < 60.0

    assert max(counts) > min(counts)
    assert scenario["metadata"]["input_pattern"] == "sinusoidal"
    assert scenario["carbon_forecast"] == flat_scenario["carbon_forecast"]


def test_unknown_scenario_pattern_is_rejected():
    with pytest.raises(ValueError, match="Unknown input pattern"):
        _generate("bursty")


def test_multiple_runs_patterns_get_distinct_scenario_ids():
    base = {"scenarios": [{"id": "workload", "seed": 9}]}

    flat = multiple_runs._with_input_pattern(base, "flat")
    sinusoidal = multiple_runs._with_input_pattern(base, "sinusoidal")

    assert flat["scenarios"][0]["id"] == "workload_flat"
    assert flat["scenarios"][0]["input_pattern"] == "flat"
    assert sinusoidal["scenarios"][0]["id"] == "workload_sinusoidal"
    assert sinusoidal["scenarios"][0]["input_pattern"] == "sinusoidal"
    assert base["scenarios"][0] == {"id": "workload", "seed": 9}
