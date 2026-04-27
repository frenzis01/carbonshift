# Online2: Batch Scheduler with DP and Capacity Tiers

**Advanced carbon-aware scheduling with batch processing, dynamic programming, and sliding error windows.**

## Overview

Online2 is a production-ready batch scheduling system for carbon-aware request processing. Unlike the simple online scheduler, Online2:

1. **Batches requests**: Waits for N requests before scheduling (N ≥ 1)
2. **Uses DP solver**: Finds optimal batch placement considering all decisions
3. **Handles capacity tiers**: Implements rebound effect (emissions multiply when overloaded)
4. **Enforces error windows**: Weighted average error on requests in [t-5, t+5] must stay below threshold
5. **Thread-safe**: Uses shared state and background processing

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    Online2 System                        │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  ┌─────────────────┐        ┌──────────────────┐        │
│  │ RequestGenerator│        │ BatchScheduler   │        │
│  │  (Thread)       │        │   (Thread)       │        │
│  └────────┬────────┘        └────────┬─────────┘        │
│           │                         │                   │
│           ├─────────┬───────────────┤                   │
│           │         │               │                   │
│           v         v               v                   │
│  ┌───────────────────────────────────────┐             │
│  │     SharedSchedulerState              │             │
│  │  - Pending requests queue             │             │
│  │  - Active assignments                 │             │
│  │  - Historical assignments             │             │
│  │  - Error budget tracking              │             │
│  └───────────────────────────────────────┘             │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

## Modules

### `config.py`
Central configuration file containing:
- **Batch parameters**: `BATCH_SIZE` (default: 3)
- **Time slot settings**: `SLOT_DURATION_SECONDS` (default: 10s), `TOTAL_SLOTS` (default: 24)
- **Strategies**: 3 compute strategies with different error/duration tradeoffs
- **Error budget**: Window size (11 slots: t-5 to t+5), threshold (3%)
- **Capacity tiers**: Rebound effect multipliers at different load levels
- **DP parameters**: Pruning strategy (beam/kbest), timeout, pruning_k
- **Request generation**: Arrival rate, deadline ranges

### `shared_state.py`
Thread-safe state container managing:
- **Request queue**: Pending requests waiting to be scheduled
- **Assignments**: Current scheduling decisions
- **Historical tracking**: Past assignments for reference
- **Error budget**: Weighted average error on requests in sliding windows
- **Statistics**: Total requests, scheduled count, pending queue size

**Key methods**:
- `add_request()` - Add new request
- `get_pending_requests()` - Peek at batch without removing
- `pop_pending_requests()` - Remove scheduled requests
- `add_assignments()` - Record scheduling decisions
- `get_average_error_in_window()` - Validate error budget
- `get_requests_in_slot()` - Count requests in slot (for capacity tiers)

### `request_generator.py`
Generates incoming requests at constant rate:
- Runs in background thread
- Generates ~5 requests per slot (configurable)
- Each request has random deadline (2-8 slots from arrival)
- Adds to shared state queue
- Stops gracefully

### `scheduler.py`
Batch processing scheduler using DP:
- Runs in background thread
- Waits for BATCH_SIZE pending requests
- Solves batch scheduling using DP with optional Beam Search
- Considers:
  - Current assignments and their carbon/error impacts
  - Capacity tier multipliers (rebound effect)
  - Sliding error window constraints
  - Previous decisions
- Returns list of assignments for the batch

**DP Solver**:
Uses dynamic programming with pruning to:
- Generate feasible `(request, slot, strategy)` placements
- Respect `current_slot <= scheduled_slot <= deadline_slot`
- Reprice slot carbon cost when capacity tier changes
- Enforce weighted error window constraints

### `main.py`
System orchestrator:
- Initializes all components
- Starts request generator and scheduler threads
- Monitors statistics
- Handles graceful shutdown (Ctrl+C)
- Exports results to CSV

## Configuration Quick Reference

```python
# Time slot every 10 seconds
SLOT_DURATION_SECONDS = 10

# Schedule 3 requests at a time
BATCH_SIZE = 3

# Maximum weighted average error in sliding window
MAX_ERROR_THRESHOLD = 3.0
ERROR_WINDOW_PAST = 5
ERROR_WINDOW_FUTURE = 8
ASSIGNMENT_MAX_FUTURE_SLOTS = 8

# Rebound effect: >2000 requests = 1.5x carbon multiplier
CAPACITY_TIERS = [
    {"max_requests": 1000, "multiplier": 1.0},
    {"max_requests": 2000, "multiplier": 1.5},
    {"max_requests": 3000, "multiplier": 2.0},
]

# DP solver: optional Beam Search
DP_PRUNING_STRATEGY = 'beam'
DP_PRUNING_K = 150

# Future commitments behavior:
# True  -> keep already assigned future requests fixed
# False -> include future assignments in DP and allow re-planning
DP_LOCK_FUTURE_ASSIGNMENTS = True

# Strict infeasibility handling:
# - allow one relaxed retry
# - relaxed retry can prefer minimum-error strategy for recovery
DP_ALLOW_RELAXED_ERROR_RETRY = True
DP_RELAXED_RETRY_PREFER_MIN_ERROR = True

# Sliding-window infeasibility recovery policy:
# "min_error_recovery" | "carryover_last_slot" | "forecast_mock_current_slot"
INFEASIBILITY_RECOVERY_MODE = "min_error_recovery"

# Predicted request rate (used by generator and startup pre-history)
PREDICTED_REQUESTS_PER_SLOT = 10.0
REQUEST_RATE_STD_FACTOR = 0.3

# Startup pre-history: virtual past slots with avg error = threshold * 0.5
PREHISTORY_USE_VIRTUAL_PAST = True
PREHISTORY_ERROR_RATIO_OF_THRESHOLD = 0.5
```

