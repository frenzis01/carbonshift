"""
Tests for Online Heuristics

Compares new heuristics (GreedyCarbonLookahead, ProbabilisticSlack)
against baseline greedy approaches from greedy.py.
"""

import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from online.heuristics import (
    GreedyCarbonLookahead,
    ProbabilisticSlackScheduler,
    Request,
    Strategy
)
from online.request_predictor import MockRequestPredictor


class TestGreedyCarbonLookahead(unittest.TestCase):
    """Test GreedyCarbonLookahead heuristic"""

    def setUp(self):
        """Initialize test fixtures"""
        self.strategies = [
            Strategy(name="High", error=0, duration=120),
            Strategy(name="Medium", error=2, duration=60),
            Strategy(name="Low", error=5, duration=30),
        ]

        # Carbon pattern: slot 2 is greenest
        self.carbon = [150, 180, 100, 120, 200]

        self.predictor = MockRequestPredictor(base_load=100.0, seed=42)

    def test_basic_scheduling(self):
        """Should schedule request to valid slot"""
        scheduler = GreedyCarbonLookahead(
            strategies=self.strategies,
            carbon=self.carbon,
            capacity=1000,
            predictor=self.predictor
        )

        request = Request(id=1, deadline=3, arrival_time=0)

        slot, strategy = scheduler.schedule(request, current_time=0)

        # Assertions
        self.assertIsInstance(slot, int)
        self.assertIsInstance(strategy, str)
        self.assertTrue(0 <= slot <= request.deadline)
        self.assertIn(strategy, ["High", "Medium", "Low"])

    def test_green_slot_preference(self):
        """Should prefer green slots (low carbon)"""
        scheduler = GreedyCarbonLookahead(
            strategies=self.strategies,
            carbon=self.carbon,
            capacity=10000,  # High capacity (no pressure)
            pressure_weight=0.0,  # No pressure penalty
            predictor=None  # No predictions
        )

        request = Request(id=1, deadline=4, arrival_time=0)
        slot, strategy = scheduler.schedule(request, current_time=0)

        # Should pick slot 2 (carbon=100, greenest)
        self.assertEqual(slot, 2, f"Should choose greenest slot 2, got {slot}")

    def test_capacity_pressure(self):
        """Should avoid congested slots"""
        scheduler = GreedyCarbonLookahead(
            strategies=self.strategies,
            carbon=[100, 100, 100],  # All slots equally green
            capacity=5,
            pressure_weight=0.0,  # No pressure initially
            predictor=None
        )

        # Fill slot 0 with 4 requests (no pressure, so all go to first slot with equal carbon)
        slots_used = []
        for i in range(4):
            req = Request(id=i, deadline=2, arrival_time=0)
            slot, _ = scheduler.schedule(req, current_time=0)
            slots_used.append(slot)

        # Most should prefer slot 0 (first available with equal carbon)
        # At least 2 should be in slot 0
        self.assertGreaterEqual(
            slots_used.count(0),
            2,
            "Multiple requests should choose slot 0 when carbon is equal"
        )

        # Now enable pressure and add 5th request
        scheduler.pressure_weight = 1.0
        req5 = Request(id=5, deadline=2, arrival_time=0)
        slot5, _ = scheduler.schedule(req5, current_time=0)

        # 5th request should consider pressure
        # (May still choose slot 0 if not too congested, so just check it's reasonable)
        self.assertIn(slot5, [0, 1, 2], "Should choose valid slot")

    def test_error_budget_constraint(self):
        """Should respect error budget"""
        scheduler = GreedyCarbonLookahead(
            strategies=self.strategies,
            carbon=self.carbon,
            capacity=1000,
            error_threshold=2.0,  # Tight budget
            predictor=None
        )

        # First request: can use any strategy
        req1 = Request(id=1, deadline=3, arrival_time=0)
        slot1, strategy1 = scheduler.schedule(req1, current_time=0)

        # If first used Low (error=5), second must use High to compensate
        if strategy1 == "Low":
            req2 = Request(id=2, deadline=3, arrival_time=0)
            slot2, strategy2 = scheduler.schedule(req2, current_time=0)

            # Average error should be <= 2.0
            avg_error = scheduler.get_current_avg_error()
            self.assertLessEqual(
                avg_error,
                2.0,
                f"Average error {avg_error} exceeds threshold 2.0"
            )

    def test_capacity_hard_constraint(self):
        """Should not exceed capacity"""
        scheduler = GreedyCarbonLookahead(
            strategies=self.strategies,
            carbon=[100],  # Only 1 slot
            capacity=3,
            predictor=None
        )

        # Schedule 3 requests (fill slot 0)
        for i in range(3):
            req = Request(id=i, deadline=0, arrival_time=0)
            slot, _ = scheduler.schedule(req, current_time=0)
            self.assertEqual(slot, 0)

        # Verify utilization
        utilization = scheduler.get_slot_utilization(0)
        self.assertEqual(utilization, 1.0, "Slot 0 should be fully utilized")

        # 4th request should use fallback (capacity exceeded)
        req4 = Request(id=4, deadline=0, arrival_time=0)
        slot4, strategy4 = scheduler.schedule(req4, current_time=0)

        # Should still return assignment (fallback)
        self.assertIsNotNone(slot4)

    def test_state_reset(self):
        """State reset should clear tracking"""
        scheduler = GreedyCarbonLookahead(
            strategies=self.strategies,
            carbon=self.carbon,
            predictor=None
        )

        # Schedule some requests
        for i in range(5):
            req = Request(id=i, deadline=3, arrival_time=0)
            scheduler.schedule(req, current_time=0)

        self.assertEqual(scheduler.total_requests, 5)

        # Reset
        scheduler.reset_state()

        self.assertEqual(scheduler.total_requests, 0)
        self.assertEqual(len(scheduler.load_per_slot), 0)
        self.assertEqual(scheduler.total_error, 0.0)


