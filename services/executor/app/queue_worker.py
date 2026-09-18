"""Single-consumer, per-timeslot FIFO job queue.

No parallelism: exactly one job runs at a time, earliest-due timeslot
first (ties broken by arrival order within that timeslot's own queue) —
this is the "one queue per timeslot" behaviour requested for the executor.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from .clock import VirtualClock
from .config import settings
from .inference import run_task
from .metrics import metrics_store

logger = logging.getLogger("executor.queue")


class Job:
    def __init__(
        self,
        request_id: str,
        task: str,
        flavour: str,
        task_input: dict[str, Any],
        execute_at: datetime,
        callback_url: Optional[str],
        context: Optional[dict[str, Any]] = None,
        queued_at: Optional[datetime] = None,
    ):
        self.request_id = request_id
        self.task = task
        self.flavour = flavour
        self.input = task_input
        self.execute_at = execute_at
        self.callback_url = callback_url
        self.context = context or {}
        self.queued_at = queued_at or datetime.now(timezone.utc)
        self.started_at: Optional[datetime] = None
        self.finished_at: Optional[datetime] = None
        self.status = "queued"
        self.result: Optional[dict[str, Any]] = None
        self.error: Optional[str] = None

    def to_status_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "status": self.status,
            "task": self.task,
            "flavour": self.flavour,
            "execute_at": self.execute_at.isoformat(),
            "queued_at": self.queued_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "result": self.result,
            "error": self.error,
            "context": self.context,
        }


class JobQueue:
    """Groups pending jobs by timeslot (`execute_at` truncated to the
    second) and serves the earliest due slot's jobs first, one at a time,
    from a dedicated background thread."""

    def __init__(self, clock: VirtualClock):
        self._clock = clock
        self._lock = threading.Lock()
        self._slots: dict[datetime, list[Job]] = {}
        self._jobs_by_id: dict[str, Job] = {}
        self._wakeup = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # True while a job has been dequeued but hasn't finished executing
        # yet (which can take a while: model download + inference) — a job
        # in this state is invisible to `_slots`, so `advance_slot()` must
        # check this separately or it can return before the job is done.
        self._busy = False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="executor-worker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wakeup.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def submit(
        self,
        task: str,
        flavour: str,
        task_input: dict[str, Any],
        execute_at: Optional[datetime],
        callback_url: Optional[str],
        request_id: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> Job:
        execute_at = execute_at or self._clock.now()
        request_id = request_id or uuid.uuid4().hex
        job = Job(request_id, task, flavour, task_input, execute_at, callback_url, context,
                  queued_at=self._clock.now())

        slot_key = execute_at.replace(microsecond=0)
        with self._lock:
            self._slots.setdefault(slot_key, []).append(job)
            self._jobs_by_id[request_id] = job
        self._wakeup.set()
        return job

    def get(self, request_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs_by_id.get(request_id)

    def queue_position(self, job: Job) -> int:
        with self._lock:
            position = 0
            for slot_time in sorted(self._slots):
                for j in self._slots[slot_time]:
                    if j is job:
                        return position
                    position += 1
            return position

    def snapshot(self) -> dict[str, list[str]]:
        with self._lock:
            return {
                slot_time.isoformat(): [j.request_id for j in jobs]
                for slot_time, jobs in sorted(self._slots.items())
            }

    def _pop_next_due(self) -> Optional[Job]:
        now = self._clock.now()
        with self._lock:
            if not self._slots:
                return None
            earliest_slot = min(self._slots)
            if earliest_slot > now:
                return None
            jobs = self._slots[earliest_slot]
            job = jobs.pop(0)
            if not jobs:
                del self._slots[earliest_slot]
            self._busy = True
            return job

    def _run(self) -> None:
        while not self._stop.is_set():
            job = self._pop_next_due()
            if job is None:
                self._wakeup.wait(timeout=settings.poll_interval_seconds)
                self._wakeup.clear()
                continue
            try:
                self._execute(job)
            finally:
                with self._lock:
                    self._busy = False

    def _execute(self, job: Job) -> None:
        job.status = "running"
        job.started_at = self._clock.now()
        success = True
        error: Optional[str] = None
        outcome: dict[str, Any]
        try:
            outcome = run_task(job.task, job.flavour, job.input)
        except Exception as exc:  # report any inference failure to the caller, don't crash the worker
            success = False
            error = str(exc)
            outcome = {"output": None, "model": None, "confidence": None, "quality_score": None,
                       "execution_time_seconds": 0.0, "baseline_execution_time_seconds": None,
                       "baseline_model": None}
            logger.exception("job %s failed", job.request_id)

        job.finished_at = self._clock.now()
        job.status = "completed" if success else "failed"
        job.result = outcome["output"]
        job.error = error

        metrics_store.record({
            "request_id": job.request_id,
            "task": job.task,
            "flavour": job.flavour,
            "model": outcome["model"],
            "success": success,
            "error": error,
            "execution_time_seconds": outcome["execution_time_seconds"],
            "confidence": outcome["confidence"],
            "quality_score": outcome["quality_score"],
            "actual_error_pct": outcome.get("actual_error_pct"),
            "baseline_execution_time_seconds": outcome.get("baseline_execution_time_seconds"),
            "baseline_model": outcome.get("baseline_model"),
            "queued_at": job.queued_at.isoformat(),
            "execute_at": job.execute_at.isoformat(),
            "started_at": job.started_at.isoformat(),
            "finished_at": job.finished_at.isoformat(),
            "context": job.context,
        })

        self._send_callback(job, success, outcome, error)

    def _send_callback(self, job: Job, success: bool, outcome: dict[str, Any], error: Optional[str]) -> None:
        if not job.callback_url:
            return
        payload = {
            "success": success,
            "result": {
                "task": job.task,
                "flavour": job.flavour,
                "model": outcome["model"],
                "output": outcome["output"],
                "confidence": outcome["confidence"],
                "quality_score": outcome["quality_score"],
                "actual_error_pct": outcome.get("actual_error_pct"),
                "execution_time_seconds": outcome["execution_time_seconds"],
                "baseline_execution_time_seconds": outcome.get("baseline_execution_time_seconds"),
                "baseline_model": outcome.get("baseline_model"),
            },
            "error": error,
        }
        try:
            resp = requests.post(job.callback_url, json=payload, timeout=settings.callback_timeout_seconds)
            logger.info("callback delivered request_id=%s status=%s payload=%s", job.request_id, resp.status_code, payload)
        except requests.RequestException as exc:
            logger.warning("callback failed request_id=%s error=%s", job.request_id, exc)

    def advance_slot(self) -> dict[str, Any]:
        """Test-only: bump the virtual clock to the next slot boundary, then
        block until every job that's now due has actually finished running
        (not just been dequeued — a job can be mid-execution, e.g. still
        downloading its model) — mirrors carbonshift's
        `POST /v1/admin/advance-slot`.
        """
        new_now = self._clock.advance_to_next_slot()
        self._wakeup.set()

        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            with self._lock:
                still_due = self._busy or any(slot_time <= new_now for slot_time in self._slots)
            if not still_due:
                break
            time.sleep(0.01)

        return {"current_time": new_now.isoformat(), "queue": self.snapshot()}


job_queue = JobQueue(VirtualClock(manual=settings.manual_clock, slot_minutes=settings.slot_minutes))
