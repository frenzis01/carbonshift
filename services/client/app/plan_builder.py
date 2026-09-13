"""Shared helper for building timeslot-aware request plans, used by both
`scripts/run_emulation.py` and `tests/battery/run_battery.py` (kept here,
alongside `datasets.py`, so both callers build plans the exact same way).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .datasets import load_examples


def build_requests(task: str, count: int, per_slot: int, slot_minutes: float,
                    source: str, seed: int, reference: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Builds `count` requests, `per_slot` of them per timeslot, `slot_minutes`
    apart, starting at `reference` (defaults to now). Each entry is a
    `PlanRequestSpec`-shaped dict: `{task, input, start_at, deadline_at}`.
    """
    reference = reference or datetime.now(timezone.utc)
    examples = load_examples(task, count, seed=seed, source=source)
    out = []
    for i, ex in enumerate(examples):
        slot_idx = i // per_slot
        start_at = reference + timedelta(minutes=slot_minutes * slot_idx)
        deadline_at = start_at + timedelta(minutes=slot_minutes)
        out.append({
            "task": task, "input": ex["input"],
            "start_at": start_at.isoformat(), "deadline_at": deadline_at.isoformat(),
        })
    return out
