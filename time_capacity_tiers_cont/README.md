# Time-Capacity Tiers with Contiguous Execution

Extension of capacity tiers model with:
- **Contiguous execution**: Requests can span multiple consecutive time slots
- **Time-based capacity**: Slots limited by execution time, not just request count
- **Strategy duration**: Each strategy has an estimated execution duration

## Problem Definition

Unlike the basic capacity tiers model where requests fit entirely in one slot, here:
- A request may take longer than one slot duration
- Execution continues seamlessly into the next slot
- Carbon cost is the weighted sum across all slots used

### Parameters

- **Blocks**: Groups of requests with deadlines
- **Strategies**: Processing options with (error%, duration_minutes)
- **Time Slots**: Fixed duration windows (e.g., 30 min each)
- **Parallelism**: Degree of parallel execution (multiplies slot capacity)
- **Capacity Tiers**: Optional emission multipliers based on load

### Example

```
Slot duration: 30 min
Parallelism: 3
Slot capacity: 30 × 3 = 90 min

Request with strategy duration = 120 min:
- Uses 90 min in slot 5
- Uses 30 min in slot 6
- Total cost = carbon[5] × 90 + carbon[6] × 30
```

## Schedulers

### Heuristics (Fast)

1. **Greedy**: Best cost-per-request choice with lookahead
2. **First-Fit**: Earliest feasible slot
3. **Best-Fit**: Tightest capacity match
4. **Probabilistic Slack**: Slack-based postponement decisions

### Dynamic Programming (Optimal/Near-Optimal)

- **DP + K-Best Pruning**: Keep top-K states globally
- **DP + Beam Search**: Keep top-K states per error level

DP uses time discretization (5-min granules) to manage state space.

## Files

- `greedy_cont.py`: Greedy scheduler
- `first_fit_cont.py`: First-fit heuristic
- `best_fit_cont.py`: Best-fit heuristic
- `probabilistic_slack_cont.py`: Probabilistic slack heuristic
- `dp_cont.py`: DP with pruning (K-Best, Beam)
- `utils_cont.py`: Common utilities
- `tests/comparison.py`: Comprehensive scheduler comparison

## Usage

```bash
# Run comparison
cd tests
python comparison.py

# Individual scheduler examples in each file
python greedy_cont.py <blocks> <strategies> <carbon> <tiers> <error_threshold> <slot_duration> <parallelism>
python dp_cont.py <blocks> <strategies> <carbon> <tiers> <error_threshold> <slot_duration> <parallelism> \
    --pruning kbest --pruning-k 150 --granularity 5
```

## Key Differences from Basic Capacity Tiers

| Aspect | Basic Model | Contiguous Model |
|--------|-------------|------------------|
| Request fitting | Whole request in one slot | Can span multiple slots |
| Capacity | Number of requests | Total execution time |
| Strategy parameter | Only error | Error + duration |
| Complexity | Lower | Higher (discretization needed) |

## Performance Notes

- **Greedy**: ~0.001s, often near-optimal
- **DP + K-Best**: ~0.07s, 2-15% better than Greedy
- **DP + Beam**: ~2s, finds optimal or near-optimal
- State space grows exponentially with slots/parallelism
- Discretization granularity trades accuracy vs speed