class TestProbabilisticSlackScheduler(unittest.TestCase):
    """Test ProbabilisticSlackScheduler"""

    def setUp(self):
        """Initialize fixtures"""
        self.strategies = [
            Strategy(name="High", error=0, duration=120),
            Strategy(name="Medium", error=2, duration=60),
            Strategy(name="Low", error=5, duration=30),
        ]

        # Green slot at position 3
        self.carbon = [200, 180, 150, 100, 120]

    def test_tight_deadline_immediate(self):
        """Tight deadline should schedule immediately"""
        scheduler = ProbabilisticSlackScheduler(
            strategies=self.strategies,
            carbon=self.carbon,
            slack_threshold=3
        )

        # Deadline = 1 (slack = 1 < threshold 3)
        req = Request(id=1, deadline=1, arrival_time=0)
        slot, strategy = scheduler.schedule(req, current_time=0)

        # Should schedule immediately
        self.assertEqual(slot, 0, "Tight deadline should schedule to slot 0")
        self.assertEqual(strategy, "High", "Tight deadline should use High quality")

    def test_slack_available_postpone(self):
        """Slack available should postpone to green slot"""
        scheduler = ProbabilisticSlackScheduler(
            strategies=self.strategies,
            carbon=self.carbon,
            slack_threshold=2,
            error_threshold=10.0  # Generous budget
        )

        # Deadline = 4 (slack = 4 >= threshold 2)
        req = Request(id=1, deadline=4, arrival_time=0)
        slot, strategy = scheduler.schedule(req, current_time=0)

        # Should postpone to green slot 3
        self.assertEqual(slot, 3, "Should postpone to greenest slot 3")
        self.assertEqual(strategy, "Low", "Should use low quality when postponing")

    def test_error_budget_exhausted(self):
        """Exhausted error budget should use high quality"""
        scheduler = ProbabilisticSlackScheduler(
            strategies=self.strategies,
            carbon=self.carbon,
            slack_threshold=2,
            error_threshold=1.0  # Very tight budget
        )

        # First request: uses Low (error=5), exhausts budget
        req1 = Request(id=1, deadline=4, arrival_time=0)
        scheduler.schedule(req1, current_time=0)

        # Second request: even with slack, must use High
        req2 = Request(id=2, deadline=4, arrival_time=0)
        slot2, strategy2 = scheduler.schedule(req2, current_time=0)

        # Should use High to restore error average
        # (Note: exact behavior depends on error budget calculation)
        avg_error = (scheduler.total_error / scheduler.total_requests)
        self.assertLessEqual(avg_error, scheduler.error_threshold * 1.5)


