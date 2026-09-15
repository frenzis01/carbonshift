"""Tracks every request submitted to carbonshift and the metrics derived
from its lifecycle (submit -> ack -> callback / timeout).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from .carbonshift_client import CarbonshiftError, get_status

logger = logging.getLogger("client.tracker")


def _stats(values: list[float]) -> dict[str, Optional[float]]:
    if not values:
        return {"avg": None, "min": None, "max": None}
    return {
        "avg": round(sum(values) / len(values), 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
    }


def _unix_to_iso(ts: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts is not None else None


class TrackedRequest:
    """`ack` is carbonshift's immediate `POST /v1/requests` response
    (status/scheduled_slot/eta_seconds/flavour/carbon_cost)."""

    def __init__(self, request_id: str, task: str, deadline_seconds: float,
                 submitted_at: datetime, ack: dict[str, Any]):
        self.request_id = request_id
        self.task = task
        self.deadline_seconds = deadline_seconds
        self.submitted_at = submitted_at
        self.ack = ack
        self.ack_received_at = datetime.now(timezone.utc)
        self.callback_received_at: Optional[datetime] = None
        self.status = "submitted"  # submitted -> completed | failed | timed_out
        self.success: Optional[bool] = None
        self.result: Optional[dict[str, Any]] = None
        self.error: Optional[str] = None
        # Real (not forecast-time) carbon cost, corrected by carbonshift once
        # the actual carbon intensity for the assignment's slot is known
        # (see carbonshift_client/README — reported via advance-slot).
        self.actual_carbon_cost: Optional[float] = None
        self.actual_baseline_carbon_cost: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        end_to_end_seconds = None
        late = None
        if self.callback_received_at:
            end_to_end_seconds = (self.callback_received_at - self.submitted_at).total_seconds()
            late = end_to_end_seconds > self.deadline_seconds
        carbon_cost = self.ack.get("carbon_cost")
        baseline_carbon_cost = self.ack.get("baseline_carbon_cost")
        carbon_saving_pct = None
        if carbon_cost is not None and baseline_carbon_cost:
            carbon_saving_pct = (baseline_carbon_cost - carbon_cost) / baseline_carbon_cost * 100
        # Same shape as carbon_saving_pct, but with the *actual* (real carbon
        # intensity corrected) costs when available, falling back to the
        # predicted ones for whichever side isn't corrected yet.
        actual_carbon_cost = self.actual_carbon_cost if self.actual_carbon_cost is not None else carbon_cost
        actual_baseline_carbon_cost = (
            self.actual_baseline_carbon_cost if self.actual_baseline_carbon_cost is not None else baseline_carbon_cost
        )
        actual_carbon_saving_pct = None
        if actual_carbon_cost is not None and actual_baseline_carbon_cost:
            actual_carbon_saving_pct = (
                (actual_baseline_carbon_cost - actual_carbon_cost) / actual_baseline_carbon_cost * 100
            )
        execution_time_seconds = None
        baseline_execution_time_seconds = None
        energy_saving_pct = None
        if self.result:
            execution_time_seconds = self.result.get("execution_time_seconds")
            baseline_execution_time_seconds = self.result.get("baseline_execution_time_seconds")
            if execution_time_seconds is not None and baseline_execution_time_seconds:
                energy_saving_pct = (
                    (baseline_execution_time_seconds - execution_time_seconds)
                    / baseline_execution_time_seconds * 100
                )
        return {
            "request_id": self.request_id,
            "task": self.task,
            "status": self.status,
            "deadline_seconds": self.deadline_seconds,
            "submitted_at": self.submitted_at.isoformat(),
            "ack_latency_seconds": (self.ack_received_at - self.submitted_at).total_seconds(),
            "carbonshift_status": self.ack.get("status"),
            "scheduled_slot": self.ack.get("scheduled_slot"),
            "eta_seconds": self.ack.get("eta_seconds"),
            "flavour": self.ack.get("flavour"),
            "scheduled_at": _unix_to_iso(self.ack.get("scheduled_at")),
            "carbon_cost": carbon_cost,
            "baseline_carbon_cost": baseline_carbon_cost,
            "carbon_saving_pct": carbon_saving_pct,
            "actual_carbon_cost": self.actual_carbon_cost,
            "actual_baseline_carbon_cost": self.actual_baseline_carbon_cost,
            "actual_carbon_saving_pct": actual_carbon_saving_pct,
            "execution_time_seconds": execution_time_seconds,
            "baseline_execution_time_seconds": baseline_execution_time_seconds,
            "energy_saving_pct": energy_saving_pct,
            "callback_received_at": self.callback_received_at.isoformat() if self.callback_received_at else None,
            "end_to_end_seconds": end_to_end_seconds,
            "late": late,
            "success": self.success,
            "result": self.result,
            "error": self.error,
        }


class RequestTracker:
    def __init__(self, metrics_path: str, timeout_seconds: float):
        self._lock = threading.Lock()
        self._by_id: dict[str, TrackedRequest] = {}
        self.metrics_path = metrics_path
        self.timeout_seconds = timeout_seconds
        directory = os.path.dirname(metrics_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    def add(self, tracked: TrackedRequest) -> None:
        with self._lock:
            self._by_id[tracked.request_id] = tracked

    def get(self, request_id: str) -> Optional[TrackedRequest]:
        with self._lock:
            return self._by_id.get(request_id)

    def on_callback(self, request_id: str, success: bool, result: Optional[dict[str, Any]],
                     error: Optional[str], actual_carbon_cost: Optional[float] = None,
                     actual_baseline_carbon_cost: Optional[float] = None) -> bool:
        with self._lock:
            t = self._by_id.get(request_id)
            stale = t is not None and t.ack.get("status") == "pending"
        if t is None:
            return False
        if stale:
            # The initial submit-time poll timed out before the DP solver
            # committed an assignment, so `ack` never got flavour/carbon_cost.
            # A callback only ever arrives after real scheduling+dispatch, so
            # a fresh poll here is guaranteed to have them.
            try:
                fresh_ack = get_status(request_id)
                with self._lock:
                    t.ack = fresh_ack
            except CarbonshiftError:
                logger.warning("failed to refresh stale ack for request_id=%s", request_id, exc_info=True)
        with self._lock:
            t.callback_received_at = datetime.now(timezone.utc)
            t.success = success
            t.result = result
            t.error = error
            t.actual_carbon_cost = actual_carbon_cost
            t.actual_baseline_carbon_cost = actual_baseline_carbon_cost
            t.status = "completed" if success else "failed"
            record = t.to_dict()
        self._persist(record)
        return True

    def sweep_timeouts(self) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            pending = [t for t in self._by_id.values() if t.status == "submitted"]
        for t in pending:
            if (now - t.submitted_at).total_seconds() <= self.timeout_seconds:
                continue
            with self._lock:
                if t.status != "submitted":
                    continue
                t.status = "timed_out"
                t.error = f"no callback within {self.timeout_seconds}s"
                record = t.to_dict()
            self._persist(record)

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [t.to_dict() for t in self._by_id.values()]

    @staticmethod
    def _group_stats(its: list[dict[str, Any]]) -> dict[str, Any]:
        completed = [i for i in its if i["status"] == "completed"]
        e2e = [i["end_to_end_seconds"] for i in completed if i["end_to_end_seconds"] is not None]
        ack = [i["ack_latency_seconds"] for i in its]
        carbon = [i["carbon_cost"] for i in its if i["carbon_cost"] is not None]
        baseline_carbon = [i["baseline_carbon_cost"] for i in its if i["baseline_carbon_cost"] is not None]
        exec_time = [i["execution_time_seconds"] for i in completed if i["execution_time_seconds"] is not None]
        baseline_exec_time = [i["baseline_execution_time_seconds"] for i in completed
                               if i["baseline_execution_time_seconds"] is not None]
        confidences = [i["result"]["confidence"] for i in completed
                        if i.get("result") and i["result"].get("confidence") is not None]
        qualities = [i["result"]["quality_score"] for i in completed
                     if i.get("result") and i["result"].get("quality_score") is not None]
        late_count = sum(1 for i in its if i["late"])

        total_carbon_cost = sum(carbon)
        total_baseline_carbon_cost = sum(baseline_carbon)
        total_execution_time = sum(exec_time)
        total_baseline_execution_time = sum(baseline_exec_time)
        carbon_saving_value = None
        if total_baseline_carbon_cost:
            carbon_saving_value = ((total_baseline_carbon_cost - total_carbon_cost)
                                  / total_baseline_carbon_cost) * 100
        energy_saving_value = None
        if total_baseline_execution_time:
            energy_saving_value = ((total_baseline_execution_time - total_execution_time)
                                  / total_baseline_execution_time) * 100

        return {
            "count": len(its),
            "completed": len(completed),
            "failed": sum(1 for i in its if i["status"] == "failed"),
            "timed_out": sum(1 for i in its if i["status"] == "timed_out"),
            "late_count": late_count,
            "late_rate": round(late_count / len(its), 2) if its else 0.0,
            "ack_latency_seconds": _stats(ack),
            "end_to_end_seconds": _stats(e2e),
            "execution_time_seconds": _stats(exec_time),
            "baseline_execution_time_seconds": _stats(baseline_exec_time),
            "energy_saving_pct": {
                "avg": round(energy_saving_value, 2) if energy_saving_value is not None else None,
                "min": None,
                "max": None,
            },
            "carbon_cost": _stats(carbon),
            "baseline_carbon_cost": _stats(baseline_carbon),
            "carbon_saving_pct": {
                "avg": round(carbon_saving_value, 2) if carbon_saving_value is not None else None,
                "min": None,
                "max": None,
            },
            "confidence": _stats(confidences),
            "quality_score": _stats(qualities),
        }

    def summary(self) -> dict[str, Any]:
        items = self.all()
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for i in items:
            groups[(i["task"], i["flavour"] or "unknown")].append(i)

        by_task_flavour = {f"{task}/{flavour}": self._group_stats(its) for (task, flavour), its in groups.items()}

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_requests": len(items),
            # Same shape as each `by_task_flavour` entry, but aggregated
            # across every task/flavour — e.g. "what was the overall carbon
            # saving of this whole run vs. the no-carbonshift baseline?",
            # which no per-flavour breakdown answers directly.
            "overall": self._group_stats(items),
            "by_task_flavour": by_task_flavour,
        }

    def progress(self) -> dict[str, Any]:
        """Quick top-level snapshot of an in-progress (or finished) test run:
        counts by lifecycle stage plus a handful of headline averages. Meant
        to be cheap enough to poll repeatedly with `curl` while a test is
        running (unlike `summary()`, which breaks everything down per
        task/flavour).
        """
        items = self.all()
        completed = [i for i in items if i["status"] == "completed"]
        # `scheduled_slot` on a tracked request reflects only carbonshift's
        # *synchronous* ack at submit time — with a short SUBMIT_WAIT_TIMEOUT
        # (the norm for emulated/manual-clock tests) most requests ack as
        # "pending" and get scheduled only later, so `scheduled_slot` alone
        # would badly undercount. A `completed` request necessarily went
        # through scheduling+dispatch on carbonshift's side regardless of
        # what its own ack said, so count those too.
        scheduled = [i for i in items if i["scheduled_slot"] is not None or i["status"] == "completed"]
        ack = [i["ack_latency_seconds"] for i in items]
        exec_time = [i["execution_time_seconds"] for i in completed if i["execution_time_seconds"] is not None]
        carbon_cost = [i["carbon_cost"] for i in items if i["carbon_cost"] is not None]
        baseline_carbon_cost = [i["baseline_carbon_cost"] for i in items if i["baseline_carbon_cost"] is not None]
        energy_cost = [i["execution_time_seconds"] for i in completed if i["execution_time_seconds"] is not None]
        baseline_execution_cost = [i["baseline_execution_time_seconds"] for i in completed
                                   if i["baseline_execution_time_seconds"] is not None]
        confidences = [i["result"]["confidence"] for i in completed
                        if i.get("result") and i["result"].get("confidence") is not None]
        qualities = [i["result"]["quality_score"] for i in completed
                     if i.get("result") and i["result"].get("quality_score") is not None]
        total_carbon_cost = sum(carbon_cost)
        total_baseline_carbon_cost = sum(baseline_carbon_cost)
        total_execution_time = sum(energy_cost)
        total_baseline_execution_time = sum(baseline_execution_cost)

        avg_carbon_saving_pct = None
        if total_baseline_carbon_cost:
            avg_carbon_saving_pct = ((total_baseline_carbon_cost - total_carbon_cost)
                                    / total_baseline_carbon_cost) * 100
        avg_energy_saving_pct = None
        if total_baseline_execution_time:
            avg_energy_saving_pct = ((total_baseline_execution_time - total_execution_time)
                                    / total_baseline_execution_time) * 100

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "requests_sent": len(items),
            "requests_scheduled": len(scheduled),
            "requests_completed": len(completed),
            "requests_failed": sum(1 for i in items if i["status"] == "failed"),
            "requests_timed_out": sum(1 for i in items if i["status"] == "timed_out"),
            "requests_pending": sum(1 for i in items if i["status"] == "submitted"),
            "avg_quality_score": _stats(qualities)["avg"],
            "avg_confidence": _stats(confidences)["avg"],
            "avg_execution_time_seconds": _stats(exec_time)["avg"],
            "avg_ack_latency_seconds": _stats(ack)["avg"],
            "avg_carbon_saving_pct": round(avg_carbon_saving_pct, 2) if avg_carbon_saving_pct is not None else None,
            "avg_energy_saving_pct": round(avg_energy_saving_pct, 2) if avg_energy_saving_pct is not None else None,
        }

    def _persist(self, record: dict[str, Any]) -> None:
        with open(self.metrics_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
