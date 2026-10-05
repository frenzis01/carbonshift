from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
import requests

from .config import settings

logger = logging.getLogger("visualizer.data")

# Persistent cache across polling ticks so transient timeouts never wipe the UI
_cache: Dict[str, Any] = {
    "client_requests": [],
    "client_summary": {},
    "capacity_tiers": [],
    "max_error_threshold": 20.26,
    "carbon_ci_list": [],
    "error_metrics_by_slot": {},
}


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
    raw_client_requests: Optional[List[Dict[str, Any]]] = fetch_json(f"{settings.client_url}/requests")
    if raw_client_requests:
        _cache["client_requests"] = raw_client_requests
    client_requests: List[Dict[str, Any]] = _cache["client_requests"]

    raw_client_summary: Optional[Dict[str, Any]] = fetch_json(f"{settings.client_url}/metrics/summary")
    if raw_client_summary:
        _cache["client_summary"] = raw_client_summary
    client_summary: Dict[str, Any] = _cache["client_summary"]

    horizon_info: Dict[str, Any] = fetch_json(f"{settings.carbonshift_url}/v1/horizon") or {}
    stats_info: Dict[str, Any] = fetch_json(f"{settings.carbonshift_url}/v1/stats") or {}
    provider_slot: Dict[str, Any] = fetch_json(f"{settings.provider_url}/v1/slot") or {}

    current_slot = horizon_info.get("current_slot", stats_info.get("current_slot", 0))
    total_slots = horizon_info.get("total_slots", 8640)
    global_slot = provider_slot.get("current_slot")

    # Fetch with lookahead so carbon intensity and forecast for future slots are always available
    lookahead_slot = current_slot + 24
    raw_ci_list: Optional[List[Dict[str, Any]]] = fetch_json(
        f"{settings.carbonshift_url}/v1/carbon_intensity?until_slot={lookahead_slot}"
    )
    if raw_ci_list:
        _cache["carbon_ci_list"] = raw_ci_list
    carbon_ci_list: List[Dict[str, Any]] = _cache["carbon_ci_list"]

    # Fetch fine-grained Carbonshift endpoints
    cost_metrics: Dict[str, Any] = fetch_json(f"{settings.carbonshift_url}/v1/metrics/costs") or {}
    error_history: Dict[str, Any] = fetch_json(f"{settings.carbonshift_url}/v1/metrics/error-history") or {}
    engine_assignments: Optional[List[Dict[str, Any]]] = fetch_json(f"{settings.carbonshift_url}/v1/assignments")

    # 2. Extract Task Configurations (Capacity tiers & error threshold)
    scheduler_snapshot = client_summary.get("scheduler", {})
    tasks_cfg = scheduler_snapshot.get("tasks", {})
    if error_history.get("max_error_threshold"):
        _cache["max_error_threshold"] = float(error_history["max_error_threshold"])
    max_error_threshold = _cache["max_error_threshold"]

    capacity_tiers = []
    for t_name, t_info in tasks_cfg.items():
        if t_info.get("capacity_tiers"):
            capacity_tiers = t_info["capacity_tiers"]
            _cache["capacity_tiers"] = capacity_tiers
            break

    if not capacity_tiers:
        # Fallback to direct query on carbonshift
        for task_candidate in ("question_answering", "default", "text_generation", "ner"):
            task_resp = fetch_json(f"{settings.carbonshift_url}/v1/tasks/{task_candidate}")
            if task_resp and task_resp.get("capacity_tiers"):
                capacity_tiers = task_resp["capacity_tiers"]
                _cache["capacity_tiers"] = capacity_tiers
                break

    if not capacity_tiers and _cache["capacity_tiers"]:
        capacity_tiers = _cache["capacity_tiers"]

    # 3. Combine client_requests and engine_assignments
    seen_req_ids = set()
    all_req_items = []
    for r in client_requests:
        r_id = str(r.get("request_id"))
        seen_req_ids.add(r_id)
        all_req_items.append(r)
    for a in (engine_assignments or []):
        a_id = str(a.get("request_id"))
        if a_id not in seen_req_ids:
            all_req_items.append({
                "request_id": a.get("request_id"),
                "scheduled_slot": a.get("scheduled_slot"),
                "arrival_slot": a.get("arrival_slot"),
                "flavour": a.get("flavour_name"),
                "carbon_cost": a.get("carbon_cost"),
                "actual_error_pct": a.get("error"),
            })

    completed_requests = [r for r in all_req_items if r.get("status") == "completed"]
    scheduled_requests = [r for r in all_req_items if r.get("scheduled_slot") is not None]

    # Calculate KPI Indicators
    total_req_count = len(all_req_items) if all_req_items else stats_info.get("total", 0)
    scheduled_req_count = len(scheduled_requests) if scheduled_requests else (stats_info.get("scheduled", 0) + stats_info.get("completed", 0))
    completed_req_count = len(completed_requests) if completed_requests else stats_info.get("completed", 0)
    pending_req_count = max(0, total_req_count - scheduled_req_count)

    # Cost calculations (prefer fine-grained engine metrics, fallback to client tracking)
    if "current_actual_carbon_cost" in cost_metrics and cost_metrics.get("current_actual_baseline_carbon_cost", 0) > 0:
        actual_cost_sum = cost_metrics["current_actual_carbon_cost"]
        actual_baseline_cost_sum = cost_metrics["current_actual_baseline_carbon_cost"]
        actual_carbon_saving_pct = cost_metrics.get("actual_carbon_saving_pct")
        if actual_carbon_saving_pct is not None:
            actual_carbon_saving_pct = round(actual_carbon_saving_pct, 2)
        pending_forecasted_cost = cost_metrics.get("forecasted_pending_carbon_cost", 0.0)
    else:
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
        pending_forecasted_cost = sum(
            (r.get("carbon_cost") or 0.0)
            for r in all_req_items
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
    # Determine slot range for visualization: ALWAYS extend at least 12 slots into the future!
    assigned_slots = [r["scheduled_slot"] for r in scheduled_requests if r.get("scheduled_slot") is not None]
    max_assigned = max(assigned_slots) if assigned_slots else current_slot
    min_vis_slot = 0
    max_vis_slot = max(max_assigned, current_slot + 12, max_known_slot)

    slot_axis = list(range(min_vis_slot, max_vis_slot + 1))
    
    # Counts by flavour per slot
    fast_counts = [0] * len(slot_axis)
    balanced_counts = [0] * len(slot_axis)
    accurate_counts = [0] * len(slot_axis)
    slot_carbon_cost = [0.0] * len(slot_axis)
    slot_total_reqs = [0] * len(slot_axis)

    # Error sums per flavour per slot
    fast_error_sum = [0.0] * len(slot_axis)
    balanced_error_sum = [0.0] * len(slot_axis)
    accurate_error_sum = [0.0] * len(slot_axis)

    assign_map = {
        str(a.get("request_id")): a
        for a in (engine_assignments or [])
    }

    for r in all_req_items:
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
            slot_total_reqs[idx] += 1
            
            c_cost = r.get("actual_carbon_cost") if r.get("actual_carbon_cost") is not None else r.get("carbon_cost")
            if c_cost:
                slot_carbon_cost[idx] += float(c_cost)

            # Error tracking: prioritize actual_error_pct if reported, else fallback to assignment/flavour error
            err = r.get("actual_error_pct")
            if err is None:
                a_info = assign_map.get(str(r.get("request_id")))
                if a_info:
                    err = a_info.get("error")
            if err is not None:
                err_val = float(err)
                if flv == "fast":
                    fast_error_sum[idx] += err_val
                elif flv == "balanced":
                    balanced_error_sum[idx] += err_val
                elif flv == "accurate":
                    accurate_error_sum[idx] += err_val

    ci_forecast_series = [ci_by_slot.get(s, {}).get("forecast") for s in slot_axis]
    ci_actual_series = [ci_by_slot.get(s, {}).get("actual") for s in slot_axis]

    # Stacked fractional error contributions (sum == slot average error %)
    fast_error_contrib = []
    balanced_error_contrib = []
    accurate_error_contrib = []
    slot_error_avg = []

    for i in range(len(slot_axis)):
        n = slot_total_reqs[i]
        if n > 0:
            f_c = round(fast_error_sum[i] / n, 2)
            b_c = round(balanced_error_sum[i] / n, 2)
            a_c = round(accurate_error_sum[i] / n, 2)
            fast_error_contrib.append(f_c)
            balanced_error_contrib.append(b_c)
            accurate_error_contrib.append(a_c)
            slot_error_avg.append(round(f_c + b_c + a_c, 2))
        else:
            fast_error_contrib.append(0.0)
            balanced_error_contrib.append(0.0)
            accurate_error_contrib.append(0.0)
            slot_error_avg.append(None)

    # These are live scheduler snapshots, not values to project across slots.
    error_current_slot = error_history.get("current_slot", current_slot)
    current_slot_error = next(
        (
            item
            for item in error_history.get("slots", [])
            if item.get("slot") == error_current_slot
        ),
        {},
    )
    global_error_avg = error_history.get("global_error_avg")
    if global_error_avg is not None:
        global_error_avg = round(float(global_error_avg), 2)
    window_error_avg = current_slot_error.get("window_error")
    if window_error_avg is not None:
        window_error_avg = round(float(window_error_avg), 2)

    error_metrics_by_slot = _cache["error_metrics_by_slot"]
    if "current_slot" in error_history:
        if error_metrics_by_slot and error_current_slot < max(error_metrics_by_slot):
            error_metrics_by_slot.clear()
        error_metrics_by_slot[error_current_slot] = {
            "global_error_avg": global_error_avg,
            "window_error_avg": window_error_avg,
        }
    error_history_slots = sorted(error_metrics_by_slot)
    global_error_history = [
        error_metrics_by_slot[slot]["global_error_avg"]
        for slot in error_history_slots
    ]
    window_error_history = [
        error_metrics_by_slot[slot]["window_error_avg"]
        for slot in error_history_slots
    ]

    # 6. Request Input Distribution (arrival_slot vs CI)
    input_arrival_counts = [0] * len(slot_axis)
    offset = (global_slot - current_slot) if (global_slot is not None and current_slot is not None) else None
    assign_arrival_map = {
        str(a.get("request_id")): a.get("arrival_slot")
        for a in (engine_assignments or [])
        if a.get("arrival_slot") is not None
    }

    for r in all_req_items:
        req_id = str(r.get("request_id"))
        local_arr = assign_arrival_map.get(req_id)
        if local_arr is None:
            raw_arr = r.get("arrival_slot")
            if raw_arr is not None:
                if offset and raw_arr >= offset:
                    local_arr = raw_arr - offset
                elif raw_arr < 1000:
                    local_arr = raw_arr
        if local_arr is None:
            local_arr = r.get("scheduled_slot", current_slot)

        if local_arr is not None and min_vis_slot <= local_arr <= max_vis_slot:
            input_arrival_counts[local_arr - min_vis_slot] += 1

    return {
        "indicators": {
            "total_requests": total_req_count,
            "completed_requests": completed_req_count,
            "scheduled_requests": scheduled_req_count,
            "pending_requests": pending_req_count,
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
            "global_error_avg": global_error_avg,
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
            "fast_error": fast_error_contrib,
            "balanced_error": balanced_error_contrib,
            "accurate_error": accurate_error_contrib,
            "slot_error_avg": slot_error_avg,
            "window_error_avg": window_error_avg,
            "global_error_avg": global_error_avg,
            "error_history_slots": error_history_slots,
            "window_error_history": window_error_history,
            "global_error_history": global_error_history,
            "max_error_threshold": max_error_threshold,
        },
        "input_plot": {
            "slots": slot_axis,
            "arrived_requests": input_arrival_counts,
            "carbon_intensity_forecast": ci_forecast_series,
            "carbon_intensity_actual": ci_actual_series,
        },
    }
