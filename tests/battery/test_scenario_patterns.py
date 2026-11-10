"""Tests for simulation-only scenario input patterns."""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

BATTERY_DIR = Path(__file__).resolve().parent
SCENARIOS_DIR = BATTERY_DIR / "scenarios"
sys.path.insert(0, str(SCENARIOS_DIR))
sys.path.insert(0, str(BATTERY_DIR))

import multiple_runs  # noqa: E402
from scenario_io import (  # noqa: E402
    _request_rate_per_slot,
    _resolve_carbon_wave_flags,
    generate_scenario_data,
)


def _generate(
    pattern: str,
    *,
    rate: float = 2.5,
    slots: int = 24,
    carbon_request_alignment: str = "phase-shifted",
    request_pattern_start: str = "low",
):
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
        input_pattern=pattern,
        carbon_request_alignment=carbon_request_alignment,
        request_pattern_start=request_pattern_start,
    )


def test_flat_pattern_has_seeded_low_count_jitter_and_preserves_total():
    scenario = _generate("flat", rate=60.5, slots=128)
    repeated = _generate("flat", rate=60.5, slots=128)

    assert "prehistory_slots" not in scenario
    assert not any(key.startswith("prehistory_") for key in scenario["metadata"])
    counts = [0] * 128
    for request in scenario["requests"]:
        counts[request["arrival_slot"]] += 1

    assert sum(counts) == round(60.5 * 128)
    assert max(counts) > min(counts)
    mean = sum(counts) / len(counts)
    stddev = math.sqrt(sum((count - mean) ** 2 for count in counts) / len(counts))
    assert 2.0 < stddev < 4.0
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
    assert scenario["metadata"]["carbon_request_alignment"] == "phase-shifted"
    assert scenario["metadata"]["request_pattern_start"] == "low"
    assert scenario["carbon_forecast"] == flat_scenario["carbon_forecast"]


def test_sinusoidal_request_wave_can_start_low_or_high_without_changing_mean():
    common = {
        "total_slots": 24,
        "carbon_intensity_cycle_slots": 24,
        "requests_per_slot": 100.0,
        "night_floor_ratio": 0.4,
    }
    low = _request_rate_per_slot(**common)
    high = _request_rate_per_slot(**common, inverted=True)

    assert low[0] < low[12]
    assert high[0] > high[12]
    assert sum(low) / len(low) == pytest.approx(100.0)
    assert sum(high) / len(high) == pytest.approx(100.0)


@pytest.mark.parametrize(
    (
        "alignment",
        "request_start",
        "configured_shift",
        "carbon_inverted",
        "phase_shift",
    ),
    [
        ("in-phase", "low", 0.4, False, 0.0),
        ("phase-opposition", "low", 0.4, True, 0.0),
        ("phase-shifted", "low", 0.4, False, 0.4),
        ("in-phase", "high", 0.4, True, 0.0),
        ("phase-opposition", "high", 0.4, False, 0.0),
        ("phase-shifted", "high", 0.4, True, 0.4),
    ],
)
def test_carbon_alignment_is_relative_to_request_wave_orientation(
    alignment, request_start, configured_shift, carbon_inverted, phase_shift
):
    assert _resolve_carbon_wave_flags(
        alignment,
        request_start,
        75.0,
        170.0,
        configured_shift,
    ) == (carbon_inverted, phase_shift)


def test_carbon_alignment_accounts_for_reversed_carbon_baseline_direction():
    assert _resolve_carbon_wave_flags("in-phase", "low", 160.0, 70.0, 0.25) == (True, 0.0)
    assert _resolve_carbon_wave_flags("phase-opposition", "low", 160.0, 70.0, 0.25) == (
        False,
        0.0,
    )


def test_generated_carbon_wave_follows_selected_alignment():
    def forecast(alignment: str, request_start: str) -> list[float]:
        scenario = generate_scenario_data(
            seed=1,
            total_slots=24,
            slot_duration_seconds=60.0,
            requests_per_slot=10.0,
            request_rate_std_factor=0.0,
            deadline_min_slack=0,
            deadline_max_slack=1,
            error_window_past=0,
            error_window_future=0,
            max_error_threshold=4.0,
            carbon_night_max=75.0,
            carbon_day_min=170.0,
            carbon_noise_std=0.0,
            carbon_request_alignment=alignment,
            request_pattern_start=request_start,
        )
        return scenario["carbon_forecast"]

    in_phase = forecast("in-phase", "low")
    opposed = forecast("phase-opposition", "low")
    shifted = forecast("phase-shifted", "low")
    inverted_in_phase = forecast("in-phase", "high")

    assert in_phase[12] > in_phase[0]
    assert opposed[12] < opposed[0]
    assert inverted_in_phase[12] < inverted_in_phase[0]
    assert shifted != in_phase


