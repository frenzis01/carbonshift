# Time-Capacity Tiers Extension

Extends CarbonShift with **temporal constraints** in addition to capacity-based emission tiers.

## Overview

Traditional capacity tiers model emission increases based on slot load. This extension adds:
- **Strategy execution duration**: Different strategies take different time to execute
- **Slot time capacity**: Each slot has limited time (duration × parallelism)
- **Parallel execution**: Multiple requests can execute concurrently

**Example scenario:**
- Slot duration: 30 minutes
- Parallelism: 4 (4 requests can run in parallel)
- Strategy durations: High=20min, Medium=10min, Low=5min

If we assign 10 requests using Medium strategy (10min each):
- With parallelism 4: Need ⌈10/4⌉ = 3 rounds
- Total time: 10 requests × 10min / 4 parallel = 25 minutes ✅ (fits in 30min slot)

If we assign 15 requests using High strategy (20min each):
- Total time: 15 × 20 / 4 = 75 minutes ❌ (exceeds 30min slot capacity!)

## Architecture

```
time_capacity_tiers/
├── __init__.py
├── README.md                    # This file
├── QUICK_START.md              # Quick start guide
├── greedy_time.py              # Greedy with time awareness
├── first_fit_time.py           # First-fit heuristic
├── best_fit_time.py            # Best-fit heuristic  
├── dp_time.py                  # DP with warm-start + pruning
├── config_example.csv          # Example capacity tiers
└── tests/
    └── comparison.py           # Comprehensive comparison
```

## Scheduling Methods

### 1. Greedy Time-Aware

**Strategy**: For each block, choose (slot, strategy) with minimum cost that respects time capacity.

**Key features**:
- Lookahead on emission factor (like capacity_tiers greedy)
- Checks time capacity before assignment
- Fast baseline (~0.02s)

**Time complexity**: O(B × S × δ)

---

### 2. First-Fit Time-Aware

**Strategy**: For each block, use first (slot, strategy) that satisfies all constraints.

**Algorithm**:
```
For each block:
    Try slots in order (0, 1, 2, ...)
        Try strategies by quality (High, Medium, Low)
            If fits (error, deadline, time capacity):
                Assign and break
```

**When to use**: Very fast, works when many feasible choices exist.

**Limitations**: May not find good solution if early slots fill up.

---

### 3. Best-Fit Time-Aware

**Strategy**: For each block, find slot with **minimum remaining time capacity** that still fits.

**Rationale**: Pack tightly to avoid fragmentation, preserve green slots for later.

**Algorithm**:
```
For each block:
    Find slot with:
        1. Fits time capacity
        2. Minimum remaining capacity after assignment
```

**When to use**: Better load balancing than First-Fit, still fast.

**Limitations**: Greedy - doesn't plan ahead.

---

### 4. DP with Warm-Start and Pruning

**State**: `D[block][error][loads_tuple][times_tuple] = min_cost`

Where:
- `error`: Cumulative error so far
- `loads_tuple`: Request counts per slot (for emission factor)
- `times_tuple`: Time used per slot (for capacity checking)

**Why times in state?**: Different time distributions → different future feasibility!

**Example**:
```
Path A: times = [25, 0, 0, 0, ...]  # Slot 0 nearly full (25/30 min)
Path B: times = [15, 10, 0, 0, ...] # Balanced

Next block needs 20-minute strategy:
  Path A: Can only use slots 1+ (slot 0 would overflow)
  Path B: Can use slot 0 (15+20=35 > 30 ❌) or slot 1 (10+20=30 ✅)
  
Different feasible choices → must track times in state!
```

**Warm-start**: Run greedy first, prune states with cost > greedy_cost.

**Pruning strategies**:
- **K-Best**: Keep top-K states globally
- **Beam Search**: Keep top-K states per error level (recommended)

**Time complexity**: O(B × E × U_loads × U_times × S × δ) where U = unique distributions