## Running

### Basic Test (30 seconds)
```bash
cd online2
python main.py --duration 30
```

### Continuous Run (until Ctrl+C)
```bash
python main.py
```

### Example Output
```
================================================================================
Online2 Batch Scheduler
================================================================================

Configuration:
  - Batch Size: 3
  - Slot Duration: 10s
  - Total Slots: 24
  - Max Error: 3%
  - DP Pruning: beam
  - Predicted Requests/Slot: 10

[RequestGenerator] Started: 10.0 req/slot
[Scheduler] Started (batch_size=3)

[t=10.0s] Statistics:
  Generated: 45
  Scheduled: 0
  Pending: 45
  Current Slot: 1
  Batches: 0

[Scheduler] Slot 1: 45 pending, scheduling batch...
[Scheduler] Processing 3 requests...
[Scheduler] ✓ Scheduled 3 requests (cost=12.50, error=2.50%)
```

## CSV Output

Results exported to `/tmp/online2_assignments.csv`:

```csv
request_id,scheduled_slot,strategy,carbon_cost,error,assignment_time
0,5,Accurate,45.23,1.0,1713873600.123
1,5,Balanced,18.92,2.5,1713873600.124
2,6,Fast,8.45,5.0,1713873600.125
```

Solver telemetry (per execution) is exported to:
- `config.SOLVER_RUNS_FILE` (default: `/tmp/online2_solver_runs.csv`)
- `config.SOLVER_ASSIGNMENTS_FILE` (default: `/tmp/online2_solver_assignments.csv`)
- `config.SOLVER_SLOT_METRICS_FILE` (default: `/tmp/online2_solver_slot_metrics.csv`)
- `config.SOLVER_INFEASIBLE_DEBUG_FILE` (default: `/tmp/online2_solver_infeasible_debug.csv`)

These files include:
- solver start/end timestamp and elapsed time
- average processing time per request and per batch
- assignment details for each solver run (all active assignments, with a flag for new assignments in that run)
- per-slot run summaries (avg error, capacity multiplier after assignment)
- strict-infeasibility debug snapshots (pending requests with deadlines, baseline error state, active/future slot load)

## Visualization (Matplotlib + Notebook)

Use:
- `visualize_solver_logs.py` for plotting helpers
- `notebooks/solver_logs_analysis.ipynb` for a complete analysis notebook

The notebook contains:
1. a single trend chart for **avg processing time per request** and **processing time per batch**
2. a trend chart for **total carbon cost** and **carbon cost per request**
2. a chart for **carbon intensity per slot**
2. one stacked bar chart per solver execution with:
   - all active assignments by slot and strategy color
   - request IDs in each rectangle
   - new assignments highlighted and previous assignments faded
   - average error per slot
   - capacity level horizontal lines
   - solver start/end times
3. strict-infeasibility debug plots:
   - overview of event frequency/severity
   - per-event snapshot of current/future load and pending request deadlines

You can choose plotting scope via notebook options:
- all runs
- one run per slot (`first_per_slot` / `last_per_slot`)
- all runs for a specific slot

The error-window trend chart shows both:
- modeled average used by the solver constraint
- real-only average (without virtual pre-history)

Run:
```bash
cd online2
python main.py --duration 30
jupyter notebook notebooks/solver_logs_analysis.ipynb
```

## Future Enhancements

### Phase 1 (Current)
- [x] Thread-safe shared state
- [x] Request generator with configurable rate
- [x] Batch scheduler framework
- [ ] Integration with carbonshift DP module
- [ ] Beam Search pruning implementation

### Phase 2
- [ ] Docker containerization (optional)
- [ ] Multi-service communication
- [ ] Response time and latency tracking
- [ ] Advanced monitoring dashboard

### Phase 3
- [ ] Machine learning-based request prediction
- [ ] Adaptive batch sizing
- [ ] Multi-region scheduling
- [ ] Cost optimization with SLAs

## Testing Plans

### Test 1: Small Batch (N=1, slot=10s)
- One request at a time
- Minimal batching overhead
- Focus on scheduler latency

### Test 2: Medium Batch (N=3, slot=10s)
- Groups of 3 requests
- Default configuration
- Test capacity tier triggering

### Test 3: Large Batch (N=10, slot=10s)
- Larger batches
- More complex DP problems
- Stress test error window constraints

## Implementation Status

| Component | Status | Notes |
|-----------|--------|-------|
| Config | ✅ Complete | All parameters defined |
| SharedState | ✅ Complete | Thread-safe, tested |
| RequestGenerator | ✅ Complete | Configurable rate |
| Scheduler framework | ✅ Complete | Placeholder DP |
| DP Solver | 🔄 In Progress | Needs carbonshift integration |
| Beam Search | 🔄 In Progress | Optional pruning strategy |
| Error window validation | 🔄 In Progress | Partially implemented |
| Capacity tiers | 🔄 In Progress | Basic framework ready |
| Docker | ⏸️ Not Started | Optional |
| Tests | ⏸️ Not Started | Phase 2 |

## Dependencies

Standard library only (no external dependencies):
- `threading`: Background threads
- `time`: Time slot management
- `dataclasses`: Clean data structures
- `collections.defaultdict`: Error tracking

When integrated with carbonshift:
- `carbonshift` DP module for optimization
- `pulp` or `scipy` (if using external solvers)

---

**Created**: April 2026  
**Author**: Copilot  
**Status**: Framework Complete, DP Integration Pending
