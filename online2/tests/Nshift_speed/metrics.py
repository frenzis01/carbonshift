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
        "batch_size": summary["batch_size"],
        "requests_total": summary["requests_total"],
        "requests_scheduled": summary["requests_scheduled"],
        "requests_unscheduled": summary["requests_unscheduled"],
        "batches_executed": summary["batches_executed"],
        "total_carbon_cost": summary["total_carbon_cost"],
        "global_average_error": summary["global_average_error"],
        "solver_time_ms_min": summary["solver_time_ms_min"],
        "solver_time_ms_max": summary["solver_time_ms_max"],
        "solver_time_ms_avg": summary["solver_time_ms_avg"],
        "queue_wait_seconds_min": summary["queue_wait_seconds_min"],
        "queue_wait_seconds_max": summary["queue_wait_seconds_max"],
        "queue_wait_seconds_avg": summary["queue_wait_seconds_avg"],
        "final_wait_seconds_min": summary["final_wait_seconds_min"],
        "final_wait_seconds_max": summary["final_wait_seconds_max"],
        "final_wait_seconds_avg": summary["final_wait_seconds_avg"],
    }

