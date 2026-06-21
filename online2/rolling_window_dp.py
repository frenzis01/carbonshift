"""
Rolling Window DP Solver for Online2 Batch Scheduler

This module implements a Dynamic Programming solver optimized for batch scheduling
in Online2. It handles:
- Batch scheduling (N requests at a time)
- Capacity tiers with rebound effect multipliers
- Error budget windows (sliding 11-slot window)
- Beam Search and K-Best pruning methods
"""

from dataclasses import dataclass
from typing import List, Dict, Optional
import time
import config


@dataclass
class RequestAssignment:
    """Result of a single request assignment"""
    request_id: str
    flavour_name: str
    slot: int
    carbon_cost: float
    error: float


class RollingWindowDPScheduler:
    """
    DP-based batch scheduler with rolling window optimization and pruning.
    
    Solves: Assign N requests to time slots and flavours to minimize carbon cost
    while respecting:
    - Deadline constraints
    - Error budget window (t-Wpast to t+Wfuture)
    - Capacity tier multipliers (rebound effect)
    - Max average error threshold
    """
    
    def __init__(self, 
                 flavours: List[dict],
                 carbon_forecast: List[float],
                 window_size: int = 24,
                 pruning: str = 'beam',
                 pruning_k: int = 150,
                 timeout: float = 5.0):
        """
        Initialize the DP scheduler.
        
        Args:
            flavours: List of flavour dicts with 'name', 'error', 'duration'
            carbon_forecast: Carbon intensity per time slot [0..window_size-1]
            window_size: Total number of time slots (default 24)
            pruning: Pruning method - 'beam', 'kbest', or 'none' (default 'beam')
            pruning_k: Number of states to keep when pruning (default 150)
            timeout: Maximum execution time in seconds (default 5.0)
        """
        self.flavours = flavours
        self.carbon_forecast = carbon_forecast
        self.window_size = window_size
        self.pruning = pruning
        self.pruning_k = pruning_k
        self.timeout = timeout
        
        # Validate inputs
        if len(carbon_forecast) != window_size:
            raise ValueError(f"Carbon forecast length {len(carbon_forecast)} != window_size {window_size}")
    
    def solve_batch(
        self,
        requests: List[dict],
        current_slot: int,
        capacity_multiplier: float = 1.0,
        error_window_errors: Dict[int, float] = None,
        capacity_tiers: Optional[List[dict]] = None,
        baseline_slot_counts: Optional[Dict[int, int]] = None,
        baseline_slot_durations: Optional[Dict[int, int]] = None,
        error_window_baseline: Optional[Dict[str, float]] = None,
        max_error_threshold: Optional[float] = None,
        error_window_past: int = 5,
        error_window_future: int = 5,
        assignment_max_slot: Optional[int] = None,
        dynamic_mock_pool: Optional[Dict[str, float]] = None,
    ) -> List[RequestAssignment]:
        """
        Solve batch scheduling with DP.

        Key properties:
        - Requests cannot be scheduled before current_slot.
        - Capacity multipliers are dynamic per slot and depend on runtime load.
        - Slot carbon cost is repriced globally when tier changes.
        - Error budget uses weighted average on requests:
          total_error_in_window / total_requests_in_window.
        """
        if not requests:
            return []

        if current_slot >= self.window_size:
            return []

        if capacity_tiers is None:
            capacity_tiers = [{"max_requests": float("inf"), "multiplier": capacity_multiplier}]
        if baseline_slot_counts is None:
            baseline_slot_counts = {}
        if baseline_slot_durations is None:
            baseline_slot_durations = {}
        if error_window_baseline is None:
            error_window_baseline = {"error_sum": 0.0, "request_count": 0}
        if error_window_errors is None:
            error_window_errors = {}
        if dynamic_mock_pool is None:
            dynamic_mock_pool = {"initial_count": 0, "error_per_request": 0.0}

        T = self.window_size
        window_start = max(0, current_slot - error_window_past)
        window_end = min(T - 1, current_slot + error_window_future)
        if assignment_max_slot is None:
            assignment_max_slot = T - 1
        assignment_cap = max(current_slot, min(int(assignment_max_slot), T - 1))

        # Clamp every request deadline to:
        # 1) current slot (cannot schedule in the past)
        # 2) global horizon [0, T-1]
        # 3) assignment cap (right edge of allowed placement horizon)
        deadlines = [
            max(current_slot, min(req.get("deadline_slot", T - 1), T - 1, assignment_cap))
            for req in requests
        ]

        base_counts = [0] * T
        base_durations = [0] * T
        for slot, count in baseline_slot_counts.items():
            if 0 <= slot < T:
                base_counts[slot] = int(count)
        for slot, duration_sum in baseline_slot_durations.items():
            if 0 <= slot < T:
                base_durations[slot] = int(duration_sum)

        # Backward-compatible fallback: if legacy per-slot errors are provided,
        # treat each slot average as one sample (best-effort only).
        legacy_error_sum = 0.0
        legacy_error_count = 0
        for slot, avg_err in error_window_errors.items():
            if window_start <= slot <= window_end:
                legacy_error_sum += float(avg_err)
                legacy_error_count += 1

        initial_error_sum_bp = int(round((error_window_baseline.get("error_sum", 0.0) + legacy_error_sum) * 100))
        initial_error_count = float(error_window_baseline.get("request_count", 0.0)) + float(legacy_error_count)
        initial_mock_count = max(0, int(dynamic_mock_pool.get("initial_count", 0)))
        mock_error_bp = int(round(float(dynamic_mock_pool.get("error_per_request", 0.0)) * 100))

        # ── DP state representation ──────────────────────────────────────────
        # Each state is a 5-tuple:
        #   (error_sum_bp, error_count, mock_remaining, inc_counts_t, inc_durations_t)
        # • error_sum_bp     – total error in window (multiplied by 100 for int arith.)
        # • error_count      – number of requests counted in that sum
        # • mock_remaining   – synthetic baseline requests not yet "consumed"
        # • inc_counts_t     – per-slot request count delta introduced by this batch
        # • inc_durations_t  – per-slot total duration delta introduced by this batch
        init_state = (
            initial_error_sum_bp,
            initial_error_count,
            initial_mock_count,
            tuple([0] * T),
            tuple([0] * T),
        )
        dp_prev = {init_state: (0.0, [])}

        start_ts = time.time()

        # ── DP expansion loop ────────────────────────────────────────────────
        # One layer per request; each layer expands every live state by trying
        # every feasible (flavour, slot) pair.  States with the same key are
        # merged by keeping the minimum-cost path (optimal substructure).

        # Expand request-by-request: each DP layer schedules exactly one request.
        for req_idx, req in enumerate(requests):
            req_id = req["id"]
            deadline = deadlines[req_idx]
            dp_curr = {}

            for state_key, (prev_cost, prev_assignments) in dp_prev.items():
                error_sum_bp, error_count, mock_remaining, inc_counts_t, inc_durations_t = state_key
                inc_counts = list(inc_counts_t)
                inc_durations = list(inc_durations_t)

                # Try every feasible flavour and slot for the current request.
                for flavour in self.flavours:
                    flavour_error = float(flavour["error"])
                    flavour_error_bp = int(round(flavour_error * 100))
                    flavour_duration = int(flavour["duration"])

                    for slot in range(current_slot, deadline + 1):
                        # Incremental cost is computed with dynamic repricing:
                        # if the new assignment changes capacity tier, the whole slot
                        # cost is repriced, not only the marginal request.
                        delta_cost = self._incremental_carbon_cost(
                            slot=slot,
                            add_duration=flavour_duration,
                            base_counts=base_counts,
                            base_durations=base_durations,
                            inc_counts=inc_counts,
                            inc_durations=inc_durations,
                            capacity_tiers=capacity_tiers,
                        )

                        new_error_sum_bp = error_sum_bp
                        new_error_count = error_count
                        new_mock_remaining = mock_remaining
                        if window_start <= slot <= window_end:
                            new_error_sum_bp += flavour_error_bp
                            new_error_count += 1.0

                            # Optional synthetic baseline decay:
                            # each new in-window assignment can "replace" one mock
                            # request injected by the recovery policy.
                            if new_mock_remaining > 0 and mock_error_bp > 0:
                                new_error_sum_bp -= mock_error_bp
                                new_error_count = max(0.0, new_error_count - 1.0)
                                new_mock_remaining -= 1

                        new_inc_counts = inc_counts.copy()
                        new_inc_durations = inc_durations.copy()
                        new_inc_counts[slot] += 1
                        new_inc_durations[slot] += flavour_duration

                        assignment = RequestAssignment(
                            request_id=req_id,
                            flavour_name=flavour["name"],
                            slot=slot,
                            carbon_cost=delta_cost,
                            error=flavour_error,
                        )
                        new_assignments = prev_assignments + [assignment]
                        new_cost = prev_cost + delta_cost
                        new_state = (
                            new_error_sum_bp,
                            new_error_count,
                            new_mock_remaining,
                            tuple(new_inc_counts),
                            tuple(new_inc_durations),
                        )

                        if new_state not in dp_curr or new_cost < dp_curr[new_state][0]:
                            dp_curr[new_state] = (new_cost, new_assignments)

            if not dp_curr:
                # No feasible expansion for this request layer.
                return []

            # ── Pruning ──────────────────────────────────────────────────────
            # After each layer, the state space is pruned to at most pruning_k
            # states.  Beam keeps the cheapest states; kbest also considers avg
            # error (helps diversity when many states have identical cost).
            if self.pruning in {"beam", "kbest"} and len(dp_curr) > self.pruning_k:
                if self.pruning == "beam":
                    sorted_states = sorted(dp_curr.items(), key=lambda x: x[1][0])
                else:
                    # kbest: prioritize low cost and low average error
                    sorted_states = sorted(
                        dp_curr.items(),
                        key=lambda x: (
                            x[1][0],
                            (x[0][0] / max(1, x[0][1])),  # avg error in basis points
                        ),
                    )
                dp_curr = dict(sorted_states[: self.pruning_k])

            if time.time() - start_ts > self.timeout:
                # Timeout fallback keeps the system responsive under high complexity.
                return self._greedy_fallback(
                    requests=requests[req_idx:],
                    deadlines=deadlines[req_idx:],
                    current_slot=current_slot,
                    capacity_tiers=capacity_tiers,
                    base_counts=base_counts,
                    base_durations=base_durations,
                )

            dp_prev = dp_curr

        # ── Feasibility filter ───────────────────────────────────────────────
        # Drop final states that violate the error-window constraint.  This is
        # evaluated only once at the end (not layer-by-layer) to avoid over-pruning
        # paths whose error improves in later layers.
        if max_error_threshold is not None:
            # Enforce strict window constraint on complete assignments only.
            feasible_states = {
                state: payload
                for state, payload in dp_prev.items()
                if state[1] == 0 or ((state[0] / 100.0) / state[1]) <= max_error_threshold
            }
            if not feasible_states:
                return []
            dp_prev = feasible_states

        _, (__, best_assignments) = min(dp_prev.items(), key=lambda x: x[1][0])
        return best_assignments
    
    def _greedy_fallback(
        self,
        requests: List[dict],
        deadlines: List[int],
        current_slot: int,
        capacity_tiers: Optional[List[dict]] = None,
        base_counts: Optional[List[int]] = None,
        base_durations: Optional[List[int]] = None,
    ) -> List[RequestAssignment]:
        """
        Fallback greedy scheduler when DP fails.
        Assigns each request to the earliest available slot with the most accurate (slowest) flavour.
        """
        assignments = []
        if capacity_tiers is None:
            capacity_tiers = [{"max_requests": float("inf"), "multiplier": 1.0}]
        if base_counts is None:
            base_counts = [0] * self.window_size
        if base_durations is None:
            base_durations = [0] * self.window_size

        inc_counts = [0] * self.window_size
        inc_durations = [0] * self.window_size

        for req_idx, req in enumerate(requests):
            req_id = req["id"]
            deadline = deadlines[req_idx]

            best_choice = None

            # select most accurate (slowest) flavour for fallback
            flavour = max(self.flavours, key=lambda s: int(s["duration"]))
            duration = int(flavour["duration"])
            for slot in range(current_slot, deadline + 1):
                delta_cost = self._incremental_carbon_cost(
                    slot=slot,
                    add_duration=duration,
                    base_counts=base_counts,
                    base_durations=base_durations,
                    inc_counts=inc_counts,
                    inc_durations=inc_durations,
                    capacity_tiers=capacity_tiers,
                )
                if best_choice is None or delta_cost < best_choice[0]:
                    best_choice = (delta_cost, slot, flavour)


            if best_choice is None:
                continue

            carbon_cost, best_slot, flavour = best_choice
            inc_counts[best_slot] += 1
            inc_durations[best_slot] += int(flavour["duration"])

            assignment = RequestAssignment(
                request_id=req_id,
                flavour_name=flavour["name"],
                slot=best_slot,
                carbon_cost=carbon_cost,
                error=float(flavour["error"]),
            )
            assignments.append(assignment)

        return assignments

    def _get_capacity_multiplier(self, capacity_tiers: List[dict], request_count: int) -> float:
        for tier in capacity_tiers:
            max_req = tier["max_requests"]
            if max_req is None or request_count <= max_req:
                return float(tier["multiplier"])
        return float(capacity_tiers[-1]["multiplier"])

    def _incremental_carbon_cost(
        self,
        slot: int,
        add_duration: int,
        base_counts: List[int],
        base_durations: List[int],
        inc_counts: List[int],
        inc_durations: List[int],
        capacity_tiers: List[dict],
    ) -> float:
        """
        Compute the marginal carbon cost of placing one request in a slot.

        Under the per-request tier model, each request is charged based on its
        1-indexed position within the slot.  The request at position K pays:
            carbon[slot] × mult(K) × duration × scale

        where mult(K) is the multiplier of the capacity tier that K falls into.
        Earlier requests are never repriced when a new request crosses a tier
        boundary — this is a purely additive model.
        """
        position = base_counts[slot] + inc_counts[slot] + 1  # 1-indexed
        mult = self._get_capacity_multiplier(capacity_tiers, position)
        slot_carbon = self.carbon_forecast[slot]
        scale = getattr(config, "CARBON_COST_DURATION_SCALE", 1.0)
        return slot_carbon * mult * add_duration * scale
