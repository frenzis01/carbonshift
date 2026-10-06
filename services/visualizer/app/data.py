from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
import requests
from urllib.parse import quote

from .config import settings

logger = logging.getLogger("visualizer.data")

# Persistent cache across polling ticks so transient timeouts never wipe the UI
_cache: Dict[str, Any] = {
    "client_requests": [],
    "client_summary": {},
    "capacity_tiers": [],
    "active_profiles": [],
    "profiles_unavailable_logged": False,
    "carbon_ci_list": [],
    "error_metrics_by_profile": {},
}


def fetch_json(url: str, timeout: float = settings.request_timeout_seconds) -> Optional[Any]:
    try:
        headers = {}
        if settings.carbonshift_api_key and url.startswith(f"{settings.carbonshift_url}/"):
            headers["X-API-Key"] = settings.carbonshift_api_key
        resp = requests.get(url, headers=headers, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:
        logger.debug("Failed to fetch %s: %s", url, exc)
    return None


def get_dashboard_data(qos_profile_id: str | None = None) -> Dict[str, Any]:
    # 1. Fetch raw data from microservices in parallel / sequence
    raw_client_requests: Optional[List[Dict[str, Any]]] = fetch_json(f"{settings.client_url}/requests")
    if raw_client_requests is not None:
        _cache["client_requests"] = raw_client_requests
    client_requests: List[Dict[str, Any]] = _cache["client_requests"]

    raw_client_summary: Optional[Dict[str, Any]] = fetch_json(f"{settings.client_url}/metrics/summary")
    if raw_client_summary:
        _cache["client_summary"] = raw_client_summary
    client_summary: Dict[str, Any] = _cache["client_summary"]

    raw_profiles: Optional[List[Dict[str, Any]]] = fetch_json(
        f"{settings.carbonshift_url}/v1/profiles"
    )
    if raw_profiles is not None:
        _cache["active_profiles"] = raw_profiles
        _cache["profiles_unavailable_logged"] = False
    elif not _cache.get("profiles_unavailable_logged", False):
        logger.warning(
            "could not load active QoS profiles; check Carbonshift connectivity and CARBONSHIFT_API_KEY"
        )
        _cache["profiles_unavailable_logged"] = True
    # The API already defaults to active-only results. Filter once more here
    # so a mixed-version service or test fixture cannot expose inactive
    # policies in the dashboard selector.
    active_profiles: List[Dict[str, Any]] = [
        profile
        for profile in _cache["active_profiles"]
        if profile.get("active") is True
    ]
    selected_profile = next(
        (profile for profile in active_profiles if profile.get("profile_id") == qos_profile_id),
        None,
    )
    if qos_profile_id is not None and selected_profile is None:
        logger.warning("requested QoS profile %s is not active; showing all profiles", qos_profile_id)
    selected_profile_id = selected_profile.get("profile_id") if selected_profile else None
    profile_query = (
        f"?qos_profile_id={quote(selected_profile_id, safe='')}"
        if selected_profile_id is not None
        else ""
    )

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
    cost_metrics: Dict[str, Any] = (
        fetch_json(f"{settings.carbonshift_url}/v1/metrics/costs{profile_query}") or {}
    )
    error_history: Dict[str, Any] = (
        fetch_json(f"{settings.carbonshift_url}/v1/metrics/error-history{profile_query}") or {}
    )
    global_assignments: Optional[List[Dict[str, Any]]] = fetch_json(
        f"{settings.carbonshift_url}/v1/assignments"
    )
    if selected_profile_id is None:
        engine_assignments = global_assignments
    else:
        engine_assignments = fetch_json(
            f"{settings.carbonshift_url}/v1/assignments{profile_query}"
        )

    # Capacity tiers are one shared pricing policy, even when the visible
    # request/error data is restricted to one QoS profile.
    capacity_tiers = cost_metrics.get("capacity_tiers")
    if isinstance(capacity_tiers, list):
        _cache["capacity_tiers"] = capacity_tiers
    else:
        capacity_tiers = _cache["capacity_tiers"]

    max_error_threshold = (
        error_history.get("max_error_threshold")
        if selected_profile_id is not None
        else None
    )
    if max_error_threshold is None and selected_profile is not None:
        max_error_threshold = selected_profile.get("max_error_threshold")

    # 3. Combine client_requests and engine_assignments
    seen_req_ids = set()
    all_req_items = []
    selected_assignment_ids = {
        str(assignment.get("request_id"))
        for assignment in (engine_assignments or [])
    }
    for r in client_requests:
        r_id = str(r.get("request_id"))
        request_profile_id = r.get("qos_profile_id")
        if selected_profile_id is not None and request_profile_id != selected_profile_id:
            # Older clients did not persist the profile returned by
            # Carbonshift. Their request can still be attributed safely when
            # the selected-profile assignment endpoint confirms its identity.
            if request_profile_id is not None or r_id not in selected_assignment_ids:
                continue
        seen_req_ids.add(r_id)
        all_req_items.append({
            **r,
            **({"qos_profile_id": selected_profile_id} if request_profile_id is None else {}),
        })
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
                "qos_profile_id": a.get("qos_profile_id"),
            })

    completed_requests = [r for r in all_req_items if r.get("status") == "completed"]
    scheduled_requests = [r for r in all_req_items if r.get("scheduled_slot") is not None]

    # Calculate KPI Indicators
    if selected_profile_id is not None:
        total_req_count = len(all_req_items)
        scheduled_req_count = len(scheduled_requests)
        completed_req_count = len(completed_requests)
        pending_req_count = max(0, total_req_count - scheduled_req_count)
    else:
        total_req_count = len(all_req_items) if all_req_items else stats_info.get("total", 0)
        scheduled_req_count = len(scheduled_requests) if scheduled_requests else (
            stats_info.get("scheduled", 0) + stats_info.get("completed", 0)
        )
        completed_req_count = len(completed_requests) if completed_requests else stats_info.get("completed", 0)
        pending_req_count = max(0, total_req_count - scheduled_req_count)

    # Cost calculations (prefer fine-grained engine metrics, fallback to client tracking)
    if "current_actual_carbon_cost" in cost_metrics:
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

    canonical_flavours = ["Accurate", "Balanced", "Fast"]
    observed_flavours = {
        str(request.get("flavour")).strip()
        for request in all_req_items
        if request.get("flavour")
    }
    canonical_by_name = {name.lower(): name for name in canonical_flavours}
    custom_flavours = sorted(
        (
            name
            for name in observed_flavours
            if name.lower() not in canonical_by_name
        ),
        key=str.casefold,
    )
    flavours = canonical_flavours + custom_flavours
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
    global_assigned_slots = [
        assignment["scheduled_slot"]
        for assignment in (global_assignments or [])
        if assignment.get("scheduled_slot") is not None
    ]
    if global_assigned_slots:
        max_assigned = max(max_assigned, max(global_assigned_slots))
    min_vis_slot = 0
    max_vis_slot = max(max_assigned, current_slot + 12, max_known_slot)

    slot_axis = list(range(min_vis_slot, max_vis_slot + 1))
    
    # Dynamic flavour arrays preserve custom profiles whose strategies are
    # not named Accurate/Balanced/Fast.
    flavour_counts = {flavour: [0] * len(slot_axis) for flavour in flavours}
    flavour_error_sums = {flavour: [0.0] * len(slot_axis) for flavour in flavours}
    slot_carbon_cost = [0.0] * len(slot_axis)
    global_slot_occupancy = [0] * len(slot_axis)
    profile_slot_counts = [0] * len(slot_axis)

    assign_map = {
        str(a.get("request_id")): a
        for a in (engine_assignments or [])
    }
    for assignment in (global_assignments or []):
        slot = assignment.get("scheduled_slot")
        if slot is not None and min_vis_slot <= slot <= max_vis_slot:
            global_slot_occupancy[slot - min_vis_slot] += 1

    for r in all_req_items:
        s = r.get("scheduled_slot")
        if s is not None and min_vis_slot <= s <= max_vis_slot:
            idx = s - min_vis_slot
            raw_flavour = (r.get("flavour") or "").strip()
            flavour = canonical_by_name.get(raw_flavour.lower(), raw_flavour)
            if flavour in flavour_counts:
                flavour_counts[flavour][idx] += 1
            profile_slot_counts[idx] += 1
            
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
                if flavour in flavour_error_sums:
                    flavour_error_sums[flavour][idx] += err_val

    ci_forecast_series = [ci_by_slot.get(s, {}).get("forecast") for s in slot_axis]
    ci_actual_series = [ci_by_slot.get(s, {}).get("actual") for s in slot_axis]

    # Each strategy's portion of the slot average; the arrays stay in the same
    # order as `flavours` for the visualizer to render without hard-coded names.
    flavour_error_contribs = {
        flavour: [] for flavour in flavours
    }
    slot_error_avg = []

    for i in range(len(slot_axis)):
        n = profile_slot_counts[i]
        if n > 0:
            for flavour in flavours:
                contribution = round(flavour_error_sums[flavour][i] / n, 2)
                flavour_error_contribs[flavour].append(contribution)
            slot_error_avg.append(round(
                sum(flavour_error_contribs[flavour][-1] for flavour in flavours),
                2,
            ))
        else:
            for flavour in flavours:
                flavour_error_contribs[flavour].append(0.0)
            slot_error_avg.append(None)

    fast_counts = flavour_counts["Fast"]
    balanced_counts = flavour_counts["Balanced"]
    accurate_counts = flavour_counts["Accurate"]
    fast_error_contrib = flavour_error_contribs["Fast"]
    balanced_error_contrib = flavour_error_contribs["Balanced"]
    accurate_error_contrib = flavour_error_contribs["Accurate"]

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
    profile_error_avg = error_history.get("profile_error_avg")
    if profile_error_avg is not None:
        profile_error_avg = round(float(profile_error_avg), 2)
    displayed_error_avg = (
        profile_error_avg if selected_profile_id is not None else global_error_avg
    )
    window_error_avg = current_slot_error.get("window_error")
    if window_error_avg is not None:
        window_error_avg = round(float(window_error_avg), 2)

    # Each view gets its own time series. A cached fleet point must never be
    # reused as a profile QoS point (or vice versa) after selector changes.
    cache_profile_key = selected_profile_id or "__all_profiles__"
    error_metrics_by_slot = _cache["error_metrics_by_profile"].setdefault(
        cache_profile_key, {}
    )
    if "current_slot" in error_history:
        if error_metrics_by_slot and error_current_slot < max(error_metrics_by_slot):
            error_metrics_by_slot.clear()
        error_metrics_by_slot[error_current_slot] = {
            "error_avg": displayed_error_avg,
            "window_error_avg": window_error_avg,
        }
    error_history_slots = sorted(error_metrics_by_slot)
    error_history_values = [
        error_metrics_by_slot[slot]["error_avg"]
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
        "active_profiles": active_profiles,
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
            "profile_error_avg": profile_error_avg,
            "max_error_threshold": max_error_threshold,
            "qos_profile_id": selected_profile_id,
            "selected_profile": selected_profile,
            "error_semantics": selected_profile.get("error_semantics") if selected_profile else None,
            "error_window": selected_profile.get("error_window") if selected_profile else None,
        },
        "assignment_plot": {
            "slots": slot_axis,
            "flavours": flavours,
            "flavour_counts": [flavour_counts[flavour] for flavour in flavours],
            "fast": fast_counts,
            "balanced": balanced_counts,
            "accurate": accurate_counts,
            "carbon_cost": [round(c, 3) for c in slot_carbon_cost],
            "carbon_intensity_forecast": ci_forecast_series,
            "carbon_intensity_actual": ci_actual_series,
            "global_slot_occupancy": global_slot_occupancy,
            "qos_profile_id": selected_profile_id,
            "capacity_tiers": capacity_tiers,
            "flavour_colors": {
                "Fast": "#1f77b4",
                "Balanced": "#2ca02c",
                "Accurate": "#ff7f0e",
            },
        },
        "error_plot": {
            "slots": slot_axis,
            "flavours": flavours,
            "error_by_flavour": [
                flavour_error_contribs[flavour] for flavour in flavours
            ],
            "fast_error": fast_error_contrib,
            "balanced_error": balanced_error_contrib,
            "accurate_error": accurate_error_contrib,
            "slot_error_avg": slot_error_avg,
            "window_error_avg": window_error_avg,
            "global_error_avg": global_error_avg,
            "profile_error_avg": profile_error_avg,
            "displayed_error_avg": displayed_error_avg,
            "error_avg_label": (
                "Profile cumulative error"
                if selected_profile_id is not None
                else "Fleet descriptive average"
            ),
            "qos_profile_id": selected_profile_id,
            "error_history_slots": error_history_slots,
            "window_error_history": window_error_history,
            "error_history": error_history_values,
            "max_error_threshold": max_error_threshold,
        },
        "input_plot": {
            "slots": slot_axis,
            "arrived_requests": input_arrival_counts,
            "carbon_intensity_forecast": ci_forecast_series,
            "carbon_intensity_actual": ci_actual_series,
        },
    }
