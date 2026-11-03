
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


def aggregate_and_save(run_dirs=None, prefix_regex=r"^(cfg)_test(1|2|3|4|5|6|7|8|9|10)_"):
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
    
    dfs = [load_run(d) for d in run_dirs]
    df_all = pd.concat(dfs, ignore_index=True)

    print(f"Righe totali: {len(df_all)}")
    print(f"Colonne: {list(df_all.columns)}")

    # sort columns to match this order
    cols = [
        'run_id',
        'scenario_id',
        'reqs_in',
        'reqs_out',
        'batch_size',
        'strategy',
        'parallelism',
        'solver_time_ms_avg',
        'elapsed_seconds',
        'carbon_cost',
        'carbon_saving',
        'reqs_fallback_assigned',
        'peak_concurrent_workers',
        'avg_concurrent_workers',
        'baseline_carbon_cost',
        'final_global_error',
        'path',

    ]
    df_all = df_all[cols]


    # round float columns to 2 decimal places
    float_cols = df_all.select_dtypes(include=["float64"]).columns
    df_all[float_cols] = df_all[float_cols].round(2)

    # Preview of the aggregated data

    # Save the aggregated dataframe
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
