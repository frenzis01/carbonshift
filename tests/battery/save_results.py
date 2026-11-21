
import logging
from pathlib import Path
import json
import pandas as pd
import re

from nb_utils import OUTPUT_SCHEMA_VERSION, require_output_schema

# The working directory of the kernel can be either the repo root or the notebook folder.
# We select the root robustly by looking for tests/battery/results.
candidate_root = Path().resolve()
if (candidate_root / "tests" / "battery" / "results").exists():
    REPO_ROOT = candidate_root
else:
    # Il notebook è in tests/battery: saliamo di due livelli
    REPO_ROOT = candidate_root.parents[1]

RESULTS_DIR = REPO_ROOT / "tests" / "battery" / "results"

# prefix_regex = r"^(cfg)_test(1|2|3|4|5|6|7|8|9|10)_"




def _resolve_run_path(run_dir: Path, scenario_id: str, strategy: str) -> str:
    """Best-effort resolution of the execution subdirectory for a scenario and strategy."""
    sc_dir = run_dir / str(scenario_id)
    candidates = [
        f"rust_{strategy}",
        f"rust_online/{strategy}",
        f"rust_offline/strategy_{strategy.removeprefix('offline_')}" if strategy.startswith("offline_") else None,
        "rust_online" if strategy.startswith("online_") else None,
        "rust_offline" if strategy.startswith("offline_") else None,
    ]
    for cand in candidates:
        if cand and (sc_dir / cand).exists():
            return f"{run_dir.name}/{scenario_id}/{cand}"
    if strategy.startswith("online_"):
        return f"{run_dir.name}/{scenario_id}/rust_online/{strategy}"
    elif strategy.startswith("offline_"):
        return f"{run_dir.name}/{scenario_id}/rust_offline/strategy_{strategy.removeprefix('offline_')}"
    else:
        return f"{run_dir.name}/{scenario_id}/rust_{strategy}"


def load_run(run_dir: Path) -> pd.DataFrame:
    """Loads the results of a run and adds metadata."""
    require_output_schema(run_dir, schema_name="carbonshift.battery-run")
    run_id = run_dir.name.split("_")[0]

    config_path = run_dir / "battery_config.json"
    cfg = json.loads(config_path.read_text()) if config_path.exists() else {}

    # Find the battery_results CSV
    csv_files = list(run_dir.glob("battery_results_*.csv"))
    if not csv_files:
        raise FileNotFoundError(f"Nessun battery_results CSV trovato in {run_dir}")
    csv_path = csv_files[0]

    df = pd.read_csv(csv_path)
    df.insert(0, "run_id", run_id)
    scenario_thresholds = {
        scenario["id"]: scenario.get("max_error_threshold")
        for scenario in cfg.get("scenarios", [])
    }
    df["thresh"] = df["scenario_id"].map(scenario_thresholds)
    # round thresh
    df["thresh"] = df["thresh"].round(2)
    df["path"] = df.apply(
        lambda r: _resolve_run_path(run_dir, r["scenario_id"], r["strategy"]),
        axis=1,
    )
    df["parallelism"] = cfg.get("max_batch_solver_parallelism")

    # Join per-proposal elapsed time by scenario, strategy, and batch size.
    timing_files = list(run_dir.glob("per_n_timings_*.csv"))
    if timing_files:
        timings = pd.read_csv(timing_files[0])
        df = df.merge(
            timings[["scenario_id", "strategy", "batch_size", "elapsed_seconds"]],
            on=["scenario_id", "strategy", "batch_size"],
            how="left",
        )

    return df


def aggregate_and_save(
    run_dirs=None,
    prefix_regex=r"^(cfg)_test(1|2|3|4|5|6|7|8|9|10)_",
    sort_by=None,
):
    """Aggregate run results, optionally sort rows, and save the full-precision data."""
    if run_dirs is None:
        run_dirs = sorted(
            d for d in RESULTS_DIR.iterdir()
            if d.is_dir() and re.match(prefix_regex, d.name)
        )
        print(f"Trovate {len(run_dirs)} run directories")
        for d in run_dirs:
            print(" -", d.name)
    if not run_dirs:
        raise ValueError("No battery run directories match the requested selection")
    
    # dfs = [load_run(d) for d in run_dirs]
    dfs = []
    for d in run_dirs:
        try:
            dfs.append(load_run(d))
        except FileNotFoundError:
            logging.warning(f"No results for {d}")
    
    df_all = pd.concat(dfs, ignore_index=True)

    print(f"Righe totali: {len(df_all)}")

    # Rename source metrics to the concise names used by the active analysis.
    column_map = {
        "batch_size": "batch",
        "strategy": "policy",
        "parallelism": "paral",
        "final_global_error": "error",
        "elapsed_seconds": "elapsed_s",
        "carbon_cost": "carb_cost",
        "baseline_carbon_cost": "baseline_carb_cost",
        "carbon_saving": "carb_saving",
        "solver_time_ms_avg": "solver_ms",
    }
    collisions = [
        new_name
        for old_name, new_name in column_map.items()
        if old_name in df_all.columns and new_name in df_all.columns
    ]
    if collisions:
        raise ValueError(
            f"Cannot rename aggregate columns because target names already exist: {collisions}"
        )
    df_all = df_all.rename(columns=column_map)

    columns = [
        "run_id",
        "scenario_id",
        "reqs_in",
        "reqs_out",
        "batch",
        "policy",
        "carb_saving",
        "elapsed_s",
        "error",
        "thresh",
        "carb_cost",
        "reqs_fallback_assigned",
        "paral",
        "solver_ms",
        "peak_concurrent_workers",
        "avg_concurrent_workers",
        "baseline_carb_cost",
        "path",
    ]
    missing_columns = [column for column in columns if column not in df_all.columns]
    if missing_columns:
        raise ValueError(
            f"Aggregated run results are missing required columns: {missing_columns}"
        )
    ordered_columns = list(columns)
    ordered_columns.extend(
        column for column in df_all.columns if column not in ordered_columns
    )
    df_all = df_all.loc[:, ordered_columns]

    print(f"Colonne: {list(df_all.columns)}")

    if sort_by is not None:
        sort_columns = [sort_by] if isinstance(sort_by, str) else list(sort_by)
        missing_columns = [column for column in sort_columns if column not in df_all.columns]
        if missing_columns:
            raise ValueError(
                f"Cannot sort aggregate results by missing columns: {missing_columns}"
            )
        df_all = df_all.sort_values(
            by=sort_columns,
            kind="stable",
            na_position="last",
        ).reset_index(drop=True)

    output_csv = RESULTS_DIR / "aggregated_battery_results.csv"
    df_all.to_csv(output_csv, index=False)
    (RESULTS_DIR / "output_schema.json").write_text(
        json.dumps(
            {
                "schema_version": OUTPUT_SCHEMA_VERSION,
                "schema_name": "carbonshift.battery-aggregate",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Salvato in: {output_csv}")
    return df_all
