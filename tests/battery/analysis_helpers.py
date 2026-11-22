"""Reusable analysis, table-formatting, and plotting helpers for battery runs."""
from __future__ import annotations

from html import escape
from typing import Any

import matplotlib.pyplot as plt
from matplotlib.colors import to_hex, to_rgb
import pandas as pd
import seaborn as sns
from IPython.display import HTML, display as _display


comparison_metric_sources = {
    "elapsed_s": "elapsed_s",
    "carb_saving": "carb_saving",
    "error": "error",
    "solver_ms": "solver_ms",
    "carb_cost": "carb_cost",
}

SHOW_SCENARIO_HEADINGS = True
SHOW_SCENARIO_COLORS = True
SCENARIO_COLOR_THEME = "dark"
_SCENARIO_COLOR_THEMES = {
    "light": {"saturation": 0.5, "lightness": 0.86, "text_color": "#202124"},
    "dark": {"saturation": 0.5, "lightness": 0.36, "text_color": "#f5f5f5"},
}
_SCENARIO_ROW_SHADE_AMOUNT = 0.1
_SCENARIO_COLOR_MAP: dict[str | None, str] = {}
_SCENARIO_COLOR_MAP_THEME: str | None = None
_SCENARIO_COLOR_SOURCE: pd.DataFrame | None = None


def configure_scenario_display(
    *,
    show_headings: bool = True,
    show_colors: bool = True,
    color_theme: str = "dark",
) -> None:
    """Configure scenario table headings and colors for notebook displays."""
    global SHOW_SCENARIO_HEADINGS, SHOW_SCENARIO_COLORS
    global SCENARIO_COLOR_THEME, _SCENARIO_COLOR_MAP_THEME

    if color_theme not in _SCENARIO_COLOR_THEMES:
        raise ValueError("color_theme must be 'light' or 'dark'")
    SHOW_SCENARIO_HEADINGS = show_headings
    SHOW_SCENARIO_COLORS = show_colors
    if SCENARIO_COLOR_THEME != color_theme:
        _SCENARIO_COLOR_MAP.clear()
        _SCENARIO_COLOR_MAP_THEME = None
    SCENARIO_COLOR_THEME = color_theme


def set_scenario_color_source(data: pd.DataFrame) -> None:
    """Set the full dataframe used to keep scenario colors consistent across tables."""
    if not isinstance(data, pd.DataFrame):
        raise TypeError("data must be a pandas DataFrame")
    if "scenario_id" not in data.columns:
        raise ValueError("data must contain a 'scenario_id' column")
    global _SCENARIO_COLOR_SOURCE, _SCENARIO_COLOR_MAP_THEME
    _SCENARIO_COLOR_SOURCE = data
    _SCENARIO_COLOR_MAP.clear()
    _SCENARIO_COLOR_MAP_THEME = None


def _scenario_color_key(scenario_id: Any) -> str | None:
    if pd.isna(scenario_id):
        return None
    return str(scenario_id).replace("_sine_IP_L", "")


def _shade_scenario_color(color: str, *, toward_white: bool) -> str:
    target = (1.0, 1.0, 1.0) if toward_white else (0.0, 0.0, 0.0)
    return to_hex(
        tuple(
            channel + (target_value - channel) * _SCENARIO_ROW_SHADE_AMOUNT
            for channel, target_value in zip(to_rgb(color), target)
        )
    )


