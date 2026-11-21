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
slot.

For `sinusoidal` scenarios, configure:

- `carbon_request_alignment`: `in-phase`, `phase-opposition`, or
  `phase-shifted` (the standalone scenario-generation default; `multiple_runs.py`
  uses its `CARBON_REQUEST_ALIGNMENT` constant).
- `carbon_phase_shift_fraction`: the shifted-wave offset as a fraction of the
  cycle, from `0.0` to `0.5`; it applies only to `phase-shifted`. The default
  `0.25` is halfway between aligned and opposed peaks.
- `request_pattern_start`: `low` (default: night demand low, daytime demand
  high) or `high` to invert the request wave.

`multiple_runs.py` configures these with the `CARBON_REQUEST_ALIGNMENT`,
`CARBON_PHASE_SHIFT_FRACTION`, and `REQUEST_PATTERN_START` constants beside
`INPUT_PATTERNS`.

## Flavour selection

For `multiple_runs.py`, edit its `FLAVOURS` list to choose the flavour
definitions shared by the baseline and every enabled phase. Comment out entries
to test a subset, such as only Accurate. Each entry uses `name`, `error`
(percent), and `duration_sec` (seconds). This list overrides the top-level
`flavours` field in `battery_config.json`. When running `run_battery.py`
directly, configure that JSON field instead; set it to `null` or remove it to
use nshift's built-in defaults.

## Ant-colony search size

Set top-level `aco_n_ants` and `aco_n_iterations` in `battery_config.json` to
choose nshift's default ACO effort (40 ants × 10 iterations by default). For
each batch, nshift caps that product against
`error_window_future × flavour_count × batch_size`. When it must scale down, it
scales both values proportionally and rounds each up. Offline whole-scenario
ACO runs use the scenario request count as their batch size. This adjustment
applies only to nshift simulations; the live service keeps its configured ACO
settings unchanged.

## Batch-worker parallelism

`max_batch_solver_parallelism` is an upper bound on concurrent online
simulation workers, not a promise that workers stay busy when there are too
few pending batches or a run is starting or draining. Online batches may be
claimed concurrently from the same QoS profile. Each worker solves from the
snapshot taken atomically with its queue claim. Commit recalculates carbon
prices using current slot occupancy, but does not revalidate the profile's
error threshold against concurrent commits; a burst can temporarily or
permanently exceed it.

Bandit and ant-colony online batches use additive state merging, so they can
use the configured worker cap without a separate concurrency-mode setting.
The live service uses the default one-worker-per-profile scheduling policy.

Offline whole-scenario strategies run synchronously and do not use this worker
pool or the REST service. `avg_concurrent_workers` is time-weighted over
intervals with at least one batch worker active, excluding no-work startup and
drain time.

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

`reqs_in` is the number of requests generated for the scenario;
`reqs_out` is the run-specific number assigned within the original
horizon. These values can differ across strategy runs. Leaving
`trim_future_assignments` absent or `false` preserves the existing behavior.

`multiple_runs.py` runs the `sinusoidal` input pattern by default. Use
`--input-pattern flat` or `--input-pattern sinusoidal flat` to choose other
patterns. It always sets each scenario's input pattern and sinusoidal wave
settings. `APPEND_SUFFIX_TO_SCENARIOS` controls whether the corresponding tags
are also added to scenario IDs; previously generated tags are normalized first.
The battery ID includes the selected pattern and wave settings regardless of
this option, so sweep outputs remain distinct. For a single battery run, set
`input_pattern`, `carbon_request_alignment`, and
`request_pattern_start` per scenario in the config; omitted values use the
defaults in `scenario_config.py`.

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
- `offline_random_slotflav` and `offline_random_flav`: reference random assignments.
  They choose a random flavour while both the cumulative and current-slot-centered
  window averages are at or below the threshold; once either is above it, they
  use the minimum-error flavour at the slot selected by that strategy's mode.
  Randomized assignments may push an average above threshold; later requests
  in that solve then use the fallback until both averages are back at or below
  threshold. `offline_random_slotflav` samples a legal slot, while
  `offline_random_flav` uses the earliest legal slot (normally arrival).
- `hazard`: samples a feasible slot with probability inversely proportional
  to forecast carbon intensity multiplied by the capacity-tier multiplier at
  that slot's next occupancy position. It samples flavours from the
  distribution closest to equal probabilities in squared Euclidean distance,
  subject to expected flavour error equalling the configured threshold when
  attainable; otherwise it targets the nearest available flavour error. For
  errors 0%, 5%, and 10% with a 7.5% threshold, the flavour probabilities are
  1/12, 1/3, and 7/12. This is an expectation over many assignments; it does
  not enforce the threshold on each run or error window. The scenario seed
  makes the hazard schedule reproducible.
- `bandit` and `ant_colony`: use the common engine kernels, with a fresh
  runtime and learned state for each run. The live scheduler and offline
  runner use the same common engine registry.

## Online strategies

Set `hazard`, `bandit`, `ant_colony`, `singleton_best`, `random_slotflav`, or
`random_flav` in
`online_strategies` to run it through the streaming scheduler. The same engine
implementations are available offline through `additional_strategies`.

The random-reference modes use the cumulative and current-slot-centered window
averages as a gate: random flavour selection is allowed at or below threshold,
and above threshold the minimum-error flavour is used without changing the
selected slot mode. A random choice may cross the threshold; subsequent
requests then use the fallback until both averages are back at or below
threshold.

The random policy names are intentionally explicit: `random_slotflav` chooses
a random slot and flavour, while `random_flav` keeps the earliest legal slot
and chooses a random flavour. Offline configuration uses the corresponding
`offline_random_slotflav` and `offline_random_flav` names. The previous
`random_slot` / `arrival_slot` and `offline_random_slot` /
`offline_arrival_slot` policy names are rejected by nshift.

`ant_colony`, `bandit`, `hazard`, `singleton_best`, `random_slotflav`, and
`random_flav` use the
`online_batch_sizes` sweep. Batch size changes when request-level strategies
observe committed feedback, so compare both runtime and scheduling quality.

Bandit exploitation blends its learned per-slot average with 25% of the
candidate's current estimated cost. That candidate cost includes its
QoS-feasible flavour, forecast intensity, and current capacity-tier position.
ACO's cost heuristic uses the same candidate-specific feasible-flavour cost
rather than assuming the globally shortest-duration flavour is feasible.
For these stateful strategies, the sliding error window is centered on
`max(arrival_slot, current_slot)`, independently of the candidate's scheduled
slot.

`fallback_assigned` and `reqs_fallback_assigned` include both coverage/greedy
fallbacks and best-effort placements made when no legal slot has a
QoS-feasible flavour. Bandit and ACO force the minimum-error flavour but still
choose its slot using their normal selection policies. It is explicitly
reported because this best-effort choice may still exceed a QoS limit.

Example:

```json
{
  "batch_sizes": [],
  "online_strategies": [],
  "additional_strategies": [
    "offline_singleton_best",
    "offline_singleton_best_accurate",
    "hazard"
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
