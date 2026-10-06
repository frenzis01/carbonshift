"""Shared helper for building timeslot-aware request plans, used by both
`scripts/run_emulation.py` and `tests/battery/run_battery.py` (kept here,
alongside datasets.py, so both callers build plans the exact same way).
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from .datasets import load_examples
from .timeslots import floor_to_slot


def build_requests(
    task: str,
    total_slots: int,
    per_slot_avg: int,
    slot_minutes: float,
    source: str,
    seed: int,
    pattern: str = "flat",
    cycle_slots: Optional[int] = None,
    reference: Optional[datetime] = None,
    qos_profile_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Builds timeslot requests for total_slots with an average of per_slot_avg
    requests per slot using the requested distribution pattern ("flat", "sinusoidal", "random").
    
    `reference` is floored to a slot boundary so the plan's slots line up with
        the provider's global slots — otherwise the client cannot map "the system
        entered slot N" onto "which of my requests belong to N".
    """
    reference = floor_to_slot(reference or datetime.now(timezone.utc), slot_minutes)
    rng = random.Random(seed)

    if cycle_slots is None:
        cycle_slots = max(1, int(round(720.0 / slot_minutes)))  # 12 hours

    match pattern:
        case "flat":
            count_per_slot = _flat_count(total_slots, requests_per_slot=per_slot_avg)
        case "sinusoidal":
            count_per_slot = _sinusoidal_count(
                total_slots, cycle_slots=cycle_slots, requests_per_slot=per_slot_avg, rng=rng
            )
        case "random":
            count_per_slot = _random_count(total_slots, requests_per_slot=per_slot_avg, rng=rng)
        case _:
            raise ValueError(f"Unknown pattern: {pattern}")

    total_requests = sum(count_per_slot)
    examples = load_examples(task, total_requests, seed=seed, source=source)

    out = []
    ex_idx = 0
    jitter_rng = random.Random(seed ^ 0xA1B2C3D4)

    for slot_idx, count in enumerate(count_per_slot):
        if count <= 0:
            continue
        subslot_size = slot_minutes / count
        slot_start = reference + timedelta(minutes=slot_minutes * slot_idx)
        deadline_at = slot_start + timedelta(minutes=slot_minutes)

        for subslot_index in range(count):
            ex = examples[ex_idx]
            ex_idx += 1
            jitter = jitter_rng.uniform(0, subslot_size)
            start_at = slot_start + timedelta(minutes=subslot_index * subslot_size + jitter)
            request = {
                "task": task,
                "input": ex["input"],
                "start_at": start_at.isoformat(),
                "deadline_at": deadline_at.isoformat(),
            }
            if qos_profile_id is not None:
                request["qos_profile_id"] = qos_profile_id
            out.append(request)

    return out


def _sinusoidal_count(
    total_slots: int,
    cycle_slots: int,
    requests_per_slot: int,
    requests_rate_std_factor: float = 0.25,
    rng: Optional[random.Random] = None,
) -> List[int]:
    """
    Given the mean request rate per slot (computed internally as a sinusoidal function) and the number of requests per slot (`requests_per_slot`),
    compute the actual number of requests for each slot.

    Returns a list of integers representing the number of requests for each slot.
    """
    if rng is None:
        rng = random.Random()
    rate_per_slot = _request_rate_per_slot(total_slots, cycle_slots, requests_per_slot)
    out: List[int] = []
    for slot in range(total_slots):
        mean_rate = rate_per_slot[slot]
        sigma = max(1.0, mean_rate * requests_rate_std_factor)
        count = max(0, int(round(rng.gauss(mean_rate, sigma))))
        out.append(count)
    return out


def _flat_count(total_slots: int, requests_per_slot: int) -> List[int]:
    """
    Compute a flat request count for each slot, where every slot has the same number of requests.

    Returns a list of integers representing the number of requests for each slot.
    """
    return [requests_per_slot for _ in range(total_slots)]


def _random_count(
    total_slots: int, requests_per_slot: int, rng: Optional[random.Random] = None
) -> List[int]:
    """
    Compute a random request count for each slot, where the number of requests per slot is drawn from a uniform distribution between 0 and twice the specified `requests_per_slot`.

    Returns a list of integers representing the number of requests for each slot.
    """
    if rng is None:
        rng = random.Random()
    out: List[int] = []
    for _ in range(total_slots):
        count = max(0, int(round(rng.uniform(0, 2 * requests_per_slot))))
        out.append(count)
    return out


def _request_rate_per_slot(
    total_slots: int,
    cycle_slots: int,
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
    
    cycle = max(1, int(cycle_slots))

    cycle_shape: List[float] = []
    for s in range(cycle):
        x = s / cycle
        rise = 1.0 / (1.0 + math.exp(-transition_slope * (x - sunrise_fraction)))
        fall = 1.0 / (1.0 + math.exp(-transition_slope * (x - sunset_fraction)))
        df = max(0.0, rise - fall)
        cycle_shape.append(df)

    max_df = max(cycle_shape) if any(v > 0 for v in cycle_shape) else 1.0

    cycle_shape = [
        night_floor_ratio + (1.0 - night_floor_ratio) * (v / max_df)
        for v in cycle_shape
    ]

    avg_shape = sum(cycle_shape) / len(cycle_shape)
    scale = (float(requests_per_slot)) / avg_shape if avg_shape > 0 else float(requests_per_slot)

    return [cycle_shape[slot % cycle] * scale for slot in range(total_slots)]