def display_by_scenario(
    data: Any,
    *,
    title: str | None = None,
    head: int | None = None,
    show_scenario_headings: bool = True,
) -> None:
    """Display scenario-bearing dataframes as configurable HTML sections."""
    global _SCENARIO_COLOR_MAP_THEME
    if SCENARIO_COLOR_THEME not in _SCENARIO_COLOR_THEMES:
        raise ValueError("SCENARIO_COLOR_THEME must be 'light' or 'dark'")
    theme = _SCENARIO_COLOR_THEMES[SCENARIO_COLOR_THEME]

    if (
        not isinstance(data, pd.DataFrame)
        or "scenario_id" not in data.columns
        or data.empty
    ):
        _display(data)
        return

    if (
        SHOW_SCENARIO_COLORS
        and _SCENARIO_COLOR_MAP_THEME != SCENARIO_COLOR_THEME
    ):
        _SCENARIO_COLOR_MAP.clear()
        _SCENARIO_COLOR_MAP_THEME = SCENARIO_COLOR_THEME

    if SHOW_SCENARIO_COLORS and not _SCENARIO_COLOR_MAP:
        color_source = _SCENARIO_COLOR_SOURCE
        if color_source is None or "scenario_id" not in color_source.columns:
            color_source = data
        scenario_ids = sorted(
            {
                _scenario_color_key(scenario_id)
                for scenario_id in color_source["scenario_id"].dropna().unique()
            },
            key=str,
        )
        if color_source["scenario_id"].isna().any():
            scenario_ids.append(None)
        palette = sns.husl_palette(
            n_colors=max(len(scenario_ids), 1),
            s=theme["saturation"],
            l=theme["lightness"],
        ).as_hex()
        _SCENARIO_COLOR_MAP.update(
            {
                scenario_id: palette[index]
                for index, scenario_id in enumerate(scenario_ids)
            }
        )

    if title:
        _display(HTML(f"<h2>{escape(str(title))}</h2>"))

    for scenario_id, scenario_rows in data.groupby(
        "scenario_id", sort=True, dropna=False
    ):
        is_missing = pd.isna(scenario_id)
        label = "(missing scenario_id)" if is_missing else str(scenario_id)
        if SHOW_SCENARIO_COLORS:
            color_key = _scenario_color_key(scenario_id)
            if color_key not in _SCENARIO_COLOR_MAP:
                _SCENARIO_COLOR_MAP[color_key] = sns.husl_palette(
                    n_colors=len(_SCENARIO_COLOR_MAP) + 1,
                    s=theme["saturation"],
                    l=theme["lightness"],
                ).as_hex()[-1]
            color = _SCENARIO_COLOR_MAP[color_key]

        if SHOW_SCENARIO_HEADINGS and show_scenario_headings:
            heading_style = (
                f" style='margin:0.8em 0 0.35em;padding:0.25em 0.5em;"
                f"border-left:6px solid {color};background-color:{color};"
                f"color:{theme['text_color']}'"
                if SHOW_SCENARIO_COLORS
                else ""
            )
            _display(HTML(f"<h3{heading_style}>{escape(label)}</h3>"))

        rows = scenario_rows.head(head) if head is not None else scenario_rows
        rows = rows.reset_index(drop=True)
        if not SHOW_SCENARIO_COLORS:
            _display(rows)
            continue

        lighter_color = _shade_scenario_color(color, toward_white=True)
        darker_color = _shade_scenario_color(color, toward_white=False)
        styled_rows = rows.style.set_table_styles(
            [
                {
                    "selector": "thead th",
                    "props": [
                        ("background-color", color),
                        ("color", theme["text_color"]),
                        ("font-weight", "bold"),
                    ],
                },
                {
                    "selector": "tbody tr:nth-child(odd) td",
                    "props": [
                        ("background-color", lighter_color),
                        ("color", theme["text_color"]),
                    ],
                },
                {
                    "selector": "tbody tr:nth-child(even) td",
                    "props": [
                        ("background-color", darker_color),
                        ("color", theme["text_color"]),
                    ],
                },
            ]
        )
        _display(styled_rows)


