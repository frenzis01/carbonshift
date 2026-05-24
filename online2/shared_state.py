"""
Thread-safe shared state for Online2 Batch Scheduler

Manages:
- Pending requests queue
- Current scheduling decisions
- Historical assignments
- Error budget tracking
"""

import threading
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
from collections import defaultdict
import time


@dataclass
class Request:
    """Represents a scheduling request"""
    id: int
    arrival_slot: int
    deadline_slot: int
    arrival_time: float = field(default_factory=time.time)


@dataclass
class Assignment:
    """Represents a scheduling decision"""
    request_id: int
    scheduled_slot: int
    flavour_name: str
    carbon_cost: float
    error: float
    flavour_duration: int = 0
    arrival_slot: Optional[int] = None
    deadline_slot: Optional[int] = None
    assignment_time: float = field(default_factory=time.time)


class SharedSchedulerState:
    """
    Thread-safe container for shared state between:
    - Request generator (writes new requests)
    - Scheduler (reads requests, writes assignments)
    - Monitoring (reads current state)
    """

    def __init__(self):
        """Initialize thread-safe shared state"""
        self._lock = threading.RLock()

        # Request queue (pending scheduling)
        self._pending_requests: List[Request] = []

        # Assignments (scheduled requests)
        self._assignments: Dict[int, Assignment] = {}  # {request_id -> Assignment}

        # Historical assignments from previous slots
        self._historical_assignments: Dict[int, List[Assignment]] = defaultdict(list)

        # Current slot
        self._current_slot = 0

        # Statistics
        self._total_requests_received = 0
        self._total_requests_scheduled = 0

        # Global error tracking: running totals across all ever-assigned requests.
        self._global_error_sum = 0.0
        self._global_assignment_count = 0

    def add_request(self, request: Request) -> None:
        """Add a new request to pending queue (thread-safe)"""
        with self._lock:
            self._pending_requests.append(request)
            self._total_requests_received += 1

    def get_pending_requests(self, batch_size: int) -> List[Request]:
        """
        Get up to batch_size pending requests without removing them yet.
        This allows scheduler to plan before committing.
        (thread-safe)
        """
        with self._lock:
            return self._pending_requests[:batch_size]

    def claim_pending_requests(self, count: int) -> List[Request]:
        """
        Atomically claim (remove) up to count pending requests.
        Intended for worker-based schedulers to avoid duplicate batch claims.
        (thread-safe)
        """
        with self._lock:
            claimed = self._pending_requests[:count]
            self._pending_requests = self._pending_requests[count:]
            return claimed

    def pop_pending_requests(self, count: int) -> List[Request]:
        """
        Remove and return up to count pending requests.
        Call this after successfully scheduling.
        (thread-safe)
        """
        return self.claim_pending_requests(count)

    def requeue_pending_requests_front(self, requests: List[Request]) -> None:
        """
        Put requests back at the head of pending queue preserving input order.
        Used when a claimed batch cannot be scheduled and must be retried.
        (thread-safe)
        """
        if not requests:
            return
        with self._lock:
            self._pending_requests = list(requests) + self._pending_requests

    def add_assignments(self, assignments: List[Assignment]) -> None:
        """
        Add scheduling decisions for a batch.
        (thread-safe)
        """
        with self._lock:
            for assignment in assignments:
                is_new_request = assignment.request_id not in self._assignments
                self._assignments[assignment.request_id] = assignment
                if is_new_request:
                    self._total_requests_scheduled += 1
                    self._global_error_sum += float(assignment.error)
                    self._global_assignment_count += 1

            # Move old assignments to history
            self._archive_old_assignments()

    def get_current_assignments(self) -> Dict[int, Assignment]:
        """
        Get snapshot of current active assignments.
        (thread-safe)
        """
        with self._lock:
            return dict(self._assignments)

    def get_historical_assignments_by_slot(self, slot: int) -> List[Assignment]:
        """
        Get all assignments from a historical slot.
        (thread-safe)
        """
        with self._lock:
            return list(self._historical_assignments.get(slot, []))

    def get_average_error_in_window(self, center_slot: int, window_past: int, window_future: int) -> Optional[float]:
        """
        Calculate average error in a sliding window [center_slot - window_past, center_slot + window_future].
        Used for error budget validation.
        (thread-safe)
        """
        stats = self.get_window_error_stats(center_slot, window_past, window_future)
        return stats["average_error"] if stats["request_count"] > 0 else None

    def get_window_error_stats(
        self,
        center_slot: int,
        window_past: int,
        window_future: int,
        exclude_request_ids: Optional[set] = None,
    ) -> Dict[str, float]:
        """
        Return weighted error stats in a window:
        - error_sum: sum of per-request errors
        - request_count: number of requests in the window
        - average_error: error_sum / request_count (0 if empty)
        """
        with self._lock:
            excluded = exclude_request_ids or set()
            error_sum = 0.0
            request_count = 0
            window_start = center_slot - window_past
            window_end = center_slot + window_future

            for assignment in self._assignments.values():
                if assignment.request_id in excluded:
                    continue
                if window_start <= assignment.scheduled_slot <= window_end:
                    error_sum += assignment.error
                    request_count += 1

            average_error = (error_sum / request_count) if request_count > 0 else 0.0
            return {
                "error_sum": error_sum,
                "request_count": request_count,
                "average_error": average_error,
            }

    def get_requests_in_slot(self, slot: int) -> List[Assignment]:
        """
        Get all requests scheduled for a specific slot.
        (thread-safe)
        """
        with self._lock:
            return [a for a in self._assignments.values() if a.scheduled_slot == slot]

    def get_future_assignments(self, current_slot: int) -> List[Assignment]:
        """
        Get assignments scheduled at or after current_slot.
        """
        with self._lock:
            return [a for a in self._assignments.values() if a.scheduled_slot >= current_slot]

    def set_current_slot(self, slot: int) -> None:
        """Update current slot counter (thread-safe)"""
        with self._lock:
            self._current_slot = slot

    def get_current_slot(self) -> int:
        """Get current slot (thread-safe)"""
        with self._lock:
            return self._current_slot

    def get_pending_count(self) -> int:
        """Get number of pending requests (thread-safe)"""
        with self._lock:
            return len(self._pending_requests)

    def get_global_error_stats(self) -> Dict[str, float]:
        """
        Return cumulative error stats across all ever-assigned requests.
        Running totals updated in add_assignments(); not affected by archiving.
        (thread-safe)
        """
        with self._lock:
            count = self._global_assignment_count
            error_sum = self._global_error_sum
            avg = (error_sum / count) if count > 0 else 0.0
            return {"error_sum": error_sum, "count": count, "avg": avg}

    def get_statistics(self) -> Dict:
        """Get current statistics (thread-safe)"""
        with self._lock:
            return {
                "total_received": self._total_requests_received,
                "total_scheduled": self._total_requests_scheduled,
                "pending": len(self._pending_requests),
                "current_slot": self._current_slot,
            }

    def _archive_old_assignments(self) -> None:
        """
        Move old assignments (more than MAX_HISTORY slots old) to historical storage.
        Called internally with lock held.
        """
        current_slot = self._current_slot
        max_age = 20  # Keep assignments from last 20 slots

        slots_to_archive = [
            s for s in self._historical_assignments.keys()
            if current_slot - s > max_age
        ]

        for slot in slots_to_archive:
            del self._historical_assignments[slot]

    def export_to_csv(self, filename: str) -> None:
        """
        Export all assignments to CSV for analysis.
        (thread-safe)
        """
        with self._lock:
            import csv

            with open(filename, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['request_id', 'scheduled_slot', 'flavour', 'carbon_cost', 'error', 'assignment_time'])

                for assignment in self._assignments.values():
                    writer.writerow([
                        assignment.request_id,
                        assignment.scheduled_slot,
                        assignment.flavour_name,
                        assignment.carbon_cost,
                        assignment.error,
                        assignment.assignment_time,
                    ])