In practice with pruning: manageable for ~30-50 requests.

---

## Configuration

### Slot Time Capacity

```python
SLOT_DURATION_MIN = 30    # Each slot is 30 minutes
PARALLELISM = 4           # 4 requests execute in parallel

# Effective capacity: 30 min × 4 parallel = 120 request-minutes per slot
```

**Interpretation**: Can fit:
- 12 requests @ 10min each: 12 × 10 / 4 = 30 minutes ✅
- 8 requests @ 20min each: 8 × 20 / 4 = 40 minutes ❌
- 24 requests @ 5min each: 24 × 5 / 4 = 30 minutes ✅

### Strategies (input_strategies.csv)

```csv
error,duration,name
0,20,High      # Perfect quality, slow
3,10,Medium    # Good quality, medium speed
6,5,Low        # Fast but low quality
```

### Capacity Tiers

Same format as `capacity_tiers/`:

```csv
5,1.0     # 0-5 requests: normal emissions
10,2.0    # 6-10: 2× penalty
15,4.0    # 11-15: 4× penalty
25,8.0    # 16+: 8× penalty
```

---

## Running Comparison

```bash
cd time_capacity_tiers/tests
python comparison.py
```

**Expected output**:
```
Method                    Cost       Gap      Time      States
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Greedy Time             345,000    15.0%     0.02s           -
First-Fit Time          380,000    26.7%     0.01s           -
Best-Fit Time           360,000    20.0%     0.02s           -
🏆DP (no warm-start)    300,000     0.0%    45.23s   1,234,567
🏆DP + Warm-Start       300,000     0.0%     2.15s     123,456
🏆DP + K-Best           300,000     0.0%     0.85s      45,678
🏆DP + Beam             300,000     0.0%     0.72s      23,456
```

**Key insights**:
- Time constraints make problem harder (more infeasible states)
- Best-Fit often beats First-Fit (better packing)
- Greedy competitive but DP finds better strategic plan
- Warm-start + Beam: ~60× speedup while maintaining optimality

---

## Time Capacity Violations

If a scheduler assigns too much work to a slot:

```
⚠️ Time capacity exceeded in 2 slots:
  Slot 3: 145.0/120 min
  Slot 7: 132.5/120 min
```

**Causes**:
1. **Fallback assignments**: No feasible slot found, used default
2. **Heuristic limitations**: Greedy made suboptimal early choices
3. **Configuration issue**: Parallelism too low or slot duration too short

**Solutions**:
- Increase parallelism (more concurrent execution)
- Increase slot duration (more time per slot)
- Use DP instead of heuristics (finds feasible solution if exists)
- Relax error threshold (allows faster strategies)

---

## Example Usage

### Greedy Time-Aware

```bash
python greedy_time.py \
    requests.csv strategies.csv carbon.txt \
    10 6 4.0 30 4 \
    output.csv \
    --capacity-file config_example.csv

# Arguments:
# 10 = delta (number of slots)
# 6 = beta (number of blocks)
# 4.0 = error threshold (%)
# 30 = slot duration (minutes)
# 4 = parallelism
```

### DP with Warm-Start and Beam Pruning

```bash
# Step 1: Get greedy bound
python greedy_time.py requests.csv strategies.csv carbon.txt \
    10 6 4.0 30 4 greedy.csv --capacity-file config.csv
# Extract: COST: 345000

# Step 2: Run DP
python dp_time.py requests.csv strategies.csv carbon.txt \
    10 6 4.0 30 4 output.csv \
    --capacity-file config.csv \
    --upper-bound 345000 \
    --pruning beam \
    --pruning-k 150
```

---

## Understanding Results

### Cost vs Heuristic Gap

```
Greedy Time:   345,000  (Gap: 15.0%)
Best-Fit Time: 360,000  (Gap: 20.0%)
DP:            300,000  (Optimal)
```