def test_unknown_scenario_pattern_is_rejected():
    with pytest.raises(ValueError, match="Unknown input pattern"):
        _generate("bursty")

    with pytest.raises(ValueError, match="Unknown carbon request alignment"):
        generate_scenario_data(
            seed=1,
            total_slots=2,
            slot_duration_seconds=60.0,
            requests_per_slot=1.0,
            request_rate_std_factor=0.25,
            deadline_min_slack=0,
            deadline_max_slack=1,
            error_window_past=0,
            error_window_future=0,
            max_error_threshold=4.0,
            carbon_request_alignment="unknown",
        )


def test_multiple_runs_patterns_get_distinct_scenario_ids():
    base = {"scenarios": [{"id": "workload", "seed": 9}]}

    flat = multiple_runs._with_input_pattern(base, "flat")
    sinusoidal = multiple_runs._with_input_pattern(base, "sinusoidal")

    assert flat["scenarios"][0]["id"] == "workload_FLAT"
    assert flat["scenarios"][0]["input_pattern"] == "flat"
    assert sinusoidal["scenarios"][0]["id"] == "workload_sine"
    assert sinusoidal["scenarios"][0]["input_pattern"] == "sinusoidal"
    assert base["scenarios"][0] == {"id": "workload", "seed": 9}


def test_multiple_runs_applies_sinusoidal_wave_constants_without_mutating_base(monkeypatch):
    monkeypatch.setattr(multiple_runs, "CARBON_REQUEST_ALIGNMENT", "phase-shifted")
    monkeypatch.setattr(multiple_runs, "REQUEST_PATTERN_START", "high")
    monkeypatch.setattr(multiple_runs, "CARBON_PHASE_SHIFT_FRACTION", 0.4)
    base = {
        "scenarios": [
            {"id": "workload_sine", "input_pattern": "sinusoidal"},
            {"id": "workload_FLAT", "input_pattern": "flat"},
        ]
    }

    configured = multiple_runs._with_sinusoidal_wave_settings(base)

    sinusoidal, flat = configured["scenarios"]
    assert sinusoidal["carbon_request_alignment"] == "phase-shifted"
    assert sinusoidal["carbon_phase_shift_fraction"] == 0.4
    assert sinusoidal["request_pattern_start"] == "high"
    assert sinusoidal["id"] == "workload_sine_PS_shift0p4_H"
    assert flat == base["scenarios"][1]
    assert base["scenarios"][0] == {
        "id": "workload_sine",
        "input_pattern": "sinusoidal",
    }


@pytest.mark.parametrize(
    ("alignment", "request_start", "fraction", "expected_suffix"),
    [
        ("in-phase", "low", 0.25, "_IP_L"),
        ("phase-opposition", "high", 0.25, "_PO_H"),
        ("phase-shifted", "low", 0.25, "_PS_shift0p25_L"),
    ],
)
def test_multiple_runs_uses_compact_wave_tags(
    monkeypatch, alignment, request_start, fraction, expected_suffix
):
    monkeypatch.setattr(multiple_runs, "CARBON_REQUEST_ALIGNMENT", alignment)
    monkeypatch.setattr(multiple_runs, "REQUEST_PATTERN_START", request_start)
    monkeypatch.setattr(multiple_runs, "CARBON_PHASE_SHIFT_FRACTION", fraction)

    assert multiple_runs._wave_output_suffix() == expected_suffix


def test_multiple_runs_rejects_out_of_range_phase_shift(monkeypatch):
    monkeypatch.setattr(multiple_runs, "CARBON_PHASE_SHIFT_FRACTION", 0.75)
    with pytest.raises(ValueError, match="between 0 and 0.5"):
        multiple_runs._with_sinusoidal_wave_settings(
            {"scenarios": [{"id": "workload", "input_pattern": "sinusoidal"}]}
        )
