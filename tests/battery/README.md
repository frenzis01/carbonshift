# Simulation battery

The simulation battery generates deterministic scenarios and runs the Rust
`nshift` benchmark against them. Its request patterns and offline strategies
are separate from the client/emulation application; changing these options
does not alter emulation plans or live service behavior.

## Request input patterns

Set `input_pattern` on a scenario in `battery_config.json` to:

- `sinusoidal` (default): request counts follow the configured daylight-shaped
  request-rate curve, with the existing per-slot count variation.
- `flat`: spreads the rounded total request count as evenly as possible over
  all slots. If the requested rate is fractional, slot counts differ by at
  most one so the overall rate is preserved.

Both patterns retain independently seeded random arrival times within each
slot. Carbon-intensity generation is unchanged by this setting.

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
python -m pytest -q tests/battery/test_scenario_patterns.py
```
