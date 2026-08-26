"""Scenario generation and I/O helpers for N-shift speed benchmarks."""

from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Any, Dict, List, Optional


def _serialize_capacity_tiers(tiers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return a JSON-safe copy of capacity tiers (None max_requests stays null)."""
    result = []
    for tier in tiers:
        max_req = tier["max_requests"]
        result.append({
            "max_requests": None if max_req is None else int(max_req),
            "multiplier": float(tier["multiplier"]),
        })
    return result


def _resolve_path(path_value: str, base_dir: Path) -> Path:
    candidate = Path(path_value)
    if candidate.is_absolute():
        return candidate
    return (base_dir / candidate).resolve()


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def save_rows_as_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})

def _request_rate_per_slot(
    total_slots: int,
    carbon_intensity_cycle_slots: int,
    requests_per_slot: float,
    sunrise_fraction: float = 0.30,
    sunset_fraction: float = 0.78,
    transition_slope: float = 20.0,
    night_floor_ratio: float = 0.40,
) -> List[float]:
    """
    Compute the mean request rate for each slot following the same daylight cycle
    as carbon intensity.

    The returned values are scaled so their average over ``total_slots`` equals
    ``requests_per_slot``.  ``night_floor_ratio`` controls how many requests
    arrive during the night valley as a fraction of the daytime peak rate
    (before averaging), mirroring the carbon-intensity night/day ratio.
    """
    cycle = max(1, int(carbon_intensity_cycle_slots))

    cycle_shape: List[float] = []
    for s in range(cycle):
        x = s / cycle
        rise = 1.0 / (1.0 + math.exp(-transition_slope * (x - sunrise_fraction)))
        fall = 1.0 / (1.0 + math.exp(-transition_slope * (x - sunset_fraction)))
        df = max(0.0, rise - fall)
        cycle_shape.append(df)

    max_df = max(cycle_shape) if any(v > 0 for v in cycle_shape) else 1.0

    # Map [0..max_df] → [night_floor_ratio..1.0]
    cycle_shape = [
        night_floor_ratio + (1.0 - night_floor_ratio) * (v / max_df)
        for v in cycle_shape
    ]

    avg_shape = sum(cycle_shape) / len(cycle_shape)
    scale = requests_per_slot / avg_shape if avg_shape > 0 else requests_per_slot

    return [cycle_shape[slot % cycle] * scale for slot in range(total_slots)]


def generate_carbon_intensity_forecast(
    total_slots: int,
    carbon_intensity_cycle_slots: int,
    seed: int,
    night_max: float = 160.0,
    day_min: float = 70.0,
    sunrise_fraction: float = 0.25,
    sunset_fraction: float = 0.75,
    transition_slope: float = 18.0,
    noise_std: float = 2.0,
    noise_persistence: float = 0.95,
    inverted: bool = False,
    phase_shifted: bool = False,
) -> List[float]:
    """
    Generate synthetic carbon intensity data with realistic day/night plateaus.

    Parameters
    ----------
    total_slots:
        Number of samples to generate.

    carbon_intensity_cycle_slots:
        Number of slots representing one full day.

    seed:
        Random seed.

    night_max:
        Typical carbon intensity during the night peak.

    day_min:
        Typical carbon intensity during the daytime minimum.

    sunrise_fraction:
        Position of the morning transition within the cycle
        (0.25 ≈ 06:00 if cycle represents 24h).

    sunset_fraction:
        Position of the evening transition within the cycle
        (0.75 ≈ 18:00 if cycle represents 24h).

    transition_slope:
        Controls how sharp the transitions are.
        Higher values -> steeper transitions.
        Lower values -> smoother transitions.

    noise_std:
        Standard deviation of the short-term fluctuations.

    noise_persistence:
        Autocorrelation factor of the fluctuations.
        Typical values: 0.90 - 0.99.

    Returns
    -------
    List[float]
        Synthetic carbon intensity forecast.
    """

    rng = random.Random(seed)

    cycle = max(1, int(carbon_intensity_cycle_slots))

    forecast: List[float] = []

    noise_state = 0.0

    for slot in range(total_slots):
        x = (slot % cycle) / cycle

        if phase_shifted:
            x = (x + 0.25) % 1.0

        rise = 1.0 / (
            1.0 + math.exp(
                -transition_slope * (x - sunrise_fraction)
            )
        )

        fall = 1.0 / (
            1.0 + math.exp(
                -transition_slope * (x - sunset_fraction)
            )
        )

        daylight_factor = rise - fall
        
        # in case the user wants to invert the wave 
        # (e.g. testing purposes or simulating a different region with opposite solar cycle)
        if inverted:
            daylight_factor = -daylight_factor

        trend = (
            night_max
            - (night_max - day_min) * daylight_factor
        )

        noise_state = (
            noise_persistence * noise_state
            + rng.gauss(0.0, noise_std)
        )

        value = trend + noise_state

        forecast.append(
            round(max(1.0, value), 6)
        )

    return forecast


def generate_scenario_data(
    *,
    seed: int,
    total_slots: int,
    slot_duration_seconds: float,
    requests_per_slot: float,
    request_rate_std_factor: float,
    deadline_min_slack: int,
    deadline_max_slack: int,
    error_window_past: int,
    error_window_future: int,
    max_error_threshold: float,
    prehistory_error_ratio: float,
    carbon_random_noise_amplitude: float = 40.0,
    prehistory_mock_influence: float = 1.0,
    error_window_past_decay_slots: int = 0,
    include_prehistory: bool = True,
    carbon_intensity_cycle_slots: int = 24,
    capacity_tiers: Optional[List[Dict[str, Any]]] = None,
    carbon_night_max: float = 160.0,
    carbon_day_min: float = 70.0,
    carbon_sunrise_fraction: float = 0.30,
    carbon_sunset_fraction: float = 0.78,
    carbon_transition_slope: float = 20.0,
    carbon_noise_std: float = 1.5,
    carbon_noise_persistence: float = 0.97,
    carbon_inverted: bool = False,
    carbon_shifted_phase: bool = False,
    request_night_floor_ratio: float = 0.40,
) -> Dict[str, Any]:
    """
    Build a deterministic scenario payload.

    Includes:
    - full request set (id, arrival_time, deadline_slot)
    - per-slot carbon forecast
    - synthetic prehistory slots used by modeled error calculations

    The ``carbon_*`` and ``request_night_floor_ratio`` parameters shape the
    daylight wave (see `generate_carbon_intensity_forecast` /
    `_request_rate_per_slot`). Their defaults match `scenario_config.py`;
    callers (e.g. `generate_scenario.py`) should pass explicit values sourced
    from that module rather than relying on these hardcoded fallbacks.
    """
    rng = random.Random(seed)

    forecast = generate_carbon_intensity_forecast(
        total_slots=total_slots,
        carbon_intensity_cycle_slots=carbon_intensity_cycle_slots,
        seed=seed,
        night_max=carbon_night_max,
        day_min=carbon_day_min,
        sunrise_fraction=carbon_sunrise_fraction,
        sunset_fraction=carbon_sunset_fraction,
        transition_slope=carbon_transition_slope,
        noise_std=carbon_noise_std,
        noise_persistence=carbon_noise_persistence,
        inverted=carbon_inverted,
        phase_shifted=carbon_shifted_phase,
    )

    # Per-slot mean request rate following the same daylight wave as carbon intensity.
    # requests_per_slot is the target average across all slots.
    rate_per_slot = _request_rate_per_slot(
        total_slots=total_slots,
        carbon_intensity_cycle_slots=carbon_intensity_cycle_slots,
        requests_per_slot=requests_per_slot,
        sunrise_fraction=carbon_sunrise_fraction,
        sunset_fraction=carbon_sunset_fraction,
        transition_slope=carbon_transition_slope,
        night_floor_ratio=request_night_floor_ratio,
    )

    requests: List[Dict[str, Any]] = []
    request_id = 0
    for slot in range(total_slots):
        mean_rate = rate_per_slot[slot]
        # NOTE on tuning randomness:
        # To increase/decrease noise in the request counts, change
        # `request_rate_std_factor` (CLI: --request-rate-std-factor, default:
        # scenario_config.REQUEST_RATE_STD_FACTOR) — NOT this sigma formula.
        # sigma is proportional to mean_rate (sigma = mean_rate * factor), so
        # busy slots naturally get more absolute noise than quiet slots while
        # keeping the *relative* spread constant (a Poisson-like scaling).
        # Typical values: 0.15-0.2 = low noise (counts hug the wave closely),
        # 0.3 (current default) = moderate noise, 0.5+ = high noise (peaks/
        # valleys can blur together). The `max(1.0, ...)` floor only prevents
        # sigma from collapsing to 0 in near-empty slots; it does not
        # otherwise affect the noise level.
        sigma = max(1.0, mean_rate * request_rate_std_factor)
        count = max(0, int(round(rng.gauss(mean_rate, sigma))))
        for _ in range(count):
            slack = rng.randint(deadline_min_slack, deadline_max_slack)
            deadline_slot = min(total_slots - 1, slot + slack)
            requests.append(
                {
                    "request_id": request_id,
                    "arrival_slot": slot,
                    "arrival_time": slot * slot_duration_seconds,  # fractional offset added below
                    "deadline_slot": deadline_slot,
                }
            )
            request_id += 1

    # Add a sub-slot fractional offset to each arrival_time using a separate RNG
    # so the main rng sequence (counts, deadlines) is unaffected by this change.
    arrival_rng = random.Random(seed ^ 0xA1B2C3D4)
    for req in requests:
        req["arrival_time"] = round(
            req["arrival_time"] + arrival_rng.uniform(0.0, slot_duration_seconds), 6
        )

    prehistory_slots: List[Dict[str, Any]] = []
    synthetic_error = max_error_threshold * prehistory_error_ratio
    prehistory_influence = max(0.0, min(1.0, float(prehistory_mock_influence)))
    if include_prehistory:
        for slot in range(-error_window_past, 0):
            mean_rate = rate_per_slot[slot % total_slots]
            sigma = max(1.0, mean_rate * request_rate_std_factor)
            raw_count = max(0, int(round(rng.gauss(mean_rate, sigma))))
            count = int(round(raw_count * prehistory_influence))
            prehistory_slots.append(
                {
                    "slot": slot,
                    "request_count": count,
                    "error_per_request": round(synthetic_error, 6),
                }
            )
    else:
        prehistory_influence = 0.0

    requests.sort(key=lambda item: (int(item["arrival_slot"]), float(item["arrival_time"]), int(item["request_id"])))

    return {
        "metadata": {
            "seed": int(seed),
            "total_slots": int(total_slots),
            "slot_duration_seconds": float(slot_duration_seconds),
            "requests_per_slot": float(requests_per_slot),
            "request_rate_std_factor": float(request_rate_std_factor),
            "deadline_min_slack": int(deadline_min_slack),
            "deadline_max_slack": int(deadline_max_slack),
            "error_window_past": int(error_window_past),
            "error_window_future": int(error_window_future),
            "error_window_past_decay_slots": int(error_window_past_decay_slots),
            "max_error_threshold": float(max_error_threshold),
            "prehistory_error_ratio": float(prehistory_error_ratio),
            "carbon_random_noise_amplitude": float(carbon_random_noise_amplitude),
            "prehistory_mock_influence": prehistory_influence,
            "prehistory_enabled": bool(include_prehistory),
            "carbon_intensity_cycle_slots": int(carbon_intensity_cycle_slots),
            **({"capacity_tiers": _serialize_capacity_tiers(capacity_tiers)} if capacity_tiers is not None else {}),
        },
        "carbon_forecast": forecast,
        "requests": requests,
        "prehistory_slots": prehistory_slots,
    }


def load_runner_config(config_path: Path) -> Dict[str, Any]:
    config_raw = load_json(config_path)
    if "batch_sizes" not in config_raw or not isinstance(config_raw["batch_sizes"], list):
        raise ValueError("Runner config must contain 'batch_sizes' as a list.")
    if "scenario_path" not in config_raw:
        raise ValueError("Runner config must contain 'scenario_path'.")
    if "output_dir" not in config_raw:
        raise ValueError("Runner config must contain 'output_dir'.")

    batch_sizes = [int(value) for value in config_raw["batch_sizes"]]
    if not batch_sizes or any(value <= 0 for value in batch_sizes):
        raise ValueError("batch_sizes must contain positive integers.")

    base_dir = config_path.parent
    scenario_path = _resolve_path(str(config_raw["scenario_path"]), base_dir)
    output_dir = _resolve_path(str(config_raw["output_dir"]), base_dir)
    runner_cfg = dict(config_raw.get("runner", {}))

    return {
        "batch_sizes": batch_sizes,
        "scenario_path": scenario_path,
        "output_dir": output_dir,
        "runner": runner_cfg,
    }
