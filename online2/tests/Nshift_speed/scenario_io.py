"""Scenario generation and I/O helpers for N-shift speed benchmarks."""

from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Any, Dict, List


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
) -> Dict[str, Any]:
    """
    Build a deterministic scenario payload.

    Includes:
    - full request set (id, arrival_time, deadline_slot)
    - per-slot carbon forecast
    - synthetic prehistory slots used by modeled error calculations
    """
    rng = random.Random(seed)
    sigma = max(1.0, requests_per_slot * request_rate_std_factor)

    forecast: List[float] = []
    cycle = 10
    base_carbon = 40.0
    amplitude = 100.0
    noise = max(0.0, float(carbon_random_noise_amplitude))
    for slot in range(total_slots):
        phase = 2.0 * math.pi * (slot % cycle) / cycle
        value = base_carbon + amplitude * (1.0 + 0.8 * math.cos(phase)) + rng.uniform(-noise, noise)
        forecast.append(round(max(base_carbon, value), 6))

    requests: List[Dict[str, Any]] = []
    request_id = 0
    for slot in range(total_slots):
        count = max(0, int(round(rng.gauss(requests_per_slot, sigma))))
        for _ in range(count):
            slack = rng.randint(deadline_min_slack, deadline_max_slack)
            deadline_slot = min(total_slots - 1, slot + slack)
            requests.append(
                {
                    "request_id": request_id,
                    "arrival_slot": slot,
                    "arrival_time": round(slot * slot_duration_seconds, 6),
                    "deadline_slot": deadline_slot,
                }
            )
            request_id += 1

    prehistory_slots: List[Dict[str, Any]] = []
    synthetic_error = max_error_threshold * prehistory_error_ratio
    prehistory_influence = max(0.0, min(1.0, float(prehistory_mock_influence)))
    for slot in range(-error_window_past, 0):
        raw_count = max(0, int(round(rng.gauss(requests_per_slot, sigma))))
        count = int(round(raw_count * prehistory_influence))
        prehistory_slots.append(
            {
                "slot": slot,
                "request_count": count,
                "error_per_request": round(synthetic_error, 6),
            }
        )

    requests.sort(key=lambda item: (int(item["arrival_slot"]), int(item["request_id"])))

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
            "carbon_random_noise_amplitude": noise,
            "prehistory_mock_influence": prehistory_influence,
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
