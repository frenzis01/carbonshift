"""Shared utilities for online2 analysis notebooks."""
from __future__ import annotations

from typing import Dict, Tuple

import pandas as pd


FLAVOUR_COLORS: Dict[str, str] = {
    "Fast": "#1f77b4",
    "Balanced": "#2ca02c",
    "Accurate": "#ff7f0e",
}


def make_prehistory_by_slot(scenario: dict) -> Dict[int, Dict[str, float]]:
    """Build a ``{slot: {"count": int, "error": float}}`` mapping from a scenario dict."""
    return {
        int(p["slot"]): {
            "count": int(p["request_count"]),
            "error": float(p["error_per_request"]),
        }
        for p in scenario.get("prehistory_slots", [])
    }


def bool_series(values: pd.Series) -> pd.Series:
    """Coerce a column of various truthy representations to a boolean Series."""
    return values.astype(str).str.strip().str.lower().isin(["1", "true", "yes", "y", "t"])


def resolve_plot_mode(mode: str) -> str:
    """Validate and normalise a timeline highlight plot mode string."""
    normalized = str(mode).strip().lower()
    if normalized not in {"scatter", "stacked", "both"}:
        raise ValueError("TIMELINE_HIGHLIGHT_PLOT_MODE must be one of: scatter, stacked, both")
    return normalized


def prepare_local(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce and sort an assignment timeline DataFrame by arrival_slot / request_id."""
    local = df.copy()
    for col in ("request_id", "arrival_slot", "scheduled_slot"):
        local[col] = pd.to_numeric(local[col], errors="coerce")
    local = local.dropna(subset=["request_id", "arrival_slot", "scheduled_slot"]).copy()
    local["request_id"] = local["request_id"].astype(int)
    local["arrival_slot"] = local["arrival_slot"].astype(int)
    local["scheduled_slot"] = local["scheduled_slot"].astype(int)
    local["flavour_name"] = local.get("flavour_name", "Unknown").fillna("Unknown").astype(str)
    return local.sort_values(["arrival_slot", "request_id"]).reset_index(drop=True)


def flavour_order(local: pd.DataFrame) -> list:
    """Return flavour names ordered by FLAVOUR_COLORS key order, then alphabetically."""
    seen = set(local["flavour_name"].unique().tolist())
    ordered = [name for name in FLAVOUR_COLORS if name in seen]
    ordered.extend(sorted(name for name in seen if name not in ordered))
    return ordered


def compute_decay_window_avg(
    state_df: pd.DataFrame,
    current_slot: int,
    w_past: int,
    w_future: int,
    total_slots: int,
    decay_slots: int,
    prehistory_by_slot: Dict[int, Dict[str, float]],
) -> Tuple[float, float]:
    """Compute decay-weighted window average error for a single solver run.

    Returns ``(avg_real, avg_modeled)`` where *avg_real* covers only observed
    requests and *avg_modeled* also incorporates synthetic prehistory.  Returns
    ``float("nan")`` for each component when there is no data.

    *prehistory_by_slot* must be a ``{slot: {"count": int, "error": float}}``
    mapping as produced by :func:`make_prehistory_by_slot`.
    """
    window_start_modeled = current_slot - w_past
    window_start_real = max(0, window_start_modeled)
    window_end = min(total_slots - 1, current_slot + w_future)

    in_window = state_df[
        (state_df["scheduled_slot"] >= window_start_real)
        & (state_df["scheduled_slot"] <= window_end)
    ]
    real_sum = float(in_window["error"].sum()) if not in_window.empty else 0.0
    real_cnt = int(len(in_window))

    real_w_cnt = 0.0
    real_w_sum = 0.0
    mod_w_cnt = 0.0
    mod_w_sum = 0.0

    for offset in range(1, decay_slots + 1):
        decay_slot = window_start_modeled - offset
        weight = float(decay_slots - offset + 1) / float(decay_slots + 1)

        slot_real = state_df[state_df["scheduled_slot"] == decay_slot]["error"]
        sl_cnt = int(len(slot_real))
        sl_sum = float(slot_real.sum()) if sl_cnt else 0.0

        if sl_cnt > 0:
            w_cnt = float(sl_cnt) * weight
            real_w_cnt += w_cnt
            real_w_sum += (sl_sum / float(sl_cnt)) * w_cnt

        mod_sl_cnt = float(sl_cnt)
        mod_sl_sum = sl_sum
        pre = prehistory_by_slot.get(decay_slot)
        if pre is not None:
            mod_sl_cnt += float(pre["count"])
            mod_sl_sum += float(pre["count"]) * float(pre["error"])
        if mod_sl_cnt > 0.0:
            w_cnt = mod_sl_cnt * weight
            mod_w_cnt += w_cnt
            mod_w_sum += (mod_sl_sum / mod_sl_cnt) * w_cnt

    pre_cnt = 0
    pre_sum = 0.0
    for p_slot, p in prehistory_by_slot.items():
        if window_start_modeled <= p_slot <= window_end:
            pre_cnt += int(p["count"])
            pre_sum += float(p["count"]) * float(p["error"])

    real_cnt_eff = float(real_cnt) + real_w_cnt
    mod_cnt = float(real_cnt + pre_cnt) + mod_w_cnt
    avg_real = (real_sum + real_w_sum) / real_cnt_eff if real_cnt_eff > 0.0 else float("nan")
    avg_modeled = (real_sum + pre_sum + mod_w_sum) / mod_cnt if mod_cnt > 0.0 else float("nan")
    return avg_real, avg_modeled
