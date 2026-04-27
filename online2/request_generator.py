"""
Request Generator Module

Simulates incoming requests at a constant rate.
Runs in a separate thread and adds requests to shared state.
"""

import threading
import time
import random
from typing import Optional
from shared_state import Request, SharedSchedulerState
import config


class RequestGenerator:
    """
    Generates requests at a constant rate (requests_per_slot).
    Runs in background thread, adds to shared state.
    """

    def __init__(self, shared_state: SharedSchedulerState, requests_per_slot: float = config.PREDICTED_REQUESTS_PER_SLOT):
        """
        Initialize request generator.

        Args:
            shared_state: SharedSchedulerState instance
            requests_per_slot: Average requests per time slot
        """
        self.shared_state = shared_state
        self.requests_per_slot = requests_per_slot
        self.slot_duration = config.SLOT_DURATION_SECONDS

        # Request ID counter
        self._request_id = 0
        self._lock = threading.Lock()

        # Thread control
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start request generation in background thread"""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=False)
        self._thread.start()

        if config.VERBOSE:
            print(f"[RequestGenerator] Started: {self.requests_per_slot} req/slot")

    def stop(self) -> None:
        """Stop request generation"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)

        if config.VERBOSE:
            print("[RequestGenerator] Stopped")

    def _run(self) -> None:
        """Main generation loop (runs in thread)"""
        slot = 0
        slot_start_time = time.time()

        while self._running:
            now = time.time()
            elapsed = now - slot_start_time

            # Determine current slot
            current_slot = int(elapsed / self.slot_duration)

            # Generate requests for this slot
            if current_slot > slot:
                # We've entered a new slot
                slot = current_slot
                sigma = max(1.0, self.requests_per_slot * config.REQUEST_RATE_STD_FACTOR)
                num_requests = max(1, int(random.gauss(self.requests_per_slot, sigma)))

                for _ in range(num_requests):
                    req = self._generate_request(slot)
                    self.shared_state.add_request(req)

                if config.VERBOSE:
                    print(f"[RequestGenerator] Slot {slot}: Generated {num_requests} requests")

            # Small sleep to avoid busy-waiting
            time.sleep(0.01)

    def _generate_request(self, arrival_slot: int) -> Request:
        """Generate a single request"""
        with self._lock:
            request_id = self._request_id
            self._request_id += 1

        # Random deadline: configurable range (default: +0 .. +8 from arrival)
        slack = random.randint(config.DEADLINE_MIN_SLACK, config.DEADLINE_MAX_SLACK)
        deadline_slot = min(arrival_slot + slack, config.TOTAL_SLOTS - 1)

        return Request(
            id=request_id,
            arrival_slot=arrival_slot,
            deadline_slot=deadline_slot,
        )

    def get_total_generated(self) -> int:
        """Get total requests generated so far"""
        with self._lock:
            return self._request_id
