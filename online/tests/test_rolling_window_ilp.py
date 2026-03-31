"""
Tests for Rolling Window ILP Scheduler

Tests integration with carbonshift.py and correctness of rolling window approach.
"""

import unittest
import sys
import os
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from online.rolling_window_ilp import RollingWindowILPScheduler, Request, Strategy
from online.request_predictor import MockRequestPredictor


class TestRollingWindowILP(unittest.TestCase):
    """Test RollingWindowILPScheduler"""

    def setUp(self):
        """Initialize test fixtures"""
        self.strategies = [
            Strategy(name="High", error=0, duration=120),
            Strategy(name="Medium", error=2, duration=60),
            Strategy(name="Low", error=5, duration=30),
        ]

        self.carbon = [150, 180, 100, 120, 200, 90, 110, 130]

        self.predictor = MockRequestPredictor(base_load=50.0, seed=42)

    def test_initialization(self):
        """Should initialize without errors"""
        scheduler = RollingWindowILPScheduler(
            strategies=self.strategies,
            carbon=self.carbon,
            window_size=3,
            reopt_interval=60,
            predictor=self.predictor
        )

        self.assertIsNotNone(scheduler)
        self.assertEqual(scheduler.window_size, 3)

    def test_fallback_heuristic(self):
        """Should use fallback heuristic if ILP not yet run"""
        scheduler = RollingWindowILPScheduler(
            strategies=self.strategies,
            carbon=self.carbon,
            window_size=3,
            reopt_interval=1000,  # Long interval (won't trigger)
            predictor=self.predictor
        )

        request = Request(id=1, deadline=3, arrival_time=0)

        # First call: ILP won't trigger (interval not elapsed)
        slot, strategy = scheduler.schedule_request(request, current_time=0)

        # Should return valid assignment from fallback
        self.assertIsInstance(slot, int)
        self.assertIsInstance(strategy, str)
        self.assertIn(strategy, ["High", "Medium", "Low"])

    def test_statistics(self):
        """Should track statistics"""
        scheduler = RollingWindowILPScheduler(
            strategies=self.strategies,
            carbon=self.carbon,
            predictor=self.predictor
        )

        # Add some requests
        for i in range(5):
            req = Request(id=i, deadline=3, arrival_time=0)
            scheduler.schedule_request(req, current_time=0)

        stats = scheduler.get_statistics()

        self.assertIn('pending_requests', stats)
        self.assertIn('window_size', stats)
        self.assertEqual(stats['window_size'], 5)

    def test_commit_slot(self):
        """commit_slot should remove processed requests"""
        scheduler = RollingWindowILPScheduler(
            strategies=self.strategies,
            carbon=self.carbon,
            predictor=self.predictor
        )

        # Schedule requests
        req1 = Request(id=1, deadline=0, arrival_time=0)
        req2 = Request(id=2, deadline=2, arrival_time=0)

        scheduler.schedule_request(req1, current_time=0)
        scheduler.schedule_request(req2, current_time=0)

        # Commit slot 0
        scheduler.current_assignments[1] = (0, "High")  # Manually assign
        scheduler.commit_slot(current_time=0)

        # req1 should be removed from pending (assigned to slot 0)
        pending_ids = [req.id for req in scheduler.pending_requests]
        self.assertNotIn(1, pending_ids)


class TestRollingWindowIntegration(unittest.TestCase):
    """Integration test with actual carbonshift.py"""

    def setUp(self):
        """Setup for integration test"""
        # Check if carbonshift.py exists
        module_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        carbonshift_path = os.path.join(
            os.path.dirname(module_dir),
            'carbonshift.py'
        )

        if not os.path.exists(carbonshift_path):
            self.skipTest("carbonshift.py not found, skipping integration test")

        self.carbonshift_path = carbonshift_path

        self.strategies = [
            Strategy(name="High", error=0, duration=120),
            Strategy(name="Medium", error=2, duration=60),
            Strategy(name="Low", error=5, duration=30),
        ]

        self.carbon = [150, 180, 100, 120, 200, 90, 110, 130, 140, 160]

    @unittest.skip("Integration test - run manually when carbonshift.py is available")
    def test_ilp_optimization(self):
        """
        Full integration test with carbonshift.py.

        Skipped by default (requires carbonshift.py setup).
        Run manually to verify ILP integration.
        """
        scheduler = RollingWindowILPScheduler(
            strategies=self.strategies,
            carbon=self.carbon,
            window_size=5,
            reopt_interval=0,  # Trigger immediately
            ilp_timeout=30.0,
            predictor=MockRequestPredictor(base_load=20.0, seed=42),
            carbonshift_path=self.carbonshift_path
        )

        # Generate requests
        requests = [
            Request(id=i, deadline=min(i+3, 9), arrival_time=0)
            for i in range(10)
        ]

        # Schedule all requests (should trigger ILP)
        assignments = {}
        for req in requests:
            slot, strategy = scheduler.schedule_request(req, current_time=0)
            assignments[req.id] = (slot, strategy)
            print(f"Request {req.id}: slot={slot}, strategy={strategy}")

        # Assertions
        for req_id, (slot, strategy) in assignments.items():
            self.assertIsNotNone(slot)
            self.assertIsNotNone(strategy)
            self.assertIn(strategy, ["High", "Medium", "Low"])


class TestHybridScheduler(unittest.TestCase):
    """Test HybridScheduler (stub test)"""

    def test_placeholder(self):
        """Placeholder test for HybridScheduler"""
        # HybridScheduler is more complex; thorough testing requires
        # integration with both heuristic and ILP components
        # This is a minimal placeholder
        self.assertTrue(True, "HybridScheduler tests to be implemented")


def run_integration_visual_test():
    """
    Visual test for rolling window behavior.
    Run manually: python -m online.tests.test_rolling_window_ilp
    """
    print("\n" + "="*60)
    print("VISUAL TEST: Rolling Window ILP Behavior")
    print("="*60)

    strategies = [
        Strategy(name="High", error=0, duration=120),
        Strategy(name="Medium", error=2, duration=60),
        Strategy(name="Low", error=5, duration=30),
    ]

    carbon = [150, 180, 100, 120, 200, 90, 110, 130]

    predictor = MockRequestPredictor(base_load=30.0, seed=42)

    scheduler = RollingWindowILPScheduler(
        strategies=strategies,
        carbon=carbon,
        window_size=4,
        reopt_interval=0,  # Always re-optimize (for testing)
        ilp_timeout=5.0,
        predictor=predictor
    )

    print("\nScheduling 8 requests:")
    print("-" * 60)

    for i in range(8):
        req = Request(id=i, deadline=min(i+3, 7), arrival_time=0)
        slot, strategy = scheduler.schedule_request(req, current_time=0)
        print(f"Request {i:2d} (deadline={req.deadline}) → slot={slot}, strategy={strategy}")

    print("\nNote: If carbonshift.py not available, uses fallback heuristic")
    print("="*60 + "\n")


if __name__ == '__main__':
    # Run visual test
    run_integration_visual_test()

    # Run unit tests
    unittest.main()
