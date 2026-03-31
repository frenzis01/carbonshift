"""
Tests for Request Predictor Module

Tests both mock predictor (diurnal pattern) and placeholder historical predictor.
"""

import unittest
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from online.request_predictor import MockRequestPredictor, HistoricalRequestPredictor


class TestMockRequestPredictor(unittest.TestCase):
    """Test MockRequestPredictor with bimodal pattern"""

    def setUp(self):
        """Initialize predictor with fixed seed for reproducibility"""
        self.predictor = MockRequestPredictor(
            base_load=500.0,
            morning_peak_hour=9,
            evening_peak_hour=19,
            peak_multiplier=2.5,
            night_multiplier=0.2,
            noise_stddev=0.1,
            seed=42  # Fixed seed
        )

    def test_morning_peak(self):
        """Morning peak (9 AM) should have high load"""
        morning_load = self.predictor.predict_load(9)
        base_load = self.predictor.predict_load(12)  # Mid-day baseline

        self.assertGreater(
            morning_load,
            base_load,
            "Morning peak should exceed baseline"
        )

    def test_evening_peak(self):
        """Evening peak (19 PM) should have high load"""
        evening_load = self.predictor.predict_load(19)
        base_load = self.predictor.predict_load(12)

        self.assertGreater(
            evening_load,
            base_load,
            "Evening peak should exceed baseline"
        )

    def test_night_valley(self):
        """Night (3 AM) should have low load"""
        night_load = self.predictor.predict_load(3)
        day_load = self.predictor.predict_load(12)

        self.assertLess(
            night_load,
            day_load,
            "Night load should be lower than day load"
        )

    def test_pattern_periodicity(self):
        """Pattern should repeat every 24 hours"""
        load_hour_5 = self.predictor._get_hourly_pattern(5)
        load_hour_29 = self.predictor._get_hourly_pattern(29)  # 29 % 24 = 5

        self.assertAlmostEqual(
            load_hour_5,
            load_hour_29,
            places=5,
            msg="Pattern should be periodic (24h cycle)"
        )

    def test_noise_variability(self):
        """Consecutive calls should give different values (noise)"""
        load1 = self.predictor.predict_load(10)
        load2 = self.predictor.predict_load(10)

        # With noise, should differ (unless extremely unlucky)
        # Note: With seed=42, this should be stable
        # We test that noise is applied (not deterministic without seed)

        predictor_no_seed = MockRequestPredictor(
            base_load=500.0,
            noise_stddev=0.1,
            seed=None  # No seed
        )

        loads = [predictor_no_seed.predict_load(10) for _ in range(10)]

        # Check variability
        load_std = (sum((x - sum(loads)/len(loads))**2 for x in loads) / len(loads))**0.5
        self.assertGreater(
            load_std,
            1.0,
            "Noise should create variability"
        )

    def test_daily_pattern_shape(self):
        """Daily pattern should have correct shape"""
        daily = self.predictor.get_daily_pattern()

        self.assertEqual(len(daily), 24, "Should have 24 hourly values")

        # Find peaks
        morning_peak_idx = daily.index(max(daily[:12]))  # Morning half
        evening_half = daily[12:]
        evening_peak_in_half = evening_half.index(max(evening_half))
        evening_peak_idx = 12 + evening_peak_in_half  # Evening half

        # Peaks should be near expected hours
        self.assertTrue(
            7 <= morning_peak_idx <= 11,
            f"Morning peak at hour {morning_peak_idx} (expected ~9)"
        )
        self.assertTrue(
            17 <= evening_peak_idx <= 21,
            f"Evening peak at hour {evening_peak_idx} (expected ~19)"
        )

    def test_predict_requests_count(self):
        """predict_requests should generate expected number of requests"""
        # Predict for 3 slots
        predicted = self.predictor.predict_requests(
            start_slot=10,
            end_slot=12,
            deadline_slack_range=(1, 3)
        )

        # Each slot generates ~predict_load() requests
        # Total should be roughly 3 * base_load * pattern_multiplier
        # With noise, allow some tolerance

        total_expected = sum(
            self.predictor.predict_load(t) for t in range(10, 13)
        )

        # Allow 20% tolerance due to rounding and noise
        self.assertAlmostEqual(
            len(predicted),
            total_expected,
            delta=total_expected * 0.3,
            msg=f"Expected ~{total_expected} requests, got {len(predicted)}"
        )

    def test_predict_requests_deadlines(self):
        """Predicted requests should have valid deadlines"""
        predicted = self.predictor.predict_requests(
            start_slot=5,
            end_slot=7,
            deadline_slack_range=(2, 4)
        )

        for req in predicted:
            # Deadline should be arrival + slack
            slack = req.deadline - req.arrival_time
            self.assertTrue(
                2 <= slack <= 4,
                f"Request {req.id} has invalid slack {slack} (expected [2,4])"
            )

    def test_non_negative_load(self):
        """Predicted load should never be negative"""
        # Test extreme night hours with noise
        for hour in range(24):
            for _ in range(20):  # Multiple samples
                load = self.predictor.predict_load(hour)
                self.assertGreaterEqual(
                    load,
                    0.0,
                    f"Load at hour {hour} is negative: {load}"
                )


