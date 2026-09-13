"""Runs a timeslot-aware request plan: either in real time (waits for each
request's `start_at`) or in emulated time (fires each timeslot's requests
immediately, then synchronizes via carbonshift's and the executor's
`POST /admin/advance-slot` instead of waiting for real time to pass).
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from .carbonshift_client import CarbonshiftError, submit
from .config import settings
from .timeslots import ceil_to_slot_end, slot_index
from .tracker import RequestTracker, TrackedRequest

logger = logging.getLogger("client.plan_runner")


def run_plan(tracker: RequestTracker, requests_spec: list[dict[str, Any]], slot_minutes: float,
             mode: str, executor_url: Optional[str]) -> tuple[str, int]:
    batch_id = uuid.uuid4().hex
    slots = _group_by_slot(requests_spec, slot_minutes)
    threading.Thread(
        target=_run, args=(tracker, slots, slot_minutes, mode, executor_url, batch_id),
        daemon=True, name=f"plan-{batch_id}",
    ).start()
    return batch_id, len(slots)


def _group_by_slot(requests_spec: list[dict[str, Any]], slot_minutes: float) -> dict[int, list[dict[str, Any]]]:
    if not requests_spec:
        return {}
    reference = min(r["start_at"] for r in requests_spec)
    groups: dict[int, list[dict[str, Any]]] = {}
    for r in requests_spec:
        idx = slot_index(r["start_at"], slot_minutes, reference)
        groups.setdefault(idx, []).append(r)
    return groups


def _run(tracker: RequestTracker, slots: dict[int, list[dict[str, Any]]], slot_minutes: float,
          mode: str, executor_url: Optional[str], batch_id: str) -> None:
    callback_url = f"{settings.self_base_url}/callback"
    for idx in sorted(slots):
        if mode == "realtime":
            _wait_until(slots[idx][0]["start_at"])

        for r in slots[idx]:
            submitted_at = datetime.now(timezone.utc)
            deadline_end = ceil_to_slot_end(r["deadline_at"], slot_minutes)
            deadline_seconds = max((deadline_end - submitted_at).total_seconds(), 1.0)
            payload = {"task": r["task"], "input": r["input"]}
            try:
                ack = submit(deadline_seconds, callback_url, payload, task_id=r["task"])
            except CarbonshiftError:
                logger.exception("plan %s slot %d: submit failed", batch_id, idx)
                continue
            request_id = str(ack.get("request_id"))
            tracker.add(TrackedRequest(request_id, r["task"], deadline_seconds, submitted_at, ack))
            logger.info("plan %s slot %d: submitted request_id=%s", batch_id, idx, request_id)

        if mode == "emulated":
            _advance_slot(executor_url, batch_id, idx)


def _wait_until(target: datetime) -> None:
    delay = (target - datetime.now(timezone.utc)).total_seconds()
    if delay > 0:
        time.sleep(delay)


def _advance_slot(executor_url: Optional[str], batch_id: str, idx: int) -> None:
    # 1) carbonshift: flush this slot's requests and block until its own
    #    dispatcher has handed them off to the executor.
    try:
        resp = requests.post(f"{settings.carbonshift_url}/v1/admin/advance-slot",
                              timeout=settings.admin_timeout_seconds)
        resp.raise_for_status()
        logger.info("plan %s: carbonshift advanced past slot %d -> %s", batch_id, idx, resp.json())
    except requests.RequestException:
        logger.exception("plan %s: carbonshift advance-slot failed at slot %d", batch_id, idx)
        return

    # 2) executor: run everything now due and block until it confirms done.
    base_url = (executor_url or settings.executor_admin_url).rstrip("/")
    try:
        resp = requests.post(f"{base_url}/admin/advance-slot", timeout=settings.admin_timeout_seconds)
        resp.raise_for_status()
        logger.info("plan %s: executor advanced past slot %d -> %s", batch_id, idx, resp.json())
    except requests.RequestException:
        logger.exception("plan %s: executor advance-slot failed at slot %d", batch_id, idx)
