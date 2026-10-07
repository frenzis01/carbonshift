from __future__ import annotations

import unittest
from unittest.mock import patch

from app import data
from app import main as main_module
from fastapi.testclient import TestClient


class ErrorMetricsTests(unittest.TestCase):
    def test_api_data_forwards_the_selected_profile(self):
        with patch.object(
            main_module,
            "get_dashboard_data",
            side_effect=lambda qos_profile_id=None: {"selected": qos_profile_id},
        ):
            with TestClient(main_module.app) as client:
                response = client.get("/api/data?qos_profile_id=qa-calibrated-v1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"selected": "qa-calibrated-v1"})

    def test_dashboard_page_uses_plotly_ui_revision_to_preserve_user_view_state(self):
        with TestClient(main_module.app) as client:
            response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("function plotUiRevision(chartName", response.text)
        self.assertIn("uirevision: uiRevision", response.text)
        self.assertIn("legend: { uirevision: uiRevision", response.text)

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
            "active_profiles": [],
            "profiles_unavailable_logged": False,
            "carbon_ci_list": [],
            "error_metrics_by_profile": {},
        }

        def fetch_json(url: str, timeout: float = 3.0):
            if url.startswith("http://carbonshift:8080/v1/carbon_intensity?"):
                return []
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
            self.assertEqual(next_slot_dashboard["error_plot"]["error_history"], [15.25, 17.0])
            self.assertEqual(next_slot_dashboard["error_plot"]["window_error_history"], [9.75, 11.0])

    def test_profile_view_filters_request_data_but_keeps_capacity_global_and_caches_separately(self):
        profiles = [
            {
                "profile_id": "qa-calibrated-v1",
                "active": True,
                "task_kind": "question_answering",
                "error_semantics": "word-overlap-f1-v1",
                "max_error_threshold": 20.0,
                "error_window": {
                    "past_slots": 4,
                    "future_slots": 3,
                    "past_decay_slots": 2,
                },
            },
            {
                "profile_id": "ner-calibrated-v1",
                "active": True,
                "task_kind": "ner",
                "error_semantics": "entity-set-f1-v1",
                "max_error_threshold": 30.0,
                "error_window": {
                    "past_slots": 5,
                    "future_slots": 2,
                    "past_decay_slots": 1,
                },
            },
            {
                "profile_id": "default-text-generation",
                "active": False,
                "task_kind": "text_generation",
                "error_semantics": "relative-confidence-degradation-v1",
                "max_error_threshold": 4.0,
                "error_window": {
                    "past_slots": 12,
                    "future_slots": 14,
                    "past_decay_slots": 12,
                },
            },
        ]
        requests = [
            {
                "request_id": 1,
                "qos_profile_id": "qa-calibrated-v1",
                "task": "question_answering",
                "scheduled_slot": 2,
                "arrival_slot": 1,
                "flavour": "Balanced",
                "actual_error_pct": 14.0,
                "status": "completed",
                "actual_carbon_cost": 3.0,
                "actual_baseline_carbon_cost": 5.0,
                "carbon_cost": 3.0,
                "baseline_carbon_cost": 5.0,
                "execution_time_seconds": 0.2,
                "baseline_execution_time_seconds": 0.3,
            },
            {
                "request_id": 2,
                "qos_profile_id": "ner-calibrated-v1",
                "task": "ner",
                "scheduled_slot": 2,
                "arrival_slot": 1,
                "flavour": "LowLatency",
                "actual_error_pct": 85.0,
                "status": "completed",
                "actual_carbon_cost": 9.0,
                "actual_baseline_carbon_cost": 10.0,
                "carbon_cost": 9.0,
                "baseline_carbon_cost": 10.0,
                "execution_time_seconds": 0.1,
                "baseline_execution_time_seconds": 0.2,
            },
        ]
        assignments = [
            {
                "request_id": 1,
                "qos_profile_id": "qa-calibrated-v1",
                "scheduled_slot": 2,
                "arrival_slot": 1,
                "flavour_name": "Balanced",
                "carbon_cost": 3.0,
                "error": 14.0,
            },
            {
                "request_id": 2,
                "qos_profile_id": "ner-calibrated-v1",
                "scheduled_slot": 2,
                "arrival_slot": 1,
                "flavour_name": "LowLatency",
                "carbon_cost": 9.0,
                "error": 85.0,
            },
        ]
        capacity_tiers = [
            {"max_requests": 30, "multiplier": 1.0},
            {"max_requests": None, "multiplier": 1.5},
        ]
        responses = {
            "http://client:8100/requests": requests,
            "http://client:8100/metrics/summary": {"scheduler": {"tasks": {}}},
            "http://carbonshift:8080/v1/profiles": profiles,
            "http://carbonshift:8080/v1/horizon": {"current_slot": 2, "total_slots": 100},
            "http://carbonshift:8080/v1/stats": {},
            "http://provider:9100/v1/slot": {"current_slot": 2},
            "http://carbonshift:8080/v1/assignments": assignments,
            "http://carbonshift:8080/v1/assignments?qos_profile_id=qa-calibrated-v1": [assignments[0]],
            "http://carbonshift:8080/v1/assignments?qos_profile_id=ner-calibrated-v1": [assignments[1]],
            "http://carbonshift:8080/v1/metrics/costs?qos_profile_id=qa-calibrated-v1": {
                "current_actual_carbon_cost": 3.0,
                "current_actual_baseline_carbon_cost": 5.0,
                "actual_carbon_saving_pct": 40.0,
                "forecasted_pending_carbon_cost": 0.0,
                "capacity_tiers": capacity_tiers,
            },
            "http://carbonshift:8080/v1/metrics/costs?qos_profile_id=ner-calibrated-v1": {
                "current_actual_carbon_cost": 9.0,
                "current_actual_baseline_carbon_cost": 10.0,
                "actual_carbon_saving_pct": 10.0,
                "forecasted_pending_carbon_cost": 0.0,
                "capacity_tiers": capacity_tiers,
            },
            "http://carbonshift:8080/v1/metrics/error-history?qos_profile_id=qa-calibrated-v1": {
                "current_slot": 2,
                "qos_profile_id": "qa-calibrated-v1",
                "profile_error_avg": 14.0,
                "global_error_avg": 49.5,
                "max_error_threshold": 20.0,
                "slots": [
                    {"slot": 0, "cumulative_error": 10.0, "window_error": 9.0},
                    {"slot": 1, "cumulative_error": 12.0, "window_error": 11.0},
                    {"slot": 2, "cumulative_error": 14.0, "window_error": 13.0},
                ],
            },
            "http://carbonshift:8080/v1/metrics/error-history?qos_profile_id=ner-calibrated-v1": {
                "current_slot": 2,
                "qos_profile_id": "ner-calibrated-v1",
                "profile_error_avg": 85.0,
                "global_error_avg": 49.5,
                "max_error_threshold": 30.0,
                "slots": [
                    {"slot": 0, "cumulative_error": 70.0, "window_error": 65.0},
                    {"slot": 1, "cumulative_error": 80.0, "window_error": 75.0},
                    {"slot": 2, "cumulative_error": 85.0, "window_error": 80.0},
                ],
            },
        }
        cache = {
            "client_requests": [],
            "client_summary": {},
            "capacity_tiers": [],
            "active_profiles": [],
            "carbon_ci_list": [],
            "error_metrics_by_profile": {},
        }

        def fetch_json(url: str, timeout: float = 3.0):
            if url.startswith("http://carbonshift:8080/v1/carbon_intensity?"):
                return []
            return responses.get(url)

        with (
            patch.object(data, "_cache", cache),
            patch.object(data, "fetch_json", side_effect=fetch_json),
        ):
            qa = data.get_dashboard_data("qa-calibrated-v1")
            ner = data.get_dashboard_data("ner-calibrated-v1")
            all_profiles = data.get_dashboard_data()

        self.assertEqual(qa["indicators"]["total_requests"], 1)
        self.assertEqual(qa["indicators"]["qos_profile_id"], "qa-calibrated-v1")
        self.assertEqual(qa["indicators"]["error_semantics"], "word-overlap-f1-v1")
        self.assertEqual(qa["indicators"]["max_error_threshold"], 20.0)
        self.assertEqual(qa["error_plot"]["displayed_error_avg"], 14.0)
        self.assertEqual(qa["error_plot"]["error_history_slots"], [0, 1, 2])
        self.assertEqual(qa["error_plot"]["error_history"], [10.0, 12.0, 14.0])
        self.assertEqual(qa["error_plot"]["window_error_history"], [9.0, 11.0, 13.0])
        self.assertEqual(qa["assignment_plot"]["balanced"][2], 1)
        self.assertEqual(qa["assignment_plot"]["fast"][2], 0)
        self.assertEqual(qa["assignment_plot"]["global_slot_occupancy"][2], 2)
        self.assertTrue(qa["assignment_plot"]["global_slot_occupancy_visible_by_default"])
        self.assertEqual(qa["assignment_plot"]["capacity_tiers"], capacity_tiers)
        self.assertEqual(
            qa["error_plot"]["profile_thresholds"],
            [
                {
                    "profile_id": "qa-calibrated-v1",
                    "task_kind": "question_answering",
                    "threshold": 20.0,
                },
                {
                    "profile_id": "ner-calibrated-v1",
                    "task_kind": "ner",
                    "threshold": 30.0,
                },
            ],
        )

        self.assertEqual(ner["indicators"]["qos_profile_id"], "ner-calibrated-v1")
        self.assertEqual(ner["error_plot"]["displayed_error_avg"], 85.0)
        self.assertEqual(ner["error_plot"]["error_history_slots"], [0, 1, 2])
        self.assertEqual(ner["error_plot"]["error_history"], [70.0, 80.0, 85.0])
        self.assertEqual(
            [profile["profile_id"] for profile in ner["active_profiles"]],
            ["qa-calibrated-v1", "ner-calibrated-v1"],
        )
        custom_flavour_index = ner["assignment_plot"]["flavours"].index("LowLatency")
        self.assertEqual(ner["assignment_plot"]["flavour_counts"][custom_flavour_index][2], 1)
        self.assertEqual(ner["error_plot"]["error_by_flavour"][custom_flavour_index][2], 85.0)
        self.assertEqual(cache["error_metrics_by_profile"]["qa-calibrated-v1"][2]["error_avg"], 14.0)
        self.assertEqual(cache["error_metrics_by_profile"]["ner-calibrated-v1"][2]["error_avg"], 85.0)

        self.assertEqual(all_profiles["error_plot"]["qos_profile_id"], None)
        self.assertEqual(
            all_profiles["error_plot"]["profile_thresholds"],
            qa["error_plot"]["profile_thresholds"],
        )

        responses["http://carbonshift:8080/v1/profiles"] = [profiles[0]]
        one_profile = data.get_dashboard_data()
        self.assertFalse(
            one_profile["assignment_plot"]["global_slot_occupancy_visible_by_default"]
        )


if __name__ == "__main__":
    unittest.main()
