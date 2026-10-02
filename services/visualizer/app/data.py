from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
import requests

from .config import settings

logger = logging.getLogger("visualizer.data")


def fetch_json(url: str, timeout: float = settings.request_timeout_seconds) -> Optional[Any]:
    try:
        resp = requests.get(url, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:
        logger.debug("Failed to fetch %s: %s", url, exc)
    return None


def get_dashboard_data() -> Dict[str, Any]:
    # 1. Fetch raw data from microservices in parallel / sequence
    client_requests: List[Dict[str, Any]] = fetch_json(f"{settings.client_url}/requests") or []
    client_summary: Dict[str, Any] = fetch_json(f"{settings.client_url}/metrics/summary") or {}
    carbon_ci_list: List[Dict[str, Any]] = fetch_json(f"{settings.carbonshift_url}/v1/carbon_intensity") or []
    horizon_info: Dict[str, Any] = fetch_json(f"{settings.carbonshift_url}/v1/horizon") or {}
    stats_info: Dict[str, Any] = fetch_json(f"{settings.carbonshift_url}/v1/stats") or {}
    provider_slot: Dict[str, Any] = fetch_json(f"{settings.provider_url}/v1/slot") or {}

    current_slot = horizon_info.get("current_slot", stats_info.get("current_slot", 0))
    total_slots = horizon_info.get("total_slots", 8640)
    global_slot = provider_slot.get("current_slot")

    # 2. Extract Task Configurations (Capacity tiers & error threshold)
    scheduler_snapshot = client_summary.get("scheduler", {})
    tasks_cfg = scheduler_snapshot.get("tasks", {})
    max_error_threshold = 20.0
    capacity_tiers = []
    for t_name, t_info in tasks_cfg.items():
        if t_info.get("max_error_threshold") is not None:
            max_error_threshold = float(t_info["max_error_threshold"])
        if t_info.get("capacity_tiers"):
            capacity_tiers = t_info["capacity_tiers"]
            break

    # 3. Calculate KPI Indicators
    completed_requests = [r for r in client_requests if r.get("status") == "completed"]
    scheduled_requests = [r for r in client_requests if r.get("scheduled_slot") is not None]
    
    # Cost calculations
    actual_cost_sum = sum(
        r["actual_carbon_cost"] if r.get("actual_carbon_cost") is not None else (r.get("carbon_cost") or 0.0)
        for r in completed_requests
    )
    actual_baseline_cost_sum = sum(
        r["actual_baseline_carbon_cost"] if r.get("actual_baseline_carbon_cost") is not None else (r.get("baseline_carbon_cost") or 0.0)
        for r in completed_requests
    )
    actual_carbon_saving_pct = None
    if actual_baseline_cost_sum > 0:
        actual_carbon_saving_pct = round(
            ((actual_baseline_cost_sum - actual_cost_sum) / actual_baseline_cost_sum) * 100.0, 2
        )

    # Forecasted cost for not-yet-processed requests
    # (requests either submitted/pending or scheduled for current/future slots that haven't finished yet)
    pending_forecasted_cost = sum(
        (r.get("carbon_cost") or 0.0)
        for r in client_requests
        if r.get("status") in ("submitted", "scheduled", "pending")
        and (r.get("scheduled_slot") is None or r.get("scheduled_slot") >= current_slot)
    )

    # Execution times overall & per flavour
    exec_times_all = [
        r["execution_time_seconds"] for r in completed_requests if r.get("execution_time_seconds") is not None
    ]
    baseline_exec_times_all = [
        r["baseline_execution_time_seconds"] for r in completed_requests if r.get("baseline_execution_time_seconds") is not None
    ]
    overall_avg_exec_sec = (sum(exec_times_all) / len(exec_times_all)) if exec_times_all else None
    overall_baseline_exec_sec = (sum(baseline_exec_times_all) / len(baseline_exec_times_all)) if baseline_exec_times_all else None

    flavours = ["Accurate", "Balanced", "Fast"]
    by_flavour_stats: Dict[str, Dict[str, Any]] = {}
    for flv in flavours:
        flv_reqs = [r for r in completed_requests if (r.get("flavour") or "").lower() == flv.lower()]
        flv_execs = [r["execution_time_seconds"] for r in flv_reqs if r.get("execution_time_seconds") is not None]
        flv_base = [r["baseline_execution_time_seconds"] for r in flv_reqs if r.get("baseline_execution_time_seconds") is not None]
        by_flavour_stats[flv] = {
            "count": len(flv_reqs),
            "avg_exec_sec": (sum(flv_execs) / len(flv_execs)) if flv_execs else None,
            "baseline_avg_sec": (sum(flv_base) / len(flv_base)) if flv_base else None,
        }

    # 4. Carbon Intensity Lookup Map
    ci_by_slot: Dict[int, Dict[str, Optional[float]]] = {}
    max_known_slot = current_slot
    for item in carbon_ci_list:
        s = item.get("slot")
        if s is not None:
            max_known_slot = max(max_known_slot, s)
            ci_by_slot[s] = {
                "forecast": item.get("forecast"),
                "actual": item.get("actual"),
            }

    # 5. Build Assignment Plot Data
    # Determine slot range for visualization
    assigned_slots = [r["scheduled_slot"] for r in scheduled_requests if r.get("scheduled_slot") is not None]
    min_vis_slot = 0
    max_vis_slot = max(assigned_slots + [current_slot, max_known_slot, 5])

    slot_axis = list(range(min_vis_slot, max_vis_slot + 1))
    
    # Counts by flavour per slot
    fast_counts = [0] * len(slot_axis)
    balanced_counts = [0] * len(slot_axis)
    accurate_counts = [0] * len(slot_axis)
    slot_carbon_cost = [0.0] * len(slot_axis)
    slot_error_sum = [0.0] * len(slot_axis)
    slot_error_count = [0] * len(slot_axis)

    for r in client_requests:
        s = r.get("scheduled_slot")
        if s is not None and min_vis_slot <= s <= max_vis_slot:
            idx = s - min_vis_slot
            flv = (r.get("flavour") or "").lower()
            if flv == "fast":
                fast_counts[idx] += 1
            elif flv == "balanced":
                balanced_counts[idx] += 1
            elif flv == "accurate":
                accurate_counts[idx] += 1
            
            c_cost = r.get("actual_carbon_cost") if r.get("actual_carbon_cost") is not None else r.get("carbon_cost")
            if c_cost:
                slot_carbon_cost[idx] += float(c_cost)

            # Error tracking
            err = r.get("actual_error_pct")
            if err is not None:
                slot_error_sum[idx] += float(err)
                slot_error_count[idx] += 1

    ci_forecast_series = [ci_by_slot.get(s, {}).get("forecast") for s in slot_axis]
    ci_actual_series = [ci_by_slot.get(s, {}).get("actual") for s in slot_axis]

    # Error per slot
    slot_error_avg = [
        (slot_error_sum[i] / slot_error_count[i]) if slot_error_count[i] > 0 else None
        for i in range(len(slot_axis))
    ]

    # 6. Request Input Distribution (arrival_slot vs CI)
    input_arrival_counts = [0] * len(slot_axis)
    for r in client_requests:
        arr = r.get("arrival_slot")
        if arr is None:
            # fallback: estimate arrival slot
            arr = r.get("scheduled_slot", 0)
        if min_vis_slot <= arr <= max_vis_slot:
            input_arrival_counts[arr - min_vis_slot] += 1

    return {
        "indicators": {
            "total_requests": len(client_requests),
            "completed_requests": len(completed_requests),
            "scheduled_requests": len(scheduled_requests),
            "pending_requests": len(client_requests) - len(scheduled_requests),
            "current_slot": current_slot,
            "global_slot": global_slot,
            "total_slots": total_slots,
            "actual_carbon_cost": round(actual_cost_sum, 4),
            "actual_baseline_carbon_cost": round(actual_baseline_cost_sum, 4),
            "actual_carbon_saving_pct": actual_carbon_saving_pct,
            "forecasted_pending_carbon_cost": round(pending_forecasted_cost, 4),
            "overall_avg_exec_sec": round(overall_avg_exec_sec, 4) if overall_avg_exec_sec is not None else None,
            "overall_baseline_exec_sec": round(overall_baseline_exec_sec, 4) if overall_baseline_exec_sec is not None else None,
            "by_flavour": by_flavour_stats,
            "global_error_avg": scheduler_snapshot.get("global_error_avg"),
            "max_error_threshold": max_error_threshold,
        },
        "assignment_plot": {
            "slots": slot_axis,
            "fast": fast_counts,
            "balanced": balanced_counts,
            "accurate": accurate_counts,
            "carbon_cost": [round(c, 3) for c in slot_carbon_cost],
            "carbon_intensity_forecast": ci_forecast_series,
            "carbon_intensity_actual": ci_actual_series,
            "capacity_tiers": capacity_tiers,
            "flavour_colors": {
                "Fast": "#1f77b4",
                "Balanced": "#2ca02c",
                "Accurate": "#ff7f0e",
            },
        },
        "error_plot": {
            "slots": slot_axis,
            "error_avg": slot_error_avg,
            "max_error_threshold": max_error_threshold,
            "global_error_avg": scheduler_snapshot.get("global_error_avg"),
        },
        "input_plot": {
            "slots": slot_axis,
            "arrived_requests": input_arrival_counts,
            "carbon_intensity_forecast": ci_forecast_series,
            "carbon_intensity_actual": ci_actual_series,
        },
    }
