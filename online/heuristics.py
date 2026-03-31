"""
Online Heuristics for Carbon-Aware Scheduling

Provides fast heuristic algorithms for immediate decision-making,
extending baseline greedy approaches with:
- Look-ahead using request predictions
- Capacity-aware pressure modeling
- Adaptive error budget management
"""

from typing import List, Tuple, Optional, Dict
from dataclasses import dataclass
from collections import defaultdict
import random


@dataclass
class Request:
    """Request representation"""
    id: int
    deadline: int
    arrival_time: int = 0


@dataclass
class Strategy:
    """Computation strategy (flavour)"""
    name: str
    error: int  # Error percentage
    duration: int  # Execution time in seconds


class GreedyCarbonLookahead:
    """
    Greedy carbon-aware scheduler with look-ahead.

    Extends naive_shift (from greedy.py) by considering:
    1. Future request load predictions
    2. Slot pressure (current + predicted assignments)
    3. Capacity constraints

    Decision for request r at time t:
        score(slot, strategy) = carbon[slot] * duration[strategy] / 3600 * 0.05 * (1 + α * pressure)
        where pressure = (current_load + predicted_load) / capacity

    Minimizes score subject to deadline and error constraints.
    """

    def __init__(
        self,
        strategies: List[Strategy],
        carbon: List[float],
        capacity: int = 5000,
        pressure_weight: float = 0.5,
        error_threshold: float = 5.0,
        predictor=None
    ):
        """
        Initialize greedy scheduler.

        Args:
            strategies: Available computation strategies
            carbon: Carbon intensity forecast per slot (gCO2/kWh)
            capacity: Maximum requests per slot (hard cap)
            pressure_weight: Weight α for pressure penalty (0=ignore, 1=full weight)
            error_threshold: Maximum average error tolerated (%)
            predictor: RequestPredictor instance for load forecasting
        """
        self.strategies = strategies
        self.carbon = carbon
        self.capacity = capacity
        self.pressure_weight = pressure_weight
        self.error_threshold = error_threshold
        self.predictor = predictor

        # State tracking
        self.load_per_slot = defaultdict(int)  # Actual assignments
        self.total_error = 0.0
        self.total_requests = 0

    def schedule(
        self,
        request: Request,
        current_time: int
    ) -> Tuple[int, str]:
        """
        Schedule request to (time_slot, strategy).

        Args:
            request: Request to schedule
            current_time: Current time slot

        Returns:
            (time_slot, strategy_name) assignment
        """
        # Valid slots: [current_time, deadline]
        valid_slots = range(current_time, request.deadline + 1)

        # Get predicted load for valid slots (if predictor available)
        predicted_load = {}
        if self.predictor:
            for t in valid_slots:
                if t < len(self.carbon):
                    predicted_load[t] = self.predictor.predict_load(t)
                else:
                    predicted_load[t] = 0.0
        else:
            predicted_load = {t: 0.0 for t in valid_slots}

        # Find best (slot, strategy) combination
        best_score = float('inf')
        best_assignment = None

        for slot in valid_slots:
            # Skip if slot beyond carbon forecast
            if slot >= len(self.carbon):
                continue

            # Calculate pressure for this slot
            current_load = self.load_per_slot[slot]
            pred_load = predicted_load.get(slot, 0.0)
            total_load = current_load + pred_load

            # Pressure penalty (0 to ~1, unbounded if overload)
            pressure = total_load / self.capacity if self.capacity > 0 else 0.0

            # Check capacity hard constraint
            if current_load >= self.capacity:
                continue  # Slot full, skip

            for strategy in self.strategies:
                # Check error budget constraint
                projected_avg_error = (
                    (self.total_error + strategy.error) / (self.total_requests + 1)
                )
                if projected_avg_error > self.error_threshold:
                    # This strategy would violate error budget
                    continue

                # Calculate score: carbon cost + pressure penalty
                # Emission calculation matches carbonshift.py:
                # carbon[gCO2/kWh] * duration[s] / 3600 * server_kwh[0.05kW] = gCO2
                carbon_cost = self.carbon[slot] * strategy.duration / 3600 * 0.05

                # Pressure penalty: linearly increase cost with congestion
                pressure_penalty = 1.0 + (self.pressure_weight * pressure)

                score = carbon_cost * pressure_penalty

                # Track best
                if score < best_score:
                    best_score = score
                    best_assignment = (slot, strategy.name)

        # Fallback: if no valid assignment found (e.g., all slots full)
        if best_assignment is None:
            # Emergency: assign to earliest slot with highest quality
            fallback_slot = current_time
            fallback_strategy = min(self.strategies, key=lambda s: s.error).name
            best_assignment = (fallback_slot, fallback_strategy)

        # Update state
        slot, strategy_name = best_assignment
        self.load_per_slot[slot] += 1

        # Update error tracking
        chosen_strategy = next(s for s in self.strategies if s.name == strategy_name)
        self.total_error += chosen_strategy.error
        self.total_requests += 1

        return best_assignment

    def get_current_avg_error(self) -> float:
        """Get current average error across all scheduled requests"""
        if self.total_requests == 0:
            return 0.0
        return self.total_error / self.total_requests

    def get_slot_utilization(self, slot: int) -> float:
        """Get utilization fraction for slot (0.0 to 1.0+)"""
        if self.capacity == 0:
            return 0.0
        return self.load_per_slot[slot] / self.capacity

    def reset_state(self):
        """Reset scheduler state (for new batch of requests)"""
        self.load_per_slot.clear()
        self.total_error = 0.0
        self.total_requests = 0