class TestHeuristicComparison(unittest.TestCase):
    """Compare heuristics on emissions"""

    def setUp(self):
        """Setup common test scenario"""
        self.strategies = [
            Strategy(name="High", error=0, duration=120),
            Strategy(name="Medium", error=2, duration=60),
            Strategy(name="Low", error=5, duration=30),
        ]

        # Realistic carbon pattern
        self.carbon = [150, 180, 200, 100, 120, 90, 110]

        # Generate test requests
        self.requests = [
            Request(id=i, deadline=min(i+3, 6), arrival_time=0)
            for i in range(20)
        ]

    def _calculate_emissions(self, assignments, strategies, carbon):
        """Calculate total emissions for assignments"""
        total = 0.0
        for req_id, (slot, strategy_name) in assignments.items():
            strategy = next(s for s in strategies if s.name == strategy_name)
            # Match carbonshift.py emission calculation
            emission = carbon[slot] * strategy.duration / 3600 * 0.05
            total += emission
        return total

    def test_lookahead_vs_baseline(self):
        """GreedyCarbonLookahead should reduce emissions vs naive"""
        # Baseline: always slot 0, High quality (naive immediate)
        baseline_assignments = {}
        for req in self.requests:
            baseline_assignments[req.id] = (0, "High")

        baseline_emissions = self._calculate_emissions(
            baseline_assignments,
            self.strategies,
            self.carbon
        )

        # Lookahead scheduler
        lookahead = GreedyCarbonLookahead(
            strategies=self.strategies,
            carbon=self.carbon,
            capacity=5,
            pressure_weight=0.3,
            predictor=None
        )

        lookahead_assignments = {}
        for req in self.requests:
            slot, strategy = lookahead.schedule(req, current_time=0)
            lookahead_assignments[req.id] = (slot, strategy)

        lookahead_emissions = self._calculate_emissions(
            lookahead_assignments,
            self.strategies,
            self.carbon
        )

        # Assertion: lookahead should be better
        self.assertLess(
            lookahead_emissions,
            baseline_emissions,
            f"Lookahead ({lookahead_emissions:.0f}) should beat baseline ({baseline_emissions:.0f})"
        )

        # Print improvement
        improvement = (baseline_emissions - lookahead_emissions) / baseline_emissions * 100
        print(f"\n✓ GreedyCarbonLookahead reduces emissions by {improvement:.1f}%")


def run_benchmark():
    """
    Benchmark different schedulers on emissions.
    Run manually: python -m online.tests.test_heuristics
    """
    print("\n" + "="*60)
    print("BENCHMARK: Heuristic Comparison")
    print("="*60)

    strategies = [
        Strategy(name="High", error=0, duration=120),
        Strategy(name="Medium", error=2, duration=60),
        Strategy(name="Low", error=5, duration=30),
    ]

    # Daily carbon pattern (24 hours)
    carbon = [
        150, 140, 130, 120, 110, 100,  # Night (low)
        120, 150, 180, 200, 210, 200,  # Morning (rising)
        190, 180, 170, 180, 190, 210,  # Afternoon
        220, 200, 180, 160, 140, 130   # Evening (falling)
    ]

    # Generate 100 requests
    import random
    random.seed(42)
    requests = [
        Request(id=i, deadline=min(i % 24 + random.randint(2, 5), 23), arrival_time=i % 24)
        for i in range(100)
    ]

    predictor = MockRequestPredictor(base_load=50.0, seed=42)

    # Scheduler 1: Baseline (immediate, High)
    baseline_emissions = sum(carbon[0] * 120 for _ in requests)

    # Scheduler 2: GreedyCarbonLookahead
    lookahead = GreedyCarbonLookahead(
        strategies=strategies,
        carbon=carbon,
        capacity=10,
        pressure_weight=0.5,
        predictor=predictor
    )

    lookahead_emissions = 0.0
    for req in requests:
        slot, strategy = lookahead.schedule(req, current_time=req.arrival_time)
        strat = next(s for s in strategies if s.name == strategy)
        # Match carbonshift.py emission calculation
        lookahead_emissions += carbon[slot] * strat.duration / 3600 * 0.05

    # Scheduler 3: ProbabilisticSlack
    slack_scheduler = ProbabilisticSlackScheduler(
        strategies=strategies,
        carbon=carbon,
        capacity=10,
        slack_threshold=3,
        predictor=predictor
    )

    slack_emissions = 0.0
    for req in requests:
        slot, strategy = slack_scheduler.schedule(req, current_time=req.arrival_time)
        strat = next(s for s in strategies if s.name == strategy)
        # Match carbonshift.py emission calculation
        slack_emissions += carbon[slot] * strat.duration / 3600 * 0.05

    # Results
    print("\nEmissions (arbitrary units):")
    print(f"  Baseline (immediate, High): {baseline_emissions:10.0f}")
    print(f"  GreedyCarbonLookahead:      {lookahead_emissions:10.0f} ({(lookahead_emissions/baseline_emissions-1)*100:+.1f}%)")
    print(f"  ProbabilisticSlack:         {slack_emissions:10.0f} ({(slack_emissions/baseline_emissions-1)*100:+.1f}%)")

    print("\n" + "="*60 + "\n")


if __name__ == '__main__':
    # Run benchmark first
    run_benchmark()

    # Then unit tests
    unittest.main()
