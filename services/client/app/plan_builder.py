"""Shared helper for building timeslot-aware request plans, used by both
`scripts/run_emulation.py` and `tests/battery/run_battery.py` (kept here,
alongside `datasets.py`, so both callers build plans the exact same way).
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .datasets import load_examples
from .timeslots import floor_to_slot


def build_requests(task: str, count: int, per_slot: int, slot_minutes: float,
                    source: str, seed: int, reference: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Builds `count` requests, `per_slot` of them per timeslot, `slot_minutes`
    apart, starting at `reference` (defaults to now). Each entry is a
    `PlanRequestSpec`-shaped dict: `{task, input, start_at, deadline_at}`.

    `reference` is floored to a slot boundary so the plan's slots line up with
    the provider's global slots — otherwise the client cannot map "the system
    entered slot N" onto "which of my requests belong to N".
    """
    reference = floor_to_slot(reference or datetime.now(timezone.utc), slot_minutes)
    examples = load_examples(task, count, seed=seed, source=source)
    out = []
    for i, ex in enumerate(examples):
        slot_idx = i // per_slot
        # Spread the slot's requests across it (so they don't all share one
        # instant) while keeping their order. The jitter is applied to
        # `start_at` ONLY: the deadline is a property of the *slot*, not of
        # the jittered start, so it must stay pinned to the slot's end.
        subslot_size = slot_minutes / per_slot
        subslot_index = i % per_slot
        jitter = random.uniform(0, subslot_size)
        slot_start = reference + timedelta(minutes=slot_minutes * slot_idx)
        start_at = slot_start + timedelta(minutes=subslot_index * subslot_size + jitter)
        deadline_at = slot_start + timedelta(minutes=slot_minutes)
        out.append({
            "task": task, "input": ex["input"],
            "start_at": start_at.isoformat(), "deadline_at": deadline_at.isoformat(),
        })
    return out
