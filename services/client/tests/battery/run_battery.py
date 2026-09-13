#!/usr/bin/env python3
"""Battery-style, config-driven test runner for the client.

Mirrors the *spirit* of `carbonshift/tests/battery/run_battery.py` (config-
driven scenarios, a timestamped output folder per run, CSV + README
summaries) but is adapted for this component's very different nature: a
live, HTTP/async/callback-driven end-to-end test across carbonshift +
executor + client, instead of a pure in-process Rust solver benchmark. Output
goes to a separate location and a simpler, per-scenario format (see
`output_dir` below) rather than reusing the Rust battery's `tests/results/`.

Requires carbonshift and the executor already running. Scenarios with
`"mode": "emulated"` additionally require both to be running in manual-clock
mode (`MANUAL_CLOCK=1` / `EXECUTOR_MANUAL_CLOCK=1`) — see README.md
"Emulazione a tempo fittizio". `"mode": "realtime"` works against normal,
real-time-clock services but a scenario can then take as long as its
`slot_minutes * number_of_slots`.

Usage (from client/):
    python tests/battery/run_battery.py [--config tests/battery/battery_config.json]
    python tests/battery/run_battery.py --config tests/battery/battery_config_micro.json

Config file (JSON) — see battery_config.json / battery_config_micro.json for
full examples:
    battery_id             : str   – label used in the output folder name
    client_url             : str   – base URL of the running client
    executor_url           : str   – base URL of the running executor admin
                                      API (only used for "emulated" scenarios)
    output_dir             : str   – where per-run folders are written
                                      (relative to this repo's client/ root)
    poll_interval_seconds  : float – how often to re-poll while waiting for a
                                      scenario's requests to resolve
    poll_timeout_seconds   : float – give up waiting after this long (still
                                      writes out whatever resolved so far);
                                      for "emulated" scenarios this also caps
                                      how long extra manual-clock ticks are
                                      issued to drain any work carbonshift's
                                      DP solver deferred past the plan's own
                                      slots (its horizon isn't bounded by
                                      what the plan itself spans, and its
                                      clock is a single global counter shared
                                      across the whole battery run, so later
                                      scenarios may need more of this budget
                                      than earlier ones)
    admin_timeout_seconds  : float – timeout for each individual advance-slot
                                      call while draining (default 60)
    scenarios              : list  – each entry:
        id           : str  – unique label (subfolder + CSV row name)
        task         : "text_generation" | "ner" | "question_answering"
        count        : int  – total number of requests to send (5-10 for a
                               quick "does the architecture work" microtest,
                               much larger for a real performance evaluation)
        per_slot     : int  – requests per timeslot (default 5)
        slot_minutes : float (default 30)
        source       : "synthetic" (no download) | "dataset" (real HF data,
                       see client/README.md for which dataset backs each task)
        seed         : int (default 42)
        mode         : "realtime" | "emulated" (default "emulated")
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

CLIENT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CLIENT_ROOT))

from app.plan_builder import build_requests  # noqa: E402

DEFAULT_OUTPUT_DIR = CLIENT_ROOT / "tests" / "battery" / "results"
DEFAULT_CONFIG = CLIENT_ROOT / "tests" / "battery" / "battery_config.json"

_RESOLVED_STATUSES = {"completed", "failed", "timed_out"}

SCENARIO_CSV_COLUMNS = [
    "scenario_id", "task", "mode", "source", "count",
    "requests_sent", "requests_scheduled", "requests_completed", "requests_failed", "requests_timed_out",
    "avg_quality_score", "avg_confidence", "avg_execution_time_seconds",
    "avg_ack_latency_seconds", "avg_carbon_saving_pct", "elapsed_seconds",
]


def _get_requests(client_url: str) -> list[dict[str, Any]]:
    resp = requests.get(f"{client_url}/requests", timeout=10)
    resp.raise_for_status()
    return resp.json()


def _new_items(client_url: str, before_ids: set[str]) -> list[dict[str, Any]]:
    return [i for i in _get_requests(client_url) if str(i["request_id"]) not in before_ids]


def _advance_emulated_clock(carbonshift_url: str, executor_url: str, admin_timeout: float) -> bool:
    """One manual-clock tick: advances carbonshift (which flushes/dispatches
    anything due) then the executor (which runs anything now due). Returns
    False without raising on any failure, so the caller can just stop
    draining instead of crashing the whole battery run."""
    try:
        requests.post(f"{carbonshift_url}/v1/admin/advance-slot", timeout=admin_timeout).raise_for_status()
        requests.post(f"{executor_url}/admin/advance-slot", timeout=admin_timeout).raise_for_status()
        return True
    except requests.RequestException:
        return False


def _wait_for_scenario(client_url: str, before_ids: set[str], expected_count: int, mode: str,
                       carbonshift_url: str, executor_url: str, poll_interval: float,
                       poll_timeout: float, admin_timeout: float) -> list[dict[str, Any]]:
    """Waits until this scenario's own requests (those not present in
    `before_ids`) have all reached a terminal status, or gives up after
    `poll_timeout` seconds and returns whatever is available so far.

    carbonshift's DP solver is free to defer a request to any slot within its
    horizon to optimize carbon cost, not just the slots the plan itself
    advanced through (and, since carbonshift's manual clock is a single
    global counter shared across the whole battery run, a later scenario's
    requests can be scheduled arbitrarily far past a *previous* scenario's
    own slots too) — so in `mode="emulated"` we keep "fast-forwarding" the
    manual clock with extra ticks (as fast as `poll_timeout` allows) instead
    of just waiting on a clock nothing is advancing. This stops on 3
    consecutive advance-slot failures (carbonshift/executor unreachable),
    falling back to passive polling for the rest of `poll_timeout`.
    """
    deadline = time.monotonic() + poll_timeout
    items: list[dict[str, Any]] = []
    consecutive_advance_failures = 0
    while time.monotonic() < deadline:
        items = _new_items(client_url, before_ids)
        if len(items) >= expected_count and all(i["status"] in _RESOLVED_STATUSES for i in items):
            return items
        if mode == "emulated" and consecutive_advance_failures < 3:
            if _advance_emulated_clock(carbonshift_url, executor_url, admin_timeout):
                consecutive_advance_failures = 0
                continue
            consecutive_advance_failures += 1
        time.sleep(poll_interval)
    resolved = sum(1 for i in items if i["status"] in _RESOLVED_STATUSES)
    print(f"  ! timed out after {poll_timeout}s waiting for scenario to resolve "
          f"({len(items)}/{expected_count} tracked, {resolved} resolved)")
    return items


def _stats(values: list[float]) -> dict[str, Optional[float]]:
    if not values:
        return {"avg": None, "min": None, "max": None}
    return {"avg": sum(values) / len(values), "min": min(values), "max": max(values)}


def _scenario_metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [i for i in items if i["status"] == "completed"]
    quality = [i["result"]["quality_score"] for i in completed
               if i.get("result") and i["result"].get("quality_score") is not None]
    confidence = [i["result"]["confidence"] for i in completed
                  if i.get("result") and i["result"].get("confidence") is not None]
    exec_time = [i["execution_time_seconds"] for i in completed if i["execution_time_seconds"] is not None]
    ack = [i["ack_latency_seconds"] for i in items]
    saving = [i["carbon_saving_pct"] for i in items if i["carbon_saving_pct"] is not None]
    return {
        "requests_sent": len(items),
        # `scheduled_slot` reflects only carbonshift's synchronous ack at
        # submit time; a `completed` request necessarily went through
        # scheduling regardless of what that initial ack said (see
        # RequestTracker.progress() in app/tracker.py for the same fix).
        "requests_scheduled": sum(1 for i in items if i["scheduled_slot"] is not None or i["status"] == "completed"),
        "requests_completed": len(completed),
        "requests_failed": sum(1 for i in items if i["status"] == "failed"),
        "requests_timed_out": sum(1 for i in items if i["status"] == "timed_out"),
        "avg_quality_score": _stats(quality)["avg"],
        "avg_confidence": _stats(confidence)["avg"],
        "avg_execution_time_seconds": _stats(exec_time)["avg"],
        "avg_ack_latency_seconds": _stats(ack)["avg"],
        "avg_carbon_saving_pct": _stats(saving)["avg"],
    }


def _run_scenario(scenario: dict[str, Any], client_url: str, carbonshift_url: str, executor_url: str,
                   poll_interval: float, poll_timeout: float, admin_timeout: float,
                   scenario_dir: Path) -> dict[str, Any]:
    task = scenario["task"]
    count = int(scenario["count"])
    per_slot = int(scenario.get("per_slot", 5))
    slot_minutes = float(scenario.get("slot_minutes", 30.0))
    source = scenario.get("source", "synthetic")
    seed = int(scenario.get("seed", 42))
    mode = scenario.get("mode", "emulated")

    print(f"[{scenario['id']}] task={task} count={count} source={source} mode={mode}")

    before_ids = {str(i["request_id"]) for i in _get_requests(client_url)}
    plan = build_requests(task, count, per_slot, slot_minutes, source, seed)

    t0 = time.monotonic()
    resp = requests.post(f"{client_url}/run/send-plan", json={
        "requests": plan, "slot_minutes": slot_minutes, "mode": mode, "executor_url": executor_url,
    })
    resp.raise_for_status()

    items = _wait_for_scenario(client_url, before_ids, count, mode, carbonshift_url, executor_url,
                               poll_interval, poll_timeout, admin_timeout)
    elapsed = time.monotonic() - t0

    scenario_dir.mkdir(parents=True, exist_ok=True)
    (scenario_dir / "config.json").write_text(json.dumps(scenario, indent=2))
    with open(scenario_dir / "requests.jsonl", "w", encoding="utf-8") as f:
        for i in items:
            f.write(json.dumps(i) + "\n")

    metrics = _scenario_metrics(items)
    metrics["elapsed_seconds"] = elapsed
    (scenario_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))

    print(f"  -> {metrics['requests_completed']}/{metrics['requests_sent']} completed, "
          f"avg_carbon_saving_pct={metrics['avg_carbon_saving_pct']}, elapsed={elapsed:.1f}s")

    row = {"scenario_id": scenario["id"], "task": task, "mode": mode, "source": source, "count": count}
    row.update(metrics)
    return row


def _fmt(v: Any) -> str:
    return f"{v:.3f}" if isinstance(v, float) else str(v)


def _write_battery_readme(run_dir: Path, battery_id: str, start_dt: datetime, rows: list[dict[str, Any]]) -> None:
    lines = [
        f"# Client battery run: {battery_id}",
        "",
        f"- Started: {start_dt.isoformat(timespec='seconds')}",
        f"- Scenarios: {len(rows)}",
        "",
        "`avg_carbon_saving_pct` is each request's `(baseline_carbon_cost - carbon_cost) "
        "/ baseline_carbon_cost * 100`, averaged — the baseline being carbonshift's own "
        "estimate of running that same request immediately, with the most accurate "
        "flavour, with no carbon-intensity optimization.",
        "",
        "## Results",
        "",
        "| scenario | task | mode | sent | completed | failed | timed_out | "
        "avg quality | avg confidence | avg exec time (s) | avg ack latency (s) | avg carbon saving (%) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['scenario_id']} | {r['task']} | {r['mode']} | {r['requests_sent']} | "
            f"{r['requests_completed']} | {r['requests_failed']} | {r['requests_timed_out']} | "
            f"{_fmt(r['avg_quality_score'])} | {_fmt(r['avg_confidence'])} | "
            f"{_fmt(r['avg_execution_time_seconds'])} | {_fmt(r['avg_ack_latency_seconds'])} | "
            f"{_fmt(r['avg_carbon_saving_pct'])} |"
        )
    lines.append("")
    lines.append("Per-scenario raw request records (`requests.jsonl`), the scenario's own config "
                  "snapshot (`config.json`) and its computed metrics (`metrics.json`) are in each "
                  "scenario's own subfolder next to this file.")
    lines.append("")
    (run_dir / "README.md").write_text("\n".join(lines) + "\n")


def run_battery(config_path: Path) -> None:
    with open(config_path) as f:
        cfg = json.load(f)

    battery_id = cfg.get("battery_id", "client_battery")
    client_url = cfg.get("client_url", "http://localhost:8100")
    carbonshift_url = cfg.get("carbonshift_url", "http://localhost:8080")
    executor_url = cfg.get("executor_url", "http://localhost:9000")
    poll_interval = float(cfg.get("poll_interval_seconds", 1.0))
    poll_timeout = float(cfg.get("poll_timeout_seconds", 300.0))
    admin_timeout = float(cfg.get("admin_timeout_seconds", 60.0))

    output_dir = Path(cfg["output_dir"]) if cfg.get("output_dir") else DEFAULT_OUTPUT_DIR
    if not output_dir.is_absolute():
        output_dir = (CLIENT_ROOT / output_dir).resolve()

    start_dt = datetime.now(timezone.utc)
    run_folder = f"{battery_id}_{start_dt.strftime('%Y%m%d_%H%M%S')}"
    run_dir = output_dir / run_folder
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "battery_config.json").write_text(json.dumps(cfg, indent=2))

    rows = []
    for scenario in cfg["scenarios"]:
        scenario_dir = run_dir / scenario["id"]
        row = _run_scenario(scenario, client_url, carbonshift_url, executor_url, poll_interval, poll_timeout,
                            admin_timeout, scenario_dir)
        rows.append(row)

    with open(run_dir / "results.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SCENARIO_CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in SCENARIO_CSV_COLUMNS})

    _write_battery_readme(run_dir, battery_id, start_dt, rows)
    print(f"\nBattery complete. Results in {run_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = parser.parse_args()
    run_battery(Path(args.config))


if __name__ == "__main__":
    main()
