# CarbonShift Rust Port — Implementation Plan

## Goal
Port the `online2/` Python batch scheduler to Rust, focusing on correctness, safe
multi-threading, and performance for the CPU-bound DP solver.

## Architecture mapping

| Python component | Rust equivalent |
|---|---|
| `config.py` (module globals) | `config.rs` — `Config` struct + `Config::default()` |
| `shared_state.py` — `Request`, `Assignment` | `types.rs` |
| `shared_state.py` — `SharedSchedulerState` | `shared_state.rs` — `Arc<Mutex<SharedState>>` |
| `rolling_window_dp.py` — `RollingWindowDPScheduler` | `dp_solver.rs` |
| `scheduler.py` — `BatchScheduler` | `scheduler.rs` |
| `request_generator.py` — `RequestGenerator` | `generator.rs` |
| `metrics_logger.py` — `SolverMetricsLogger` | `metrics_logger.rs` |
| `main.py` | `main.rs` |

## Concurrency model

- **SharedState**: `Arc<Mutex<SharedState>>` — single lock, short critical sections.
- **Batch workers**: `std::thread::spawn` per batch; count tracked with `Arc<Mutex<usize>>`.
- **Parallelism cap**: Semaphore pattern via `Arc<(Mutex<usize>, Condvar)>`.
- **DP solver**: purely functional per-batch; no shared mutable state inside solve.
- **Rayon (optional Phase 3 extension)**: parallelize state expansion within a DP layer.

## Phases

### Phase 1 — Project scaffold + types + config ✅
- [x] `cargo init` + `Cargo.toml` with all dependencies
- [x] `src/types.rs` — `Request`, `Assignment`, `RequestAssignment`, `Flavour`, `CapacityTier`
- [x] `src/config.rs` — `Config` struct with default values matching `config.py`
- [x] Wire up `main.rs` (stub)

### Phase 2 — SharedSchedulerState ✅
- [x] `src/shared_state.rs` — `SharedState` struct
  - [x] `add_request` / `claim_pending_requests` / `requeue_front`
  - [x] `add_assignments` (update global error totals)
  - [x] `get_window_error_stats` / `get_global_error_stats`
  - [x] `get_future_assignments` / `get_current_assignments`
  - [x] `set_current_slot` / `get_pending_count`
  - [x] `export_to_csv`
- [x] Unit tests

### Phase 3 — DP Solver ✅
- [x] `src/dp_solver.rs` — `DpSolver` struct
  - [x] `DpState` key type implementing `Hash + Eq`
  - [x] `solve_batch()` — DP expansion loop + beam/kbest pruning
  - [x] `_incremental_carbon_cost()` with capacity-tier repricing
  - [x] `_greedy_fallback()`
  - [x] Timeout check (≥ DP_TIMEOUT seconds → fall back to greedy)
- [x] Unit tests: capacity repricing, feasibility filter, beam pruning

### Phase 4 — Batch Scheduler ✅
- [x] `src/scheduler.rs` — `BatchScheduler` struct
  - [x] `start()` / `stop()` — spawn main loop thread
  - [x] `dispatch_batch_workers_for_slot()` with parallelism cap + anti-storm guard
  - [x] `solve_dp()` pipeline (steps 2–6 from Python)
  - [x] `augment_with_decayed_past()` / `augment_with_virtual_prehistory()`
  - [x] `apply_infeasibility_recovery()` (mock pool management)
  - [x] `solve_relaxed_retry()`
  - [x] `build_assignment_rows()`
  - [x] `get_statistics()`

### Phase 5 — Request Generator + Main ✅
- [x] `src/generator.rs` — `RequestGenerator`
  - [x] Gaussian arrival rate: `rand_distr::Normal`
  - [x] Deadline slack: uniform `[DEADLINE_MIN_SLACK, DEADLINE_MAX_SLACK]`
  - [x] `start()` / `stop()`
- [x] `src/main.rs` — wire generator + scheduler + metrics logger
  - [x] `Online2System` orchestrator
  - [x] Ctrl-C signal handling (`ctrlc` crate)
  - [x] Final statistics print

### Phase 6 — Metrics Logger ✅
- [x] `src/metrics_logger.rs` — `MetricsLogger` struct
  - [x] `log_solver_run()` / `log_infeasible_debug()`
  - [x] Thread-safe CSV append (Mutex-guarded write)
  - [x] Header consistency check (backup on mismatch)

### Phase 7 — Integration tests ✅
- [x] `src/lib.rs` — re-exports all modules for integration test access
- [x] `src/main.rs` — updated to import via `carbonshift_rs::` (lib crate)
- [x] Scheduler unit tests (7) added to `scheduler.rs` `#[cfg(test)]` block
- [x] `tests/integration_scenario.rs` — full slot-by-slot scenario test
  - Loads `scenario_seed_2030.json` (72 slots, 4380 requests)
  - Asserts all requests scheduled, no arrival/deadline violations

## Current status
- **Phase 1**: ✅ complete
- **Phase 2**: ✅ complete
- **Phase 3**: ✅ complete
- **Phase 4**: ✅ complete
- **Phase 5**: ✅ complete
- **Phase 6**: ✅ complete
- **Phase 7**: ✅ complete (25 tests: 24 unit + 1 integration — all pass)
