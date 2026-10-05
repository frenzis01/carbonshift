from __future__ import annotations

import unittest
from unittest.mock import patch

from app import data


class ErrorMetricsTests(unittest.TestCase):
    def test_error_plot_uses_current_scheduler_error_snapshots(self):
        responses = {
            "http://client:8100/requests": [
                {
                    "request_id": 1,
                    "scheduled_slot": 10,
                    "arrival_slot": 2,
                    "flavour": "Fast",
                    "actual_error_pct": 99.0,
                    "status": "scheduled",
                    "carbon_cost": 1.0,
                }
            ],
            "http://client:8100/metrics/summary": {
                "scheduler": {"tasks": {}, "global_error_avg": 88.0}
            },
            "http://carbonshift:8080/v1/horizon": {
                "current_slot": 2,
                "total_slots": 100,
            },
            "http://carbonshift:8080/v1/stats": {},
            "http://provider:9100/v1/slot": {"current_slot": 2},
            "http://carbonshift:8080/v1/metrics/costs": {},
            "http://carbonshift:8080/v1/metrics/error-history": {
                "current_slot": 2,
                "global_error_avg": 12.5,
                "max_error_threshold": 20.0,
                "slots": [
                    {"slot": 2, "window_error": 8.5},
                    {"slot": 10, "window_error": 99.0},
                ],
            },
            "http://carbonshift:8080/v1/assignments": [],
        }

        cache = {
            "client_requests": [],
            "client_summary": {},
            "capacity_tiers": [],
            "max_error_threshold": 20.26,
            "carbon_ci_list": [],
            "error_metrics_by_slot": {},
        }

        def fetch_json(url: str, timeout: float = 3.0):
            if url.startswith("http://carbonshift:8080/v1/carbon_intensity?"):
                return []
            if url.startswith("http://carbonshift:8080/v1/tasks/"):
                return None
            return responses.get(url)

        with (
            patch.object(data, "_cache", cache),
            patch.object(data, "fetch_json", side_effect=fetch_json),
        ):
            dashboard = data.get_dashboard_data()

            self.assertEqual(dashboard["indicators"]["global_error_avg"], 12.5)
            self.assertEqual(dashboard["error_plot"]["global_error_avg"], 12.5)
            self.assertEqual(dashboard["error_plot"]["window_error_avg"], 8.5)

            responses["http://carbonshift:8080/v1/metrics/error-history"] = {
                "current_slot": 2,
                "global_error_avg": 15.25,
                "max_error_threshold": 20.0,
                "slots": [
                    {"slot": 2, "window_error": 9.75},
                    {"slot": 10, "window_error": 99.0},
                ],
            }
            same_slot_dashboard = data.get_dashboard_data()
            self.assertEqual(same_slot_dashboard["error_plot"]["global_error_avg"], 15.25)
            self.assertEqual(same_slot_dashboard["error_plot"]["window_error_avg"], 9.75)

            responses["http://carbonshift:8080/v1/horizon"]["current_slot"] = 3
            responses["http://provider:9100/v1/slot"]["current_slot"] = 3
            responses["http://carbonshift:8080/v1/metrics/error-history"] = {
                "current_slot": 3,
                "global_error_avg": 17.0,
                "max_error_threshold": 20.0,
                "slots": [
                    {"slot": 3, "window_error": 11.0},
                    {"slot": 10, "window_error": 99.0},
                ],
            }
            next_slot_dashboard = data.get_dashboard_data()
            self.assertEqual(next_slot_dashboard["error_plot"]["error_history_slots"], [2, 3])
            self.assertEqual(next_slot_dashboard["error_plot"]["global_error_history"], [15.25, 17.0])
            self.assertEqual(next_slot_dashboard["error_plot"]["window_error_history"], [9.75, 11.0])


if __name__ == "__main__":
    unittest.main()