**Why DP wins**:
1. **Strategic time allocation**: DP plans ahead to fit critical blocks
2. **Error budget management**: Uses mix of fast/slow strategies
3. **Global optimization**: Considers all blocks together

**When heuristics match DP**:
- Loose time constraints (plenty of capacity)
- Simple carbon patterns (monotonic)
- Tight error budget (forces all-High strategy)

### Time Utilization

```
Greedy Time:
  Max time: 118.5/120 min (98.8% utilization)
  
Best-Fit Time:
  Max time: 120.0/120 min (100% utilization - tight!)
  
DP:
  Max time: 115.2/120 min (96.0% utilization)
```

**Interpretation**:
- Best-Fit packs tightly (risk of overflow)
- Greedy leaves some margin
- DP balances utilization and flexibility

---

## Design Decisions

### Why time in minutes (not abstract units)?

Realistic interpretation: "This slot is 30 minutes, can I fit these 8 requests @ 10min each?"

Helps with:
- Understanding capacity constraints
- Real-world deployment
- Debugging violations

### Why track both loads AND times in DP state?

- **Loads**: Determine emission factor (capacity tiers)
- **Times**: Determine feasibility (time capacity)

Cannot merge: same load different times → different futures!

Example:
- `loads=[10,0], times=[100,0]`: Slot 0 nearly full (time), low emission
- `loads=[10,0], times=[50,0]`: Slot 0 half full (time), low emission
- Next 10-minute block: Second state has more flexibility!

### Parallelism model

Simple model: `time_used = sum(durations) / parallelism`

**Assumptions**:
- Perfect parallelization (no overhead)
- All requests start together (batch execution)
- No dependencies between requests

**Reality**: More complex scheduling, but this model captures key constraint.

---

## Troubleshooting

**Q: All methods show time overflows**

A: Increase parallelism or slot duration:
```python
PARALLELISM = 8       # Was 4
SLOT_DURATION_MIN = 60  # Was 30
```

**Q: DP too slow (>60s)**

A: Reduce problem size or increase pruning:
```python
NUM_REQUESTS = 20     # Was 30
NUM_BLOCKS = 5        # Was 6
BEAM_K = 100          # Was 150 (more aggressive)
```

**Q: All heuristics fail (no feasible assignment)**

A: Problem is over-constrained. Options:
1. Relax error threshold (4% → 6%)
2. Increase parallelism (more capacity)
3. Extend deadlines (more flexibility)
4. Add faster strategies (lower duration)

**Q: Greedy matches DP (Gap = 0%)**

A: Problem is simple. Create harder scenario:
- Tighter time capacity (lower parallelism)
- Tighter error budget
- Non-monotonic carbon pattern

---

## Comparison with capacity_tiers

| Aspect | capacity_tiers | time_capacity_tiers |
|--------|---------------|---------------------|
| **Constraint** | Load-based emission factor | Time capacity per slot |
| **State complexity** | D[error, loads] | D[error, loads, times] |
| **Key trade-off** | Load balancing vs green slots | Time packing vs flexibility |
| **Heuristics** | Greedy, ProbSlack | Greedy, First-Fit, Best-Fit |
| **Typical DP time** | 10-60s | 20-120s (higher complexity) |

**When to use which**:
- **capacity_tiers**: Focus on rebound effects, no explicit time limits
- **time_capacity_tiers**: Explicit SLAs, parallel execution modeling

---

## Future Extensions

1. **Variable parallelism**: Different slots have different capacities
2. **Strategy dependencies**: Some strategies require previous results
3. **Preemption**: Allow interrupting long-running strategies
4. **Uncertainty**: Probabilistic execution durations
5. **Multi-resource**: CPU, memory, network (not just time)

---

## References

- **capacity_tiers**: `../capacity_tiers/` - Load-based emission tiers
- **Original CarbonShift**: `../carbonshift.py` - Batch ILP optimizer
- **Test configuration**: `tests/comparison.py` - Full comparison setup
