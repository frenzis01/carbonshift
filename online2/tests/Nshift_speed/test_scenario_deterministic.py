import json
import tempfile
import unittest
from pathlib import Path

from tests.Nshift_speed.generate_scenario import generate_and_save_scenario


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


if __name__ == "__main__":
    unittest.main()

