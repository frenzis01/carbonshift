"""Metric utilities for N-shift speed benchmark outputs."""

from __future__ import annotations

from typing import Dict, List


def min_max_avg(values: List[float]) -> Dict[str, float]:
    if not values:
        return {"min": 0.0, "max": 0.0, "avg": 0.0}
    return {
        "min": float(min(values)),
        "max": float(max(values)),
        "avg": float(sum(values) / len(values)),
    }


def flatten_summary_for_csv(summary: Dict[str, float]) -> Dict[str, float]:
    return {
        "execution_mode": summary.get("execution_mode", "nshift_dp"),
        "batch_size": summary["batch_size"],
        "realtime_slots": summary.get("realtime_slots", False),
        "realtime_speed_scale": summary.get("realtime_speed_scale", 1.0),
        "baseline_flavour_name": summary.get("baseline_flavour_name", ""),
        "baseline_flavour_duration": summary.get("baseline_flavour_duration", 0),
        "baseline_flavour_error": summary.get("baseline_flavour_error", 0.0),
        "requests_total": summary["requests_total"],
        "requests_scheduled": summary["requests_scheduled"],
        "requests_unscheduled": summary["requests_unscheduled"],
        "batches_executed": summary["batches_executed"],
        "total_carbon_cost": summary["total_carbon_cost"],
        "global_average_error": summary["global_average_error"],
        "global_average_error_real": summary.get("global_average_error_real", summary["global_average_error"]),
        "global_average_error_modeled": summary.get("global_average_error_modeled", summary["global_average_error"]),
        "global_average_error_real_skip_first_k": summary.get("global_average_error_real_skip_first_k", 0.0),
        "global_average_error_modeled_skip_first_k": summary.get("global_average_error_modeled_skip_first_k", 0.0),
        "requests_assigned_with_greedy_fallback": summary.get("requests_assigned_with_greedy_fallback", 0),
        "requests_assigned_with_relaxed_retry": summary.get("requests_assigned_with_relaxed_retry", 0),
        "solver_time_ms_min": summary["solver_time_ms_min"],
        "solver_time_ms_max": summary["solver_time_ms_max"],
        "solver_time_ms_avg": summary["solver_time_ms_avg"],
        "queue_wait_seconds_min": summary["queue_wait_seconds_min"],
        "queue_wait_seconds_max": summary["queue_wait_seconds_max"],
        "queue_wait_seconds_avg": summary["queue_wait_seconds_avg"],
        "final_wait_seconds_min": summary["final_wait_seconds_min"],
        "final_wait_seconds_max": summary["final_wait_seconds_max"],
        "final_wait_seconds_avg": summary["final_wait_seconds_avg"],
        "baseline_total_carbon_cost": summary.get("baseline_total_carbon_cost", 0.0),
        "carbon_cost_saving_vs_baseline": summary.get("carbon_cost_saving_vs_baseline", 0.0),
        "carbon_cost_saving_vs_baseline_pct": summary.get("carbon_cost_saving_vs_baseline_pct", 0.0),
    }
