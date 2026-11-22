"""Tests for reusable battery-analysis notebook helpers."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from matplotlib.colors import to_rgb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analysis_helpers  # noqa: E402


def test_setting_scenario_color_source_rebuilds_a_stale_palette(monkeypatch):
    scenario_ids = [
        "scen_1500_92_FLAT_IP_L",
        "scen_1800_230_FLAT_IP_L",
        "scen_2000_120_FLAT_IP_L",
    ]
    source = pd.DataFrame({"scenario_id": scenario_ids})

    cached_ids = [f"previous_scenario_{index}" for index in range(14)]
    cached_colors = analysis_helpers.sns.husl_palette(
        n_colors=len(cached_ids),
        s=0.5,
        l=0.36,
    ).as_hex()
    monkeypatch.setattr(
        analysis_helpers,
        "_SCENARIO_COLOR_MAP",
        dict(zip(cached_ids, cached_colors)),
    )
    monkeypatch.setattr(analysis_helpers, "_SCENARIO_COLOR_MAP_THEME", "dark")
    monkeypatch.setattr(analysis_helpers, "SCENARIO_COLOR_THEME", "dark")
    monkeypatch.setattr(analysis_helpers, "SHOW_SCENARIO_COLORS", True)
    monkeypatch.setattr(analysis_helpers, "SHOW_SCENARIO_HEADINGS", False)
    monkeypatch.setattr(analysis_helpers, "_SCENARIO_COLOR_SOURCE", None)
    monkeypatch.setattr(analysis_helpers, "_display", lambda _: None)

    analysis_helpers.set_scenario_color_source(source)
    analysis_helpers.display_by_scenario(source)

    colors = [
        to_rgb(analysis_helpers._SCENARIO_COLOR_MAP[scenario_id])
        for scenario_id in scenario_ids
    ]
    assert set(analysis_helpers._SCENARIO_COLOR_MAP) == set(scenario_ids)
    assert all(
        sum((left - right) ** 2 for left, right in zip(color_a, color_b)) ** 0.5
        > 0.2
        for index, color_a in enumerate(colors)
        for color_b in colors[index + 1 :]
    )