class ProbabilisticSlackScheduler:
    """
    Probabilistic scheduler that exploits deadline slack.

    Strategy:
    - If deadline is far (slack > threshold) AND error budget available:
        → Postpone to low-carbon slot with lower-quality strategy
    - If deadline is tight OR error budget exhausted:
        → Schedule immediately with high-quality strategy

    Key idea: "Lazy" scheduling to wait for green slots, but only when safe.
    """

    def __init__(
        self,
        strategies: List[Strategy],
        carbon: List[float],
        capacity: int = 5000,
        slack_threshold: int = 3,
        error_threshold: float = 5.0,
        predictor=None
    ):
        """
        Initialize probabilistic slack scheduler.

        Args:
            strategies: Available strategies
            carbon: Carbon intensity forecast
            capacity: Max requests per slot
            slack_threshold: Minimum slack to consider postponing
            error_threshold: Max average error
            predictor: Request predictor for finding green slots
        """
        self.strategies = strategies
        self.carbon = carbon
        self.capacity = capacity
        self.slack_threshold = slack_threshold
        self.error_threshold = error_threshold
        self.predictor = predictor

        # State
        self.load_per_slot = defaultdict(int)
        self.total_error = 0.0
        self.total_requests = 0

        # Strategy tiers (sorted by error: High=0, Medium, Low=max)
        self.strategies_by_quality = sorted(self.strategies, key=lambda s: s.error)

    def schedule(
        self,
        request: Request,
        current_time: int
    ) -> Tuple[int, str]:
        """Schedule request using probabilistic slack allocation"""

        slack = request.deadline - current_time

        # Calculate remaining error budget
        current_avg_error = (
            self.total_error / self.total_requests if self.total_requests > 0 else 0.0
        )
        error_budget_remaining = self.error_threshold - current_avg_error

        # Decision: postpone or immediate?
        if slack >= self.slack_threshold and error_budget_remaining > 2.0:
            # CASE 1: Slack available + error budget → postpone to green slot
            slot, strategy_name = self._postpone_to_green_slot(request, current_time)
        else:
            # CASE 2: Tight deadline or budget exhausted → immediate high quality
            slot, strategy_name = self._schedule_immediate(request, current_time)

        # Update state
        self.load_per_slot[slot] += 1
        chosen_strategy = next(s for s in self.strategies if s.name == strategy_name)
        self.total_error += chosen_strategy.error
        self.total_requests += 1

        return (slot, strategy_name)

    def _postpone_to_green_slot(
        self,
        request: Request,
        current_time: int
    ) -> Tuple[int, str]:
        """Find greenest slot with capacity, use low-quality strategy"""

        valid_slots = range(current_time, request.deadline + 1)

        # Find slot with minimum carbon (with capacity)
        best_slot = None
        best_carbon = float('inf')

        for slot in valid_slots:
            if slot >= len(self.carbon):
                continue

            # Check capacity
            if self.load_per_slot[slot] >= self.capacity:
                continue

            if self.carbon[slot] < best_carbon:
                best_carbon = self.carbon[slot]
                best_slot = slot

        # If no slot found, fallback to immediate
        if best_slot is None:
            return self._schedule_immediate(request, current_time)

        # Use low-quality strategy (saves time and reduces emissions)
        low_quality_strategy = self.strategies_by_quality[-1].name  # Max error

        return (best_slot, low_quality_strategy)

    def _schedule_immediate(
        self,
        request: Request,
        current_time: int
    ) -> Tuple[int, str]:
        """Schedule to current slot with high-quality strategy"""

        slot = current_time

        # Use high-quality strategy (min error)
        high_quality_strategy = self.strategies_by_quality[0].name

        return (slot, high_quality_strategy)

    def reset_state(self):
        """Reset state"""
        self.load_per_slot.clear()
        self.total_error = 0.0
        self.total_requests = 0


# Utility functions for integration with existing greedy.py

def convert_greedy_request_format(greedy_req: dict) -> Request:
    """Convert greedy.py request dict to Request dataclass"""
    return Request(
        id=greedy_req['id'],
        deadline=greedy_req['deadline'],
        arrival_time=0  # greedy.py doesn't track arrival time
    )


def convert_greedy_strategy_format(greedy_strat: dict) -> Strategy:
    """Convert greedy.py strategy dict to Strategy dataclass"""
    return Strategy(
        name=greedy_strat['strategy'],
        error=greedy_strat['error'],
        duration=greedy_strat['duration']
    )
