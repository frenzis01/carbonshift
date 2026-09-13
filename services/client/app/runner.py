"""Background batch sender: pulls task examples and submits them to
carbonshift one at a time, without blocking the HTTP handler that triggers
it (dataset loading + N sequential HTTP calls can take a while).
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone

from .carbonshift_client import CarbonshiftError, submit
from .config import settings
from .datasets import load_examples
from .tracker import RequestTracker, TrackedRequest

logger = logging.getLogger("client.runner")


def send_batch(tracker: RequestTracker, task: str, count: int, deadline_seconds: float,
               source: str, seed: int) -> str:
    batch_id = uuid.uuid4().hex

    def _worker() -> None:
        try:
            examples = load_examples(task, count, seed=seed, source=source)
        except Exception:
            logger.exception("batch %s: failed to build examples (task=%s, source=%s)", batch_id, task, source)
            return

        callback_url = f"{settings.self_base_url}/callback"
        for example in examples:
            submitted_at = datetime.now(timezone.utc)
            payload = {"task": task, "input": example["input"]}
            try:
                ack = submit(deadline_seconds, callback_url, payload, task_id=task)
            except CarbonshiftError:
                logger.exception("batch %s: submit failed", batch_id)
                continue

            request_id = str(ack.get("request_id"))
            tracker.add(TrackedRequest(request_id, task, deadline_seconds, submitted_at, ack))
            logger.info("batch %s: submitted request_id=%s status=%s", batch_id, request_id, ack.get("status"))

    threading.Thread(target=_worker, daemon=True, name=f"send-batch-{batch_id}").start()
    return batch_id
