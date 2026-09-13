
from pathlib import Path
import json
import pandas as pd
import re

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




def _scenario_requests_total(run_dir: Path) -> dict:
    """Best-effort scenario_id -> requests_total lookup, read from each scenario's
    summary_by_n.csv / baseline_summary.csv. Used as a fallback for "requests_in"
    when the battery_results CSV predates that column (requests_total is constant
    per scenario_id regardless of infeasibility_mode/batch_size, so a single value
    read anywhere under the scenario folder is enough)."""
    totals = {}
    for scenario_dir in run_dir.iterdir():
        if not scenario_dir.is_dir():
            continue
        for csv_name in ("summary_by_n.csv", "baseline_summary.csv"):
            matches = list(scenario_dir.glob(f"**/{csv_name}"))
            if not matches:
                continue
            summary = pd.read_csv(matches[0])
            if "requests_total" in summary.columns and not summary.empty:
                totals[scenario_dir.name] = int(summary["requests_total"].iloc[0])
                break
    return totals


def _resolve_run_path(run_dir: Path, scenario_id: str, mode: str) -> str:
    """Best-effort resolution of the execution subdirectory for a given scenario and mode."""
    sc_dir = run_dir / str(scenario_id)
    candidates = [
        f"rust_{mode}",
        f"rust_online/{mode}",
        f"rust_offline/strategy_{mode.removeprefix('offline_')}" if str(mode).startswith("offline_") else None,
        "rust_greedy_fallback" if mode == "min_error_greedy" else None,
        "rust_online" if str(mode).startswith("online_") else None,
        "rust_offline" if str(mode).startswith("offline_") else None,
    ]
    for cand in candidates:
        if cand and (sc_dir / cand).exists():
            return f"{run_dir.name}/{scenario_id}/{cand}"
    if str(mode).startswith("online_"):
        return f"{run_dir.name}/{scenario_id}/rust_online/{mode}"
    elif str(mode).startswith("offline_"):
        return f"{run_dir.name}/{scenario_id}/rust_offline/strategy_{str(mode).removeprefix('offline_')}"
    else:
        return f"{run_dir.name}/{scenario_id}/rust_{mode}"


def load_run(run_dir: Path) -> pd.DataFrame:
    """Loads the results of a run and adds metadata."""
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
        lambda r: _resolve_run_path(run_dir, r["scenario_id"], r["infeasibility_mode"]),
        axis=1,
    )
    df["rollback_max_consecutive"] = cfg.get("rollback_max_consecutive")
    df["parallelism"] = cfg.get("max_batch_solver_parallelism")
    df["online_swarm_mode"] = cfg.get("online_swarm_mode")

    # requests_in: absent in older battery_results CSVs produced before this column
    # was added to run_battery.py's output. Fall back to requests_total read from
    # summary_by_n.csv / baseline_summary.csv, per scenario_id.
    if "requests_in" not in df.columns:
        df["requests_in"] = pd.NA
    missing_requests_in = df["requests_in"].isna()
    if missing_requests_in.any():
        totals = _scenario_requests_total(run_dir)
        df.loc[missing_requests_in, "requests_in"] = df.loc[missing_requests_in, "scenario_id"].map(totals)

    # Join with per_n_timings (elapsed_seconds per scenario/mode/batch_size)
    timing_files = list(run_dir.glob("per_n_timings_*.csv"))
    if timing_files:
        timings = pd.read_csv(timing_files[0])
        # Align naming with battery_results CSV columns
        timings = timings.rename(
            columns={
                "elapsed_seconds": "elapsed_seconds",
                "mode": "infeasibility_mode",
            }
        )
        # battery_results uses "batch_size"; per_n_timings also uses "batch_size"
        df = df.merge(
            timings[["scenario_id", "infeasibility_mode", "batch_size", "elapsed_seconds"]],
            on=["scenario_id", "infeasibility_mode", "batch_size"],
            how="left",
        )

    return df


def aggregate_and_save(run_dirs=None, prefix_regex=r"^(cfg)_test(1|2|3|4|5|6|7|8|9|10)_"):
    if not run_dirs:
        run_dirs = sorted(
            d for d in RESULTS_DIR.iterdir()
            if d.is_dir() and re.match(prefix_regex, d.name)
        )
        print(f"Trovate {len(run_dirs)} run directories")
        for d in run_dirs:
            print(" -", d.name)
    
    dfs = [load_run(d) for d in run_dirs]
    df_all = pd.concat(dfs, ignore_index=True)

    # Rename carbon cost column for brevity
    df_all = df_all.rename(columns={"total_carbon_cost": "carbon_cost"})
    # rename carbon_cost_saving_vs_baseline_pct to carbon_saving for brevity
    df_all = df_all.rename(columns={"carbon_cost_saving_vs_baseline_pct": "carbon_saving"})
    # # rename max_batch_solver_parallelism to parallelism for brevity
    # df_all = df_all.rename(columns={"max_batch_solver_parallelism": "parallelism"})

    # all rollbacks column with RB instead of rollback
    df_all = df_all.rename(columns={"total_rollbacks": "total_RBs"})
    df_all = df_all.rename(columns={"peak_consecutive_rollbacks": "peak_consecutive_RBs"})
    df_all = df_all.rename(columns={"rollback_max_consecutive": "RB_max_consecutive"})
    df_all = df_all.rename(columns={"requests_assigned_with_greedy_fallback": "req_assigned_with_GF"})



    print(f"Righe totali: {len(df_all)}")
    print(f"Colonne: {list(df_all.columns)}")


    # if requests_assigned_with_relaxed_retry is all 0, drop it
    if df_all["requests_assigned_with_relaxed_retry"].sum() == 0:
        df_all = df_all.drop(columns=["requests_assigned_with_relaxed_retry"])

    # sort columns to match this order
    cols = [
        'run_id',
        'scenario_id',
        'requests_in',
        'batch_size',
        'infeasibility_mode',
        'parallelism',
        'solver_time_ms_avg',
        'elapsed_seconds',
        'carbon_cost',
        'carbon_saving',
        'req_assigned_with_GF',
        'total_RBs',
        'peak_consecutive_RBs',
        'RB_max_consecutive',
        'peak_concurrent_workers',
        'avg_concurrent_workers',
        'online_swarm_mode',
        'baseline_carbon_cost',
        'final_global_error',
        'avg_global_error_per_slot',
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
    print(f"Salvato in: {output_csv}")

