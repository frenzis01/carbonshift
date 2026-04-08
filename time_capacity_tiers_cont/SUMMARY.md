# Time-Capacity Tiers Contiguous - Summary

## Overview
Extension of the basic capacity tiers model that allows requests to span multiple contiguous time slots, with time-based capacity constraints and strategy-specific execution durations.

## Key Features
1. **Contiguous Execution**: Requests can span multiple consecutive slots
2. **Time-Based Capacity**: Slots limited by total execution time (minutes)
3. **Parallelism**: Configurable degree of parallel execution
4. **Strategy Duration**: Each strategy has a specific execution time
5. **Capacity Tiers** (optional): Emission multipliers based on load

## Schedulers Implemented

### Heuristics (Fast, ~0.001s)
1. **Greedy**: Best cost-per-request with lookahead
   - Often near-optimal
   - Good baseline

2. **First-Fit**: Earliest feasible slot
   - Very fast
   - Tends to frontload (higher cost)

3. **Best-Fit**: Tightest capacity match
   - Optimizes packing
   - May sacrifice carbon efficiency

4. **Probabilistic Slack**: Slack-based postponement
   - Best error performance
   - Higher cost due to conservative scheduling

### Dynamic Programming (Optimal, ~0.07-2s)
1. **DP + K-Best Pruning**
   - Fast approximation (0.07s)
   - 97% optimal quality
   - Explores ~14K states

2. **DP + Beam Search**
   - High-quality solution (1.8s)
   - Optimal or near-optimal
   - Explores ~250K states

## Performance Comparison

```
Scheduler          Cost      Error    Gap      Speed     Quality
Greedy             7,900     4.84%    18.5%    ⚡⚡⚡⚡⚡    Good
First-Fit         12,103     4.84%    81.5%    ⚡⚡⚡⚡⚡    Low
Best-Fit          18,630     2.39%   179.4%    ⚡⚡⚡⚡⚡    Low
ProbSlack         23,800     1.00%   257.0%    ⚡⚡⚡⚡⚡    Low cost, best error
DP+KBest           6,853     4.90%     2.8%    ⚡⚡⚡⚡     Very Good
DP+Beam            6,667     4.84%     0.0%    ⚡⚡⚡      Optimal
```

## When to Use Each Scheduler

- **Greedy**: Default choice for online/real-time scheduling
- **DP + K-Best**: When you have 0.1s budget and want near-optimal
- **DP + Beam**: Offline planning, batch optimization, benchmarking
- **First-Fit**: Emergency/fallback, extreme speed requirements
- **Best-Fit**: When packing efficiency matters more than carbon
- **ProbSlack**: When minimizing error is priority #1

## Usage

```bash
# Run comparison
cd tests
python comparison.py

# Individual schedulers
python greedy_cont.py <args>
python dp_cont.py <args> --pruning kbest --pruning-k 150
```

## Files

### Core Schedulers
- `greedy_cont.py` (256 lines) - Greedy heuristic
- `first_fit_cont.py` (237 lines) - First-fit heuristic
- `best_fit_cont.py` (243 lines) - Best-fit heuristic
- `probabilistic_slack_cont.py` (291 lines) - Probabilistic slack
- `dp_cont.py` (311 lines) - DP with K-Best and Beam pruning

### Utilities
- `utils_cont.py` (293 lines) - Common functions
  - `try_assign_contiguous()` - Simulate contiguous assignment
  - `validate_solution()` - Check feasibility
  - `calculate_initial_residuals()` - Setup

### Tests
- `tests/comparison.py` (248 lines) - **Main comparison test**
- `tests/comparison_heuristics_only.py` (248 lines) - Heuristics only

### Documentation
- `README.md` - Full documentation
- `SUMMARY.md` - This file
- `CHANGELOG.md` - Version history
- `DESIGN_CONTIGUOUS.md` - Design decisions
- `DISCRETE_VS_CONTIGUOUS.md` - Model comparison

## Technical Details

### State Space
- **DP State**: `D[block][(error, residual_times_tuple)] = cost`
- **Discretization**: 5-minute granules to manage state explosion
- **Complexity**: O(Blocks × Errors × Residual_configs)

### Pruning Strategies
- **K-Best**: Keep top-K states globally (breadth-first)
- **Beam**: Keep top-K states per error level (diversity)
- **Warm-start**: Use greedy cost as upper bound

## Validation
All solutions validated for:
- ✅ Capacity constraints (no slot overload)
- ✅ Deadline constraints (all blocks meet deadlines)
- ✅ Error threshold (average error ≤ threshold)
- ✅ Contiguity (requests span consecutive slots)
- ✅ Cost calculation (matches theoretical formula)

## Future Work
1. Adaptive granularity (coarser for early blocks, finer for late)
2. Hybrid approaches (greedy + local DP refinement)
3. Multi-objective optimization (cost vs error Pareto front)
4. Integration with online scheduler
5. Real workload evaluation
