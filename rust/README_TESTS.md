# CarbonShift Rust — Test Instructions

## Prerequisites

- Rust toolchain (stable) — install via [rustup](https://rustup.rs)
- The scenario JSON used by the integration test lives at
  `online2/tests/Nshift_speed/scenario_seed_2030.json` (already in the repo)

All commands below are run from the `rust/` directory:

```sh
cd carbonshift/rust
```

---

## Running all tests

```sh
cargo test
```

This runs 24 unit tests + 1 integration test.  
Expected output (times will vary):

```
test result: ok. 24 passed; 0 failed; ...   ← unit tests
test result: ok. 1 passed;  0 failed; ...   ← integration test (~15 s)
```

---

## Running only unit tests

```sh
cargo test --lib
```

Runs the 24 in-module tests across `config`, `dp_solver`, `shared_state`, and `scheduler`.
These are fast (< 1 s).

### Run a specific unit test

```sh
cargo test dp_solver::tests::single_request_assigned_cheapest_slot
```

---

## Running only the integration test

```sh
cargo test --test integration_scenario
```

The test `scenario_seed_2030_all_requests_scheduled_correctly`:

1. Loads `../online2/tests/Nshift_speed/scenario_seed_2030.json`
   (72 slots, 4 380 requests, seed 2030).
2. Simulates the full scheduling loop slot-by-slot using `DpSolver::solve_batch` +
   `greedy_fallback`.
3. Asserts:
   - All 4 380 requests are assigned.
   - No request is scheduled before its `arrival_slot`.
   - No request is scheduled after its `deadline_slot`.

Running time ≈ 15 s (single-threaded DP over 72 slots × ~60 req/slot).

---

## Running the application

```sh
cargo run
```

Starts the full `Online2System`: request generator → batch scheduler → metrics logger.  
Press **Ctrl-C** to stop; final statistics are printed on exit.

Metrics CSV files are written to the current directory:
- `solver_runs.csv` — one row per solved batch
- `infeasible_debug.csv` — rows for infeasible-recovery events

---

## Python tests (reference implementation)

From the repo root:

```sh
pip install -r requirements.txt      # first time only
pytest online2/tests/ -v
```

Expected: 25 tests pass.

---

## Test coverage summary

| Module | Test location | Count |
|---|---|---|
| `config` | `src/config.rs` | 2 |
| `dp_solver` | `src/dp_solver.rs` | 7 |
| `shared_state` | `src/shared_state.rs` | 8 |
| `scheduler` | `src/scheduler.rs` | 7 |
| Integration | `tests/integration_scenario.rs` | 1 |
| **Total** | | **25** |
