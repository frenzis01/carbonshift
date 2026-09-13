"""In-memory + append-only JSONL metrics collection and JSON reporting."""
from __future__ import annotations

import json
import os
import threading
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from .config import settings


def _stats(values: list[float]) -> dict[str, Optional[float]]:
    if not values:
        return {"avg": None, "min": None, "max": None}
    return {"avg": sum(values) / len(values), "min": min(values), "max": max(values)}


class MetricsStore:
    """Thread-safe: `record()` is called from the single worker thread but
    the HTTP handlers (running on the async event loop) read concurrently."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._records: list[dict[str, Any]] = []
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    def record(self, entry: dict[str, Any]) -> None:
        entry = {**entry, "recorded_at": datetime.now(timezone.utc).isoformat()}
        with self._lock:
            self._records.append(entry)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

    def raw(self, limit: Optional[int] = None) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._records)
        return records[-limit:] if limit else records

    def summary(self) -> dict[str, Any]:
        with self._lock:
            records = list(self._records)

        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for r in records:
            groups[(r["task"], r["flavour"])].append(r)

        by_task_flavour = {}
        for (task, flavour), items in groups.items():
            times = [i["execution_time_seconds"] for i in items if i.get("success")]
            confidences = [i["confidence"] for i in items if i.get("confidence") is not None]
            qualities = [i["quality_score"] for i in items if i.get("quality_score") is not None]
            success_count = sum(1 for i in items if i.get("success"))
            by_task_flavour[f"{task}/{flavour}"] = {
                "count": len(items),
                "success_count": success_count,
                "success_rate": success_count / len(items) if items else 0.0,
                "execution_time_seconds": _stats(times),
                "confidence": _stats(confidences),
                "quality_score": _stats(qualities),
            }

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_jobs": len(records),
            "by_task_flavour": by_task_flavour,
        }


metrics_store = MetricsStore(settings.metrics_path)