def compare_metrics_wide(
    data: pd.DataFrame,
    *,
    row_keys: list[str],
    comparison_column: str,
    comparison_values: list[Any],
    metric_sources: dict[str, str],
    filters: dict[str, Any] | None = None,
    labels: dict[Any, str] | None = None,
    key_renames: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Pivot selected metrics into ordered columns for each comparison value."""
    filters = filters or {}
    required_columns = [
        *row_keys,
        comparison_column,
        *metric_sources.values(),
        *filters,
    ]
    missing_columns = [
        column for column in required_columns if column not in data.columns
    ]
    if missing_columns:
        raise ValueError(f"Comparison data is missing columns: {missing_columns}")

    selected = data
    for column, value in filters.items():
        if isinstance(value, (list, tuple, set, frozenset)):
            selected = selected.loc[selected[column].isin(value)]
        else:
            selected = selected.loc[selected[column].eq(value)]
    selected = selected.loc[selected[comparison_column].isin(comparison_values)].copy()
    if selected.empty:
        raise ValueError("No rows found for the requested comparison values and filters")

    duplicate_columns = [*row_keys, comparison_column]
    duplicate_rows = selected.duplicated(duplicate_columns, keep=False)
    if duplicate_rows.any():
        details = selected.loc[duplicate_rows, duplicate_columns].drop_duplicates()
        raise ValueError(
            "Expected one row per comparison key; duplicates found:\n"
            + details.to_string(index=False)
        )

    key_renames = key_renames or {}
    metric_renames = {
        source: metric for metric, source in metric_sources.items()
    }
    selected = selected.rename(columns={**key_renames, **metric_renames})
    output_keys = [key_renames.get(column, column) for column in row_keys]
    metrics = list(metric_sources)
    wide = selected.pivot(
        index=output_keys,
        columns=comparison_column,
        values=metrics,
    ).reindex(
        columns=pd.MultiIndex.from_product([metrics, comparison_values])
    )

    labels = labels or {}
    wide.columns = [
        f"{metric}_{labels.get(value, value)}"
        for metric, value in wide.columns
    ]
    result = wide.reset_index().sort_values(output_keys).reset_index(drop=True)
    comparison_columns = [
        f"{metric}_{labels.get(value, value)}"
        for metric in metrics
        for value in comparison_values
    ]
    return result.loc[:, [*output_keys, *comparison_columns]]


def top5_by_carbon_saving(
    data: pd.DataFrame,
    scenario_id: Any,
    top_n: int = 5,
    worst_n: bool = False,
    filter_offline: bool = False,
) -> pd.DataFrame:
    """Return the highest- or lowest-saving rows for one scenario."""
    required_columns = {"scenario_id", "carb_saving"}
    if filter_offline:
        required_columns.add("policy")
    missing_columns = required_columns - set(data.columns)
    if missing_columns:
        raise ValueError(f"Dataframe is missing required columns: {sorted(missing_columns)}")
    if not isinstance(top_n, int) or isinstance(top_n, bool) or top_n < 1:
        raise ValueError("top_n must be a positive integer")

    scenario_rows = data.loc[data["scenario_id"].eq(scenario_id)].copy()
    if filter_offline:
        scenario_rows = scenario_rows.loc[
            ~scenario_rows["policy"].str.startswith("offline_", na=False)
        ]
    if scenario_rows.empty:
        raise ValueError(f"No rows found for scenario_id={scenario_id!r}")

    scenario_rows["carb_saving"] = pd.to_numeric(
        scenario_rows["carb_saving"], errors="raise"
    )
    if scenario_rows["carb_saving"].isna().any():
        raise ValueError(f"Scenario {scenario_id!r} has missing carbon-saving values")

    return (
        scenario_rows.sort_values(
            "carb_saving", ascending=worst_n, kind="stable"
        )
        .head(top_n)
        .reset_index(drop=True)
    )


def top5_by_carbon_saving_all_scenarios(
    data: pd.DataFrame,
    top_n: int = 5,
    worst_n: bool = False,
    filter_offline: bool = False,
) -> pd.DataFrame:
    """Return up to top_n carbon-saving rows for each scenario in data."""
    if "scenario_id" not in data.columns:
        raise ValueError("Dataframe is missing required column: 'scenario_id'")

    scenario_ids = sorted(data["scenario_id"].dropna().unique())
    if not scenario_ids:
        raise ValueError("Dataframe contains no scenario IDs")

    return pd.concat(
        [
            top5_by_carbon_saving(
                data, scenario_id, top_n, worst_n, filter_offline
            )
            for scenario_id in scenario_ids
        ],
        ignore_index=True,
    )


def _select_top_policies(
    summary: pd.DataFrame,
    group_columns: list[str],
    top_n: int,
) -> pd.DataFrame:
    """Rank policies by carbon saving within each threshold/scenario group."""
    sort_columns = [*group_columns, "mean_carb_saving", "policy"]
    sort_order = [True] * len(group_columns) + [False, True]
    ranked = (
        summary.sort_values(sort_columns, ascending=sort_order, kind="stable")
        .groupby(group_columns, sort=False)
        .head(top_n)
        .copy()
    )
    ranked["rank"] = ranked.groupby(group_columns, sort=False).cumcount() + 1
    return ranked


def analyze_error_threshold_runs(
    data: pd.DataFrame,
    *,
    run_id: str = "T40",
    top_n: int = 1,
    exclude_offline: bool = True,
) -> dict[str, Any]:
    """Build and return threshold tables and summaries for later display or plotting."""
    if not isinstance(data, pd.DataFrame):
        raise TypeError("data must be a pandas DataFrame")
    if not isinstance(top_n, int) or isinstance(top_n, bool) or top_n < 1:
        raise ValueError("top_n must be a positive integer")

    required_columns = {
        "run_id",
        "thresh",
        "scenario_id",
        "policy",
        "path",
        "carb_saving",
        "error",
        "elapsed_s",
        "solver_ms",
    }
    missing_columns = required_columns - set(data.columns)
    if missing_columns:
        raise ValueError(f"Dataframe is missing required columns: {sorted(missing_columns)}")

    # Keep rows whose output path belongs to the requested run before filtering
    # the explicit run_id column; both checks protect against mixed run datasets.
    selected_runs = data.loc[
        data["path"].str.startswith(run_id, na=False)
        & data["run_id"].eq(run_id)
    ].copy()
    if selected_runs.empty:
        raise ValueError(f"No results found for run_id={run_id!r}")
    if selected_runs["thresh"].isna().any():
        missing_paths = selected_runs.loc[
            selected_runs["thresh"].isna(), "path"
        ].unique().tolist()
        raise ValueError(
            f"{run_id} runs are missing max_error_threshold config values: {missing_paths}"
        )

    if exclude_offline:
        selected_runs = selected_runs.loc[
            ~selected_runs["policy"].str.startswith("offline_", na=False)
        ].copy()
    if selected_runs.empty:
        raise ValueError(f"No eligible results found for run_id={run_id!r}")

    # Average repeats per scenario first, so each scenario has equal overall weight.
    scenario_summary = (
        selected_runs.groupby(["thresh", "policy", "scenario_id"], as_index=False)
        .agg(
            mean_carb_saving=("carb_saving", "mean"),
            mean_error=("error", "mean"),
            elapsed_s=("elapsed_s", "mean"),
            solver_ms=("solver_ms", "mean"),
            run_count=("path", "nunique"),
        )
    )
    overall_summary = (
        scenario_summary.groupby(["thresh", "policy"], as_index=False)
        .agg(
            mean_carb_saving=("mean_carb_saving", "mean"),
            mean_error=("mean_error", "mean"),
            elapsed_s=("elapsed_s", "mean"),
            solver_ms=("solver_ms", "mean"),
            scenario_count=("scenario_id", "nunique"),
            run_count=("run_count", "sum"),
        )
    )

    top_by_scenario = _select_top_policies(
        scenario_summary, ["scenario_id", "thresh"], top_n
    )
    top_overall = _select_top_policies(overall_summary, ["thresh"], top_n)
    return {
        "run_id": run_id,
        "table_top_n": top_n,
        "filtered_runs": selected_runs,
        "scenario_summary": scenario_summary,
        "overall_summary": overall_summary,
        "top_by_scenario": top_by_scenario,
        "top_overall": top_overall,
    }


def _get_threshold_analysis_summaries(
    analysis: dict[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """Validate analysis output and return its summaries and run label."""
    if not isinstance(analysis, dict):
        raise TypeError(
            "analysis must be the result returned by analyze_error_threshold_runs"
        )
    try:
        scenario_summary = analysis["scenario_summary"]
        overall_summary = analysis["overall_summary"]
        run_id = analysis["run_id"]
    except KeyError as exc:
        raise ValueError(
            "analysis is missing summaries from analyze_error_threshold_runs"
        ) from exc
    if not isinstance(scenario_summary, pd.DataFrame) or not isinstance(
        overall_summary, pd.DataFrame
    ):
        raise TypeError("analysis summaries must be pandas DataFrames")
    return scenario_summary, overall_summary, run_id


def display_error_threshold_tables(
    analysis: dict[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Display the scenario-specific and overall tables returned by the analyzer."""
    _get_threshold_analysis_summaries(analysis)
    try:
        table_top_n = analysis["table_top_n"]
        top_by_scenario = analysis["top_by_scenario"]
        top_overall = analysis["top_overall"]
    except KeyError as exc:
        raise ValueError(
            "analysis is missing ranked tables from analyze_error_threshold_runs"
        ) from exc

    display_by_scenario(
        top_by_scenario,
        title=f"Top {table_top_n} policies per error threshold and scenario",
    )
    _display(
        HTML(
            f"<h2>Top {table_top_n} policies per error threshold, averaged across scenarios</h2>"
        )
    )
    _display(top_overall)
    return top_by_scenario, top_overall


def _render_threshold_comparison(
    plot_specs: list[tuple[str, pd.DataFrame]],
    *,
    header: str,
    show_rank: bool = True,
    show_policy: bool = True,
) -> Any:
    """Render shared paired-bar styling for ranked and policy-specific plots."""
    if not plot_specs:
        raise ValueError("At least one threshold comparison is required")
    x_values = [0.0]
    for _, plot_data in plot_specs:
        x_values.extend(plot_data["mean_carb_saving"].tolist())
        x_values.extend(plot_data["thresh"].tolist())
    x_min, x_max = min(x_values), max(x_values)
    x_padding = max(2.0, (x_max - x_min) * 0.15)

    ncols = 2 if len(plot_specs) > 1 else 1
    nrows = (len(plot_specs) + ncols - 1) // ncols
    fig, axes = plt.subplots(
        nrows,
        ncols,
        figsize=(17 if ncols == 2 else 11, 6.5 * nrows),
        sharex=True,
    )
    axes = list(fig.axes)
    bar_height = 0.44
    bar_offset = 0.24
    for ax, (plot_title, plot_data) in zip(axes, plot_specs):
        plot_data = plot_data.sort_values(
            ["thresh", "rank"], kind="stable"
        ).reset_index(drop=True)
        positions = list(range(len(plot_data)))
        thresholds = plot_data["thresh"].tolist()
        carbon_savings = plot_data["mean_carb_saving"].tolist()
        saving_bars = ax.barh(
            [position - bar_offset for position in positions],
            carbon_savings,
            height=bar_height,
            color="#2e8b57",
            label="Carbon saving",
        )
        threshold_bars = ax.barh(
            [position + bar_offset for position in positions],
            thresholds,
            height=bar_height,
            color="#f58518",
            label="Error threshold",
        )

        for bars in (saving_bars, threshold_bars):
            for bar in bars:
                value = bar.get_width()
                is_positive = value >= 0
                ax.annotate(
                    f"{value:.1f}%",
                    xy=(value, bar.get_y() + bar.get_height() / 2),
                    xytext=(4 if is_positive else -4, 0),
                    textcoords="offset points",
                    ha="left" if is_positive else "right",
                    va="center",
                    fontsize=9,
                )

        ax.set_yticks(positions)
        if show_rank:
            labels = [
                f"{threshold:g}% #{rank} ({policy})"
                for threshold, rank, policy in zip(
                    thresholds, plot_data["rank"], plot_data["policy"]
                )
            ]
        elif show_policy:
            labels = [
                f"{threshold:g}% ({policy})"
                for threshold, policy in zip(thresholds, plot_data["policy"])
            ]
        else:
            labels = [f"{threshold:g}%" for threshold in thresholds]
        ax.set_yticklabels(labels)
        ax.set_xlim(x_min - x_padding, x_max + x_padding)
        ax.set_xlabel("Carbon saving / configured error threshold (%)")
        ax.set_ylabel("Threshold and policy" if show_policy else "Threshold")
        ax.set_title(plot_title)
        ax.grid(axis="x", alpha=0.25)
        ax.set_axisbelow(True)
        ax.legend()
        ax.invert_yaxis()

    for ax in axes[len(plot_specs) :]:
        ax.set_visible(False)
    fig.suptitle(header, y=1.0)
    fig.tight_layout()
    _display(fig)
    plt.close(fig)
    return fig


def plot_error_threshold_comparison(
    analysis: dict[str, Any],
    *,
    top_n: int = 1,
) -> Any:
    """Plot the top-N policies per threshold for each scenario and overall."""
    if not isinstance(top_n, int) or isinstance(top_n, bool) or top_n < 1:
        raise ValueError("top_n must be a positive integer")
    scenario_summary, overall_summary, run_id = _get_threshold_analysis_summaries(
        analysis
    )

    top_by_scenario = _select_top_policies(
        scenario_summary, ["scenario_id", "thresh"], top_n
    )
    top_overall = _select_top_policies(overall_summary, ["thresh"], top_n)
    plot_specs = [
        (
            str(scenario_id),
            top_by_scenario.loc[top_by_scenario["scenario_id"].eq(scenario_id)],
        )
        for scenario_id in sorted(top_by_scenario["scenario_id"].unique())
    ]
    plot_specs.append(("All scenarios (average)", top_overall))
    return _render_threshold_comparison(
        plot_specs,
        header=f"{run_id}: top {top_n} policies per configured error threshold",
    )


def plot_overall_policy_comparison(
    analysis: dict[str, Any],
    policy: str,
) -> Any:
    """Plot one explicitly selected policy using its across-scenario averages."""
    if not isinstance(policy, str) or not policy:
        raise ValueError("policy must be a non-empty policy name")
    _, overall_summary, run_id = _get_threshold_analysis_summaries(analysis)
    policy_results = overall_summary.loc[overall_summary["policy"].eq(policy)].copy()
    if policy_results.empty:
        available = sorted(overall_summary["policy"].dropna().astype(str).unique())
        raise ValueError(
            f"Policy {policy!r} has no overall results; available policies: {available}"
        )
    policy_results = policy_results.sort_values(
        "thresh", kind="stable"
    ).reset_index(drop=True)
    policy_results["rank"] = 1
    return _render_threshold_comparison(
        [(f"Overall average - {policy}", policy_results)],
        header=f"{run_id}: overall average for policy {policy}",
        show_rank=False,
        show_policy=False,
    )


def plot_policy_saving_by_threshold(
    analysis: dict[str, Any],
    *,
    scenario_id: str | None = None,
) -> Any:
    """Plot every policy's saving curve for one scenario or the overall average."""
    scenario_summary, overall_summary, run_id = _get_threshold_analysis_summaries(
        analysis
    )

    if scenario_id is None:
        plot_data = overall_summary.copy()
        view_label = "All scenarios (average)"
    else:
        plot_data = scenario_summary.loc[
            scenario_summary["scenario_id"].eq(scenario_id)
        ].copy()
        if plot_data.empty:
            available = sorted(scenario_summary["scenario_id"].dropna().unique())
            raise ValueError(
                f"No results for scenario_id={scenario_id!r}; "
                f"available scenarios: {available}"
            )
        view_label = f"Scenario {scenario_id}"

    required_columns = {"thresh", "policy", "mean_carb_saving"}
    missing_columns = required_columns - set(plot_data.columns)
    if missing_columns:
        raise ValueError(
            f"Analysis summary is missing columns: {sorted(missing_columns)}"
        )
    if plot_data["policy"].isna().any():
        raise ValueError("Analysis summary contains rows without a policy name")

    def policy_color_group(policy: str) -> str:
        if policy.startswith("offline_"):
            return policy.removeprefix("offline_")
        if policy.startswith("online_"):
            return policy.removeprefix("online_")
        return policy

    policy_names = sorted(
        plot_data["policy"].unique(),
        key=lambda policy: (policy.startswith("offline_"), policy),
    )
    color_groups = sorted({policy_color_group(policy) for policy in policy_names})
    palette = sns.husl_palette(n_colors=max(len(color_groups), 1)).as_hex()
    colors = dict(zip(color_groups, palette))

    import plotly.graph_objects as go

    fig = go.Figure()
    policy_styles: dict[str, dict[str, Any]] = {}
    for policy in policy_names:
        policy_rows = plot_data.loc[plot_data["policy"].eq(policy)].sort_values(
            "thresh", kind="stable"
        )
        is_offline = policy.startswith("offline_")
        color = colors[policy_color_group(policy)]
        style = {
            "color": color,
            "symbol": "diamond" if is_offline else "circle",
            "size": 11 if is_offline else 9,
            "opacity": 0.7 if is_offline else 1.0,
        }
        policy_styles[policy] = style
        fig.add_trace(
            go.Scatter(
                x=policy_rows["thresh"],
                y=policy_rows["mean_carb_saving"],
                mode="lines",
                name=policy,
                legendgroup=policy,
                line={
                    "color": color,
                    "dash": "dash" if is_offline else "solid",
                    "width": 3,
                },
                opacity=style["opacity"],
                hoverinfo="skip",
            )
        )

    hover_rows = plot_data.sort_values(
        ["thresh", "mean_carb_saving", "policy"],
        ascending=[True, False, True],
        kind="stable",
    )
    for point in hover_rows.itertuples(index=False):
        style = policy_styles[point.policy]
        fig.add_trace(
            go.Scatter(
                x=[point.thresh],
                y=[point.mean_carb_saving],
                mode="markers",
                name=point.policy,
                legendgroup=point.policy,
                showlegend=False,
                marker={
                    "color": style["color"],
                    "symbol": style["symbol"],
                    "size": style["size"],
                    "line": {"color": "white", "width": 1},
                },
                opacity=style["opacity"],
                hovertemplate=(
                    "<b>%{fullData.name}</b><br>"
                    "Error threshold: %{x:.2f}%<br>"
                    "Carbon saving: %{y:.2f}%<extra></extra>"
                ),
            )
        )

    fig.update_layout(
        width=1500,
        height=900,
        template="plotly_white",
        title={
            "text": (
                f"{run_id}: policy carbon saving vs error threshold"
                f"<br><sup>{view_label} - click legend entries to show/hide policies</sup>"
            ),
            "x": 0.5,
            "xanchor": "center",
            "font": {"size": 20},
        },
        legend={
            "title": {"text": "Policy"},
            "font": {"size": 13},
            "itemclick": "toggle",
            "itemdoubleclick": "toggleothers",
            "groupclick": "togglegroup",
            "x": 1.02,
            "y": 1,
            "xanchor": "left",
            "yanchor": "top",
        },
        hovermode="x unified",
        margin={"l": 100, "r": 310, "t": 125, "b": 90},
    )
    fig.update_xaxes(
        title_text="Error threshold (%)",
        ticksuffix="%",
        tickfont={"size": 13},
        title_font={"size": 16},
        showgrid=True,
    )
    fig.update_yaxes(
        title_text="Carbon saving (%)",
        ticksuffix="%",
        tickfont={"size": 13},
        title_font={"size": 16},
        showgrid=True,
    )
    _display(fig)
    return fig


__all__ = [
    "analyze_error_threshold_runs",
    "compare_metrics_wide",
    "comparison_metric_sources",
    "configure_scenario_display",
    "display_by_scenario",
    "display_error_threshold_tables",
    "plot_error_threshold_comparison",
    "plot_overall_policy_comparison",
    "plot_policy_saving_by_threshold",
    "set_scenario_color_source",
    "top5_by_carbon_saving",
    "top5_by_carbon_saving_all_scenarios",
]
