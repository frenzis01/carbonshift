"""
Rolling Window DP Solver for Online2 Batch Scheduler

This module implements a Dynamic Programming solver optimized for batch scheduling
in Online2. It handles:
- Batch scheduling (N requests at a time)
- Capacity tiers with rebound effect multipliers
- Error budget windows (sliding 11-slot window)
- Beam Search and K-Best pruning strategies
"""

from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional
import time


@dataclass
class RequestAssignment:
    """Result of a single request assignment"""
    request_id: str
    strategy_name: str
    slot: int
    carbon_cost: float
    error: float


class RollingWindowDPScheduler:
    """
    DP-based batch scheduler with rolling window optimization and pruning.
    
    Solves: Assign N requests to time slots and strategies to minimize carbon cost
    while respecting:
    - Deadline constraints
    - Error budget window (t-Wpast to t+Wfuture)
    - Capacity tier multipliers (rebound effect)
    - Max average error threshold
    """
    
    def __init__(self, 
                 strategies: List[dict],
                 carbon_forecast: List[float],
                 window_size: int = 24,
                 pruning: str = 'beam',
                 pruning_k: int = 150,
                 timeout: float = 5.0):
        """
        Initialize the DP scheduler.
        
        Args:
            strategies: List of strategy dicts with 'name', 'error', 'duration'
            carbon_forecast: Carbon intensity per time slot [0..window_size-1]
            window_size: Total number of time slots (default 24)
            pruning: Pruning strategy - 'beam', 'kbest', or 'none' (default 'beam')
            pruning_k: Number of states to keep when pruning (default 150)
            timeout: Maximum execution time in seconds (default 5.0)
        """
        self.strategies = strategies
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
        initial_error_count = int(error_window_baseline.get("request_count", 0)) + legacy_error_count
        initial_mock_count = max(0, int(dynamic_mock_pool.get("initial_count", 0)))
        mock_error_bp = int(round(float(dynamic_mock_pool.get("error_per_request", 0.0)) * 100))

        # DP state fields:
        # - error_sum_bp: total error in the active window (basis points)
        # - error_count: number of requests counted in that window
        # - mock_remaining: synthetic requests still counted in baseline
        # - inc_counts / inc_durations: incremental slot load introduced by this batch
        init_state = (
            initial_error_sum_bp,
            initial_error_count,
            initial_mock_count,
            tuple([0] * T),
            tuple([0] * T),
        )
        dp_prev = {init_state: (0.0, [])}

        start_ts = time.time()

        # Expand request-by-request: each DP layer schedules exactly one request.
        for req_idx, req in enumerate(requests):
            req_id = req["id"]
            deadline = deadlines[req_idx]
            dp_curr = {}

            for state_key, (prev_cost, prev_assignments) in dp_prev.items():
                error_sum_bp, error_count, mock_remaining, inc_counts_t, inc_durations_t = state_key
                inc_counts = list(inc_counts_t)
                inc_durations = list(inc_durations_t)

                # Try every strategy and every feasible slot for the current request.
                for strategy in self.strategies:
                    strategy_error = float(strategy["error"])
                    strategy_error_bp = int(round(strategy_error * 100))
                    strategy_duration = int(strategy["duration"])

                    for slot in range(current_slot, deadline + 1):
                        # Incremental cost is computed with dynamic repricing:
                        # if the new assignment changes capacity tier, the whole slot
                        # cost is repriced, not only the marginal request.
                        delta_cost = self._incremental_carbon_cost(
                            slot=slot,
                            add_duration=strategy_duration,
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
                            new_error_sum_bp += strategy_error_bp
                            new_error_count += 1

                            # Optional synthetic baseline decay:
                            # each new in-window assignment can "replace" one mock
                            # request injected by the recovery policy.
                            if new_mock_remaining > 0 and mock_error_bp > 0:
                                new_error_sum_bp -= mock_error_bp
                                new_error_count = max(0, new_error_count - 1)
                                new_mock_remaining -= 1

                        new_inc_counts = inc_counts.copy()
                        new_inc_durations = inc_durations.copy()
                        new_inc_counts[slot] += 1
                        new_inc_durations[slot] += strategy_duration

                        assignment = RequestAssignment(
                            request_id=req_id,
                            strategy_name=strategy["name"],
                            slot=slot,
                            carbon_cost=delta_cost,
                            error=strategy_error,
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
        Assigns each request to the earliest available slot with the fastest strategy.
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
            for strategy in self.strategies:
                duration = int(strategy["duration"])
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
                        best_choice = (delta_cost, slot, strategy)

            if best_choice is None:
                continue

            carbon_cost, best_slot, strategy = best_choice
            inc_counts[best_slot] += 1
            inc_durations[best_slot] += int(strategy["duration"])

            assignment = RequestAssignment(
                request_id=req_id,
                strategy_name=strategy["name"],
                slot=best_slot,
                carbon_cost=carbon_cost,
                error=float(strategy["error"]),
            )
            assignments.append(assignment)

        return assignments

    def _get_capacity_multiplier(self, capacity_tiers: List[dict], request_count: int) -> float:
        for tier in capacity_tiers:
            if request_count <= tier["max_requests"]:
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
        before_count = base_counts[slot] + inc_counts[slot]
        after_count = before_count + 1

        before_duration = base_durations[slot] + inc_durations[slot]
        after_duration = before_duration + add_duration

        before_mult = self._get_capacity_multiplier(capacity_tiers, before_count)
        after_mult = self._get_capacity_multiplier(capacity_tiers, after_count)

        slot_carbon = self.carbon_forecast[slot]
        before_cost = slot_carbon * before_mult * before_duration
        after_cost = slot_carbon * after_mult * after_duration
        return after_cost - before_cost

    def solve_with_error_window(
        self,
        requests: List[dict],
        current_slot: int,
        capacity_multiplier: float = 1.0,
        max_error_threshold: float = 3.0,
        error_window_data: Dict[int, float] = None,
    ) -> Tuple[List[RequestAssignment], float]:
        """
        Solve batch problem while respecting error budget window constraint.
        
        Error window: average error in slots [current_slot-5, ..., current_slot+5]
        must be ≤ max_error_threshold
        
        Args:
            requests: List of requests
            current_slot: Current time slot
            capacity_multiplier: Capacity tier multiplier
            max_error_threshold: Maximum average error allowed (as percentage)
            error_window_data: Dict of {slot: current_error} in the window
            
        Returns:
            (assignments, average_error_in_window)
        """
        assignments = self.solve_batch(
            requests=requests,
            current_slot=current_slot,
            capacity_multiplier=capacity_multiplier,
            error_window_errors=error_window_data,
            max_error_threshold=max_error_threshold,
        )

        window_start = max(0, current_slot - 5)
        window_end = min(self.window_size - 1, current_slot + 5)

        # Best-effort weighted average reconstruction.
        error_sum = 0.0
        request_count = 0
        if error_window_data:
            for slot, avg_err in error_window_data.items():
                if window_start <= slot <= window_end:
                    error_sum += avg_err
                    request_count += 1

        for assignment in assignments:
            if window_start <= assignment.slot <= window_end:
                error_sum += assignment.error
                request_count += 1

        avg_error = (error_sum / request_count) if request_count else 0.0
        return assignments, avg_error
