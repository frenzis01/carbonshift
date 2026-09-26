"""In-memory registry of *plans* for the client.

## Why this module exists

The client used to run a plan by starting a thread that fired every slot's
requests in a loop, driving the clock itself. Now the **provider owns the
clock** (see `provider/ARCHITECTURE.md` §2): it tells the client "the system
has entered slot N" via `POST /v1/tick`, and the client answers "here is slot
N's work".

That inversion means the plan can no longer be a running loop — it has to be
*stored* between ticks, because each tick is a separate HTTP request. This
module is that store. It holds **plans only**.

## What this module deliberately does NOT hold

**No clock.** There is no `current_slot` counter here, and there must never be
one. The provider is the single owner of "which slot are we in"; every other
component *derives* its position from the slot it is told about. A local
counter would drift the moment a tick is retried or missed, and the two
services would silently disagree about which slot is current — the exact class
of bug this design exists to prevent.

The client's position is therefore always computed, never stored:

    plan_index = tick.current_slot - plan["plan_start_slot"]

## Scope

Module-level mutable state, shared across requests and not persisted. That is
consistent with the rest of the client (see its README: no persistence across
restarts, only the JSONL metrics log survives). A `Lock` guards mutations
because `/run/send-plan` and `/v1/tick` can arrive concurrently.
"""
from __future__ import annotations

import copy
import itertools
import threading
from datetime import datetime
from typing import Any, Optional

from .timeslots import floor_to_slot, slot_of

#: plan_id -> plan record. See `store_plan` for the record's shape.
plans: dict[int, dict[str, Any]] = {}

#: Monotonic plan ids. `len(plans) + 1` would collide if a plan were ever
#: removed, so use a counter that only ever moves forward.
_next_plan_id = itertools.count(1)

#: Guards `plans` — `/run/send-plan` and `/v1/tick` can run concurrently.
_lock = threading.Lock()


def _as_datetime(value: Any) -> datetime:
    """Normalise a plan's `start_at`/`deadline_at` to a `datetime`.

    Both representations reach here in practice: `SendPlanRequest`'s Pydantic
    models hand over real `datetime` objects, while a hand-written plan (or a
    JSON round-trip) gives ISO strings. Normalising **once, at intake** means
    every downstream consumer (`slot_of`, `slot_index`, `ceil_to_slot_end`)
    can assume a `datetime` and none of them needs to care which it got.
    """
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def store_plan(requests_spec: list[dict[str, Any]], slot_minutes: float,
               mode: str, executor_url: Optional[str]) -> int:
    """Register a plan and return its id.

    The plan is stored, not started: the provider's ticks drive it. The two
    derived fields are what make the tick→plan-index mapping a subtraction:

    * `plan_start_slot` — the **global** slot the plan begins in, computed
      from its first request's `start_at` using the same epoch and slot length
      the provider uses. This is the anchor that ties the plan to the
      provider's clock.
    * `slot_count` — how many slots the plan spans, so a tick past the end is
      recognised as "nothing to do" rather than an error.
    """
    requests = copy.deepcopy(requests_spec)
    if not requests:
        raise ValueError("a plan must contain at least one request")

    # Normalise the time fields once, so nothing downstream has to guess.
    for r in requests:
        r["start_at"] = _as_datetime(r["start_at"])
        r["deadline_at"] = _as_datetime(r["deadline_at"])

    # `build_requests` already floors the reference to a slot boundary, but
    # recompute from the data so a hand-written plan is handled too.
    plan_start_slot = slot_of(floor_to_slot(requests[0]["start_at"], slot_minutes), slot_minutes)

    last_start = max(r["start_at"] for r in requests)
    last_slot = slot_of(floor_to_slot(last_start, slot_minutes), slot_minutes)
    slot_count = last_slot - plan_start_slot + 1

    with _lock:
        plan_id = next(_next_plan_id)
        plans[plan_id] = {
            "requests": requests,
            "slot_minutes": slot_minutes,
            "mode": mode,
            "executor_url": executor_url,
            "plan_start_slot": plan_start_slot,
            "slot_count": slot_count,
            # Highest slot already processed, so a retried tick is idempotent
            # (the provider retries on failure; see INTERFACE.md).
            "last_processed_slot": None,
        }
    return plan_id


def get_plan_for_slot(slot: int) -> list[tuple[int, int]]:
    """`(plan_id, plan_index)` pairs whose requests fall in global `slot`.

    Returns an empty list for a slot before the plan starts or after it ends —
    that is a normal outcome (the provider ticks on its own schedule, which
    need not coincide with the plan's), not an error.
    """
    result: list[tuple[int, int]] = []
    with _lock:
        for plan_id, plan in plans.items():
            idx = slot - plan["plan_start_slot"]
            if 0 <= idx < plan["slot_count"]:
                result.append((plan_id, idx))
    return result


def get_plan(plan_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        return plans.get(plan_id)


def get_requests_from_plan(plan_id: int) -> Optional[list[dict[str, Any]]]:
    plan = get_plan(plan_id)
    return None if plan is None else plan["requests"]


def mark_slot_processed(plan_id: int, slot: int) -> None:
    """Record that `slot` has been handled for `plan_id`."""
    with _lock:
        plan = plans.get(plan_id)
        if plan is not None:
            plan["last_processed_slot"] = slot


def is_slot_already_processed(plan_id: int, slot: int) -> bool:
    """True if `slot` (or a later one) was already handled for `plan_id`.

    Makes a retried tick a no-op instead of a double submission — the same
    at-least-once + idempotent contract the provider uses with `expect_slot`.
    """
    with _lock:
        plan = plans.get(plan_id)
        if plan is None or plan["last_processed_slot"] is None:
            return False
        return slot <= plan["last_processed_slot"]


def reset() -> None:
    """Clear all plans. Test helper — the client has no persistence anyway."""
    with _lock:
        plans.clear()
