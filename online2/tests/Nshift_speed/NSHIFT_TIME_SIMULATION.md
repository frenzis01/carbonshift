# Nshift benchmark: time simulation with variable N

This note explains how `run_nshift_speed.py` / `simulator.py` replay the same scenario for different batch sizes (`N`).

## 1. Same inputs for all N

For each tested `N`, the runner loads the same scenario JSON:

- same request list (`request_id`, `arrival_slot`, `deadline_slot`)
- same carbon forecast by slot
- same synthetic prehistory slots (negative slots used for modeled window error)

So prehistory data are identical across all N runs.

## 2. How time advances in the simulation

The simulator (`run_single_batch_size`) uses a discrete slot loop:

1. `for slot in range(total_slots):`
2. Set scheduler current slot.
3. Inject all requests whose `arrival_slot == slot`.
4. While pending requests are at least `N`, execute solver (`_process_batch(slot)`).
5. Record one `batch_timings` row per solver execution (with `batch_sequence`, `slot`, elapsed time, etc.).

After all slots, if `flush_partial_batch=True`, remaining pending requests are flushed at the last slot with an effective batch size `<= N`.

Important: with smaller `N`, more solver runs can happen in the same slot.

## 3. Why slot 0 can differ between N values

If you plot "last solver run in slot", slot 0 is **not** "before any assignment".

It is the final solver state at slot 0:

- `N=1` can run many times at slot 0
- `N=4` runs fewer times at slot 0
- `N=10` may run once at slot 0

Because the cumulative assigned set differs at that point, real/modeled window averages at slot 0 can differ across N, even with identical prehistory.

## 4. Real vs modeled window average used in notebooks

For a run at `current_slot = t`:

- `window_start_modeled = t - ERROR_WINDOW_PAST`
- `window_start_real = max(0, window_start_modeled)`
- `window_end = min(total_slots - 1, t + ERROR_WINDOW_FUTURE)`

Real average:

- uses only real assignments with `scheduled_slot in [window_start_real, window_end]`

Modeled average:

- uses the same real assignments
- plus synthetic prehistory contributions where `prehistory_slot in [window_start_modeled, window_end]`

## 5. Notebook alignment

`nshift_assignment_analysis.ipynb` and `nshift_speed_analysis.ipynb` are aligned to the same semantics:

- compute cumulative state by solver run sequence
- derive window averages per run
- select the **last solver run per timeslot** for the per-timeslot history chart

