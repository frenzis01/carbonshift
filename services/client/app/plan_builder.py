"""Shared helper for building timeslot-aware request plans, used by both
`scripts/run_emulation.py` and `tests/battery/run_battery.py` (kept here,
alongside `datasets.py`, so both callers build plans the exact same way).
"""
from __future__ import annotations
from timeslots import floor_to_slot

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .datasets import load_examples
import random


def build_requests(task: str, count: int, per_slot: int, slot_minutes: float,
                    source: str, seed: int, reference: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Builds `count` requests, `per_slot` of them per timeslot, `slot_minutes`
    apart, starting at `reference` (defaults to now). Each entry is a
    `PlanRequestSpec`-shaped dict: `{task, input, start_at, deadline_at}`.
    """
    reference = floor_to_slot(reference or datetime.now(timezone.utc), slot_minutes)
    examples = load_examples(task, count, seed=seed, source=source)
    out = []
    for i, ex in enumerate(examples):
        slot_idx = i // per_slot
        # Add random jitter but keep ordering within the slot
        # Discretize slot in per_slot intervals to avoid collisions and enforce ordering
        subslot_size = slot_minutes / per_slot
        subslot_index = i % per_slot
        jitter = random.uniform(0, subslot_size)
        current_subslot_delta = subslot_index * subslot_size
        start_at = reference + timedelta(minutes=slot_minutes * slot_idx + current_subslot_delta + jitter)
        deadline_at = start_at + timedelta(minutes=slot_minutes)
        out.append({
            "task": task, "input": ex["input"],
            "start_at": start_at.isoformat(), "deadline_at": deadline_at.isoformat(),
        })
    return out