class TestHistoricalRequestPredictor(unittest.TestCase):
    """Test HistoricalRequestPredictor (placeholder)"""

    def setUp(self):
        """Initialize with sample historical data"""
        self.historical_data = {
            0: 100.0,
            1: 120.0,
            2: 90.0,
            3: 80.0,
            4: 110.0,
        }
        self.predictor = HistoricalRequestPredictor(self.historical_data)

    def test_moving_average(self):
        """Prediction should use moving average of past window"""
        # Predict for slot 4 (window=3, so uses slots 1,2,3)
        predicted_load = self.predictor.predict_load(4)

        expected_avg = (120.0 + 90.0 + 80.0) / 3
        self.assertAlmostEqual(
            predicted_load,
            expected_avg,
            places=2,
            msg="Should return moving average of window"
        )

    def test_predict_with_no_history(self):
        """Prediction at start (no history) should return 0"""
        predicted_load = self.predictor.predict_load(0)
        self.assertEqual(predicted_load, 0.0, "No history → 0 load")

    def test_predict_requests(self):
        """predict_requests should generate requests"""
        predicted = self.predictor.predict_requests(2, 4)

        # Should have some requests (based on moving avg ~100)
        self.assertGreater(len(predicted), 0, "Should predict some requests")


class TestPredictorIntegration(unittest.TestCase):
    """Integration tests comparing predictors"""

    def test_mock_vs_deterministic_pattern(self):
        """Mock predictor without noise should be deterministic"""
        predictor = MockRequestPredictor(
            base_load=1000.0,
            noise_stddev=0.0,  # No noise
            seed=42
        )

        load1 = predictor.predict_load(15)
        load2 = predictor.predict_load(15)

        self.assertEqual(
            load1,
            load2,
            "Without noise, should be deterministic"
        )

    def test_predicted_request_format(self):
        """Predicted requests should have correct format"""
        predictor = MockRequestPredictor(seed=42)
        predicted = predictor.predict_requests(5, 6, deadline_slack_range=(1, 2))

        for req in predicted:
            # Check attributes
            self.assertTrue(hasattr(req, 'id'))
            self.assertTrue(hasattr(req, 'arrival_time'))
            self.assertTrue(hasattr(req, 'deadline'))
            self.assertTrue(hasattr(req, 'estimated'))

            # Check types
            self.assertIsInstance(req.id, str)
            self.assertIsInstance(req.arrival_time, int)
            self.assertIsInstance(req.deadline, int)
            self.assertTrue(req.estimated)  # All predicted requests flagged


def run_visual_test():
    """
    Visual test: print daily pattern to verify bimodal shape.
    Run manually: python -m online.tests.test_request_predictor
    """
    print("\n" + "="*60)
    print("VISUAL TEST: MockRequestPredictor Daily Pattern")
    print("="*60)

    predictor = MockRequestPredictor(
        base_load=500.0,
        morning_peak_hour=9,
        evening_peak_hour=19,
        noise_stddev=0.0  # No noise for visual clarity
    )

    daily = predictor.get_daily_pattern()

    print("\nHour | Load  | Bar Chart")
    print("-" * 60)

    max_load = max(daily)
    for hour, load in enumerate(daily):
        bar_length = int((load / max_load) * 40)
        bar = "█" * bar_length
        print(f"{hour:2d}   | {load:5.0f} | {bar}")

    print("\n" + "="*60)
    print("Expected: Two peaks around 9 AM and 7 PM")
    print("          Valley around 3 AM")
    print("="*60 + "\n")


if __name__ == '__main__':
    # Run visual test first
    run_visual_test()

    # Then run unit tests
    unittest.main()
