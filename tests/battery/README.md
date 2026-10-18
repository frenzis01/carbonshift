# Simulation battery

The simulation battery generates deterministic scenarios and runs the Rust
`nshift` benchmark against them. Its request patterns and offline strategies
are separate from the client/emulation application; changing these options
does not alter emulation plans or live service behavior.

## Request input patterns

Set `input_pattern` on a scenario in `battery_config.json` to:

- `sinusoidal` (default): request counts follow the configured daylight-shaped
  request-rate curve, with the existing per-slot count variation.
- `flat`: keeps the request rate nearly constant, with deterministic Gaussian
  slot-count jitter (standard deviation 5% of the requested rate). Counts are
  rebalanced so the rounded total over the horizon is preserved.

Both patterns retain independently seeded random arrival times within each
slot. Carbon-intensity generation is unchanged by this setting.

## Future-slot assignment trimming

Set the top-level `trim_future_assignments` field in `battery_config.json` to
`true` to reduce end-of-simulation crowding without changing CarbonShift. For a
scenario with `total_slots: 72`, the runner generates requests only in slots
0–71, but generates the carbon forecast and planning horizon through slot 85
(72 + `ERROR_WINDOW_FUTURE` - 1; the current value is 14). Request deadlines
may extend into these extra slots.

After each `nshift` run, the battery runner keeps only assignments scheduled in
the original `[0, total_slots)` horizon. It also trims per-request and
per-timeslot outputs and recomputes request-level summary metrics, including
carbon cost, error, and carbon saving, from retained assignments. The baseline
carbon cost is restricted to the same retained request IDs. Solver runtime and
other execution-level measurements still describe the full extended run.

`requests_in` is the number of requests generated for the scenario;
`requests_out` is the run-specific number assigned within the original
horizon. These values can differ across strategy runs. Leaving
`trim_future_assignments` absent or `false` preserves the existing behavior.

`multiple_runs.py` runs each requested phase for both `sinusoidal` and `flat`
by default. Use `--input-pattern flat` or
`--input-pattern sinusoidal flat` to choose a subset. It adds the pattern to
scenario and battery IDs so results from different patterns do not overwrite
one another. For a single battery run, set `input_pattern` per scenario in
the config; omitting it keeps the `sinusoidal` default.

If you interrupt `multiple_runs.py` with Ctrl+C, it removes the newly created
folder for the active run if that run is incomplete. Earlier completed runs,
and any run whose final result files were already written, are preserved.

## Offline strategies

Set `additional_strategies` in `battery_config.json` to run offline engines
once per scenario:

- `offline_singleton_best`: for each request in arrival order, scans feasible
  slot/flavour pairs and commits the least-carbon-cost feasible choice. This
  is exhaustive over that request's candidates, not over complete schedules.
- `offline_singleton_best_accurate`: uses the lowest-error flavour only, then scans feasible
  slots for the least-carbon-cost assignment.
- `random`: selects a feasible slot uniformly at random and samples flavours
  from the distribution closest to equal probabilities in squared Euclidean
  distance, subject to expected flavour error equalling the configured
  threshold. For errors 0%, 5%, and 10% with a 7.5% threshold, the
  probabilities are 1/12, 1/3, and 7/12. This is an expectation over many
  assignments; it does not enforce the threshold on each run or error window.
  The scenario seed makes the random schedule reproducible. A target outside
  the available flavour-error range cannot be achieved and is reported as an
  error.
- `bandit` and `ant_colony`: existing offline variants, independent of their
  online scheduler counterparts.

Example:

```json
{
  "batch_sizes": [],
  "infeasibility_modes": [],
  "online_strategies": [],
  "additional_strategies": [
    "offline_singleton_best",
    "offline_singleton_best_accurate",
    "random"
  ],
  "scenarios": [
    {
      "id": "weekday_flat",
      "seed": 42,
      "requests_per_slot": 60,
      "total_slots": 72,
      "input_pattern": "flat"
    }
  ]
}
```

Run a pattern sweep from the repository root:

```sh
python tests/battery/multiple_runs.py --phase offline
```

Run the scenario-pattern and battery-runner unit tests with:

```sh
python -m pytest -q tests/battery/test_scenario_patterns.py tests/battery/test_run_battery.py
```

## Assignment display notebook

`display_assignment.ipynb` provides a searchable run-folder picker. Install its
optional widget dependency from this directory with:

```sh
python -m pip install -r requirements-notebook-widgets.txt
```

Run the folder-picker cell, type part of a battery run folder name, select a
suggestion, and click **Use folder**. Then run the following cell to choose an
assignment result set within that folder. **All folders** includes legacy flat
results.
