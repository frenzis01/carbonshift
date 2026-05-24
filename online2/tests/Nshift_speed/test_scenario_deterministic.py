import json
import tempfile
import unittest
from pathlib import Path

import config
from tests.Nshift_speed.generate_scenario import generate_and_save_scenario
from tests.conftest import config_override


class TestScenarioDeterminism(unittest.TestCase):
    def test_same_seed_produces_same_scenario(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            out1 = base / "scenario1.json"
            out2 = base / "scenario2.json"

            kwargs = {
                "seed": 1337,
                "total_slots": 12,
                "slot_duration_seconds": 5.0,
                "requests_per_slot": 4.0,
                "request_rate_std_factor": 0.25,
                "deadline_min_slack": 0,
                "deadline_max_slack": 5,
                "error_window_past": 5,
                "error_window_future": 8,
                "max_error_threshold": 4.0,
                "prehistory_error_ratio": 0.5,
            }

            scenario1 = generate_and_save_scenario(output_path=out1, **kwargs)
            scenario2 = generate_and_save_scenario(output_path=out2, **kwargs)

            self.assertEqual(scenario1, scenario2)

            self.assertEqual(
                json.loads(out1.read_text(encoding="utf-8")),
                json.loads(out2.read_text(encoding="utf-8")),
            )

    def test_prehistory_counts_follow_mock_influence(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            out_low = base / "scenario_low.json"
            out_high = base / "scenario_high.json"

            common = {
                "seed": 99,
                "total_slots": 16,
                "slot_duration_seconds": 10.0,
                "requests_per_slot": 12.0,
                "request_rate_std_factor": 0.30,
                "deadline_min_slack": 0,
                "deadline_max_slack": 8,
                "error_window_past": 5,
                "error_window_future": 8,
                "max_error_threshold": 4.0,
                "prehistory_error_ratio": 0.75,
                "carbon_random_noise_amplitude": 120.0,
            }

            scenario_low = generate_and_save_scenario(
                output_path=out_low,
                prehistory_mock_influence=0.4,
                **common,
            )
            scenario_high = generate_and_save_scenario(
                output_path=out_high,
                prehistory_mock_influence=1.0,
                **common,
            )

            total_low = sum(int(row["request_count"]) for row in scenario_low["prehistory_slots"])
            total_high = sum(int(row["request_count"]) for row in scenario_high["prehistory_slots"])
            self.assertLess(total_low, total_high)
            self.assertEqual(
                float(scenario_low["metadata"]["prehistory_mock_influence"]),
                0.4,
            )

    def test_default_prehistory_influence_uses_separate_config_parameter(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            out = base / "scenario.json"

        with config_override(PREHISTORY_MOCK_INFLUENCE=0.35, INFEASIBILITY_MOCK_INFLUENCE=0.95):
            scenario = generate_and_save_scenario(
                output_path=out,
                seed=42,
                total_slots=10,
                slot_duration_seconds=10.0,
                requests_per_slot=5.0,
                request_rate_std_factor=0.2,
                deadline_min_slack=0,
                deadline_max_slack=4,
                error_window_past=5,
                error_window_future=8,
                max_error_threshold=4.0,
                prehistory_error_ratio=0.75,
                carbon_random_noise_amplitude=100.0,
                prehistory_mock_influence=None,
            )

            self.assertAlmostEqual(
                float(scenario["metadata"]["prehistory_mock_influence"]),
                0.35,
                places=9,
            )


if __name__ == "__main__":
    unittest.main()
