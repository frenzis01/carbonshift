"""Submits a plan's requests to carbonshift, one slot at a time.

The client no longer drives the clock: the provider does (see
`provider/ARCHITECTURE.md` §2). This module therefore contains no loop, no
thread and no advance-slot call — it exposes exactly one operation, "submit
the requests belonging to this slot", which `POST /v1/tick` invokes.

What used to live here and is deliberately gone:

* `run_plan` / `_run` — a thread that fired every slot in a loop and drove
  carbonshift's and the executor's clocks itself. Superseded by the provider's
  fan-out, which does the same thing in the correct order with retries.
* `_perturbed_actual_ci` — the client inventing a "real" carbon intensity by
  perturbing the forecast. The provider now owns that (and models it
  correctly: a measurement is an event, not a property of a slot).
* `_advance_slot` — the client calling the peers' advance endpoints. It
  swallowed every failure (`logger.exception(...)` then `return`), which is
  exactly the silent-desync behaviour the provider's 503-on-failure replaces.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from .carbonshift_client import CarbonshiftError, submit
from .config import settings
from .timeslots import ceil_to_slot_end, slot_index
from .tracker import RequestTracker, TrackedRequest

logger = logging.getLogger("client.plan_runner")


def get_batch_from_slot(requests_spec: list[dict[str, Any]], slot_minutes: float,
                        plan_index: int, reference: datetime) -> list[dict[str, Any]]:
    """The requests belonging to `plan_index` within this plan.

    `reference` is the plan's own start instant (a slot boundary), passed in
    rather than inferred from the data: deriving it from `min(start_at)` would
    measure from a jittered mid-slot instant, which works only by luck.
    """
    slotted = _group_by_slot(requests_spec, slot_minutes, reference)
    return slotted.get(plan_index, [])


def send_slot_batch(tracker: RequestTracker, batch: list[dict[str, Any]],
                    slot_minutes: float) -> int:
    """Submit every request in `batch` to carbonshift, synchronously.

    Returns the number actually submitted. Synchronous on purpose: the
    provider blocks on this call, and that is what guarantees slot N's work is
    in carbonshift's queue *before* carbonshift is told to process slot N.

    A submission failure is logged and skipped rather than raised: one bad
    request should not abort the whole slot. The request is simply not tracked,
    so it never appears in the metrics — which is honest, since it was never
    accepted.
    """
    batch_id = uuid.uuid4().hex
    submitted = 0
    callback_url = f"{settings.self_base_url}/callback"

    for r in batch:
        submitted_at = datetime.now(timezone.utc)
        deadline_end = ceil_to_slot_end(r["deadline_at"], slot_minutes)
        deadline_seconds = max((deadline_end - submitted_at).total_seconds(), 1.0)
        payload = {"task": r["task"], "input": r["input"]}
        try:
            ack = submit(deadline_seconds, callback_url, payload, task_id=r["task"])
        except CarbonshiftError:
            logger.exception("batch %s: submit failed for task=%s", batch_id, r["task"])
            continue

        # `request_id` comes from carbonshift's ack, so the TrackedRequest can
        # only be built *after* a successful submit.
        tracker.add(TrackedRequest(
            str(ack.get("request_id")), r["task"], deadline_seconds, submitted_at, ack,
        ))
        submitted += 1
        logger.info("batch %s: submitted task=%s deadline_seconds=%.2f",
                    batch_id, r["task"], deadline_seconds)

    return submitted


def _group_by_slot(requests_spec: list[dict[str, Any]], slot_minutes: float,
                   reference: datetime) -> dict[int, list[dict[str, Any]]]:
    """Group requests by their 0-based slot index relative to `reference`."""
    if not requests_spec:
        return {}
    groups: dict[int, list[dict[str, Any]]] = {}
    for r in requests_spec:
        idx = slot_index(r["start_at"], slot_minutes, reference)
        groups.setdefault(idx, []).append(r)
    return groups
