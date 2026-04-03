# Capacity Tiers Extension

This module extends CarbonShift with capacity-aware scheduling that models emission factor increases based on slot load (rebound effects).

## Overview

Traditional CarbonShift assumes constant emission factors. In reality, overloading a "green" time slot can cause:
- Resource exhaustion requiring backup (high-emission) servers
- Performance degradation increasing execution time
- Spill-over effects to adjacent slots

**Capacity tiers** model this by defining emission factor multipliers based on load thresholds.

## Architecture

```
capacity_tiers/
├── __init__.py
├── greedy.py               # Fast baseline (with lookahead)
├── probabilistic_slack.py  # Online heuristic using deadline slack
├── dp_warmstart.py         # Optimal DP with warm-start + pruning
├── capacity_tiers_example.csv  # Example tier configuration
├── tests/
│   └── comparison.py       # Comprehensive comparison test
└── README.md               # This file
```

## Capacity Tiers Configuration

Define tiers in CSV format:

```csv
# capacity_tiers_example.csv
# capacity, emission_factor
1000, 1.0    # 0-1000 requests: normal emissions
3000, 1.2    # 1001-3000: +20% penalty
6000, 1.5    # 3001-6000: +50% penalty
9000, 4.0    # 6001-9000: +300% penalty (disaster!)
```

**Interpretation**: If a slot already has 2500 requests assigned and you add 100 more:
- New load = 2600
- Emission factor = 1.2× (second tier)
- Cost = `carbon_intensity × duration × 100 × 1.2`

## Scheduling Methods

### 1. Greedy (baseline)

**Strategy**: For each block, choose the (slot, strategy) pair with minimum cost.

**Key feature**: **Look-ahead on emission factor**
```python
new_load = current_load[t] + block_size
emission_factor = get_emission_factor(new_load, tiers)
cost = carbon[t] * duration[s] * block_size * emission_factor
```

This makes greedy surprisingly intelligent—it autocorrects when slots fill up.

**When to use**:
- Fast approximations needed
- Upper bound for warm-start pruning
- Baseline for comparison

**Limitations**:
- Myopic on complex constraints (error budgets, multi-objective)
- Cannot plan strategically across blocks

---

### 2. ProbabilisticSlack

**Strategy**: Use deadline slack to decide when to postpone.

```
If slack >= threshold AND error budget available:
    → Postpone to greenest slot with fast (high-error) strategy
Else:
    → Schedule to nearest green slot with high-quality strategy
```

**Parameters**:
- `slack_threshold`: Minimum slack required for postponing (default: 3)

**When to use**:
- Online scenarios (requests arrive incrementally)
- Simple heuristic without heavy computation

**Limitations**:
- Heuristic tuning required (slack threshold)
- May not find optimal solution

---

### 3. DP with Warm-Start

**Strategy**: Dynamic Programming exploring all feasible (error, loads) states.

**State**: `D[block][error][loads_tuple] = min_cost`
- `error`: Cumulative error so far
- `loads_tuple`: Number of requests in each slot

**Why loads in state?**: With capacity tiers, emission factor depends on load. Two paths with same error but different loads have different future costs. See `WHY_LOADS_IN_STATE.md` for mathematical proof.

**Warm-start**: Run greedy first, use cost as upper bound:
```python
if new_cost > greedy_cost:
    prune this state  # Cannot be better than greedy
```

Reduces state space by 98-99% in practice.

**Pruning strategies**:

1. **K-Best Pruning**: Keep only top-K states globally by cost
   - Simple, but loses diversity
   - May eliminate states that lead to better solutions later

2. **Beam Search**: Keep top-K states per error level
   - Maintains diversity across error levels
   - Recommended for balanced exploration/efficiency
   - Current configuration: K=200 (relaxed)

**When to use**:
- Batch optimization (all requests known upfront)
- Optimal solution required
- Small/medium problem sizes (< 50 requests)

**Limitations**:
- State space explosion: O(B × E × U × S × T) where U ≈ O(R^δ)
- Requires warm-start and pruning for practical use

---

## Running Comparison

```bash
cd capacity_tiers/tests
python comparison.py
```

**Test configuration** (in `comparison.py`):
- 40 requests, 8 blocks, 12 slots
- Error threshold: 3% (tight, exposes strategy differences)
- Generous deadlines: requests can reach last 80% of slots
- Carbon pattern: decreasing (later = greener)

**Expected results**:
- Greedy: Fast baseline (~0.02s)
- ProbabilisticSlack: Online heuristic (~0.01s)
- DP no warm-start: Optimal but slow (~10-60s)
- DP with warm-start: Optimal, 50-100× faster (~0.5s)
- DP with pruning: Optimal or near-optimal, 200-500× faster (~0.1s)

**When DP beats Greedy**:
- Tight error budgets forcing strategic error/quality trade-offs
- Complex cost landscapes where local minima trap greedy
- Long deadlines enabling postponement strategies

**When Greedy matches DP**:
- Simple scenarios with clear best choices
- Tight deadlines limiting feasible space
- Capacity tiers but no other constraints

---

## Example Usage

### Greedy

```bash
python greedy.py \
    requests.csv strategies.csv carbon.txt \
    12 8 3.0 \
    output.csv \
    --capacity-file capacity_tiers_example.csv
```

### ProbabilisticSlack

```bash
python probabilistic_slack.py \
    requests.csv strategies.csv carbon.txt \
    12 8 3.0 \
    output.csv \
    --capacity-file capacity_tiers_example.csv
```

### DP with warm-start

```bash
# Step 1: Get greedy cost
python greedy.py requests.csv strategies.csv carbon.txt 12 8 3.0 greedy.csv --capacity-file tiers.csv
# Extract cost from output: COST: 12345.0

# Step 2: Run DP with upper bound
python dp_warmstart.py \
    requests.csv strategies.csv carbon.txt \
    12 8 3.0 \
    output.csv \
    --capacity-file tiers.csv \
    --upper-bound 12345.0 \
    --pruning beam \
    --pruning-k 200
```

---

## Understanding the Results

### Cost Comparison

```
Method                    Cost       Gap    Time    States
Greedy                  907380      0.0%    0.02s        -
DP (no warm-start)      907380      0.0%    5.05s  1.2M
DP + Warm-Start         907380      0.0%    0.32s   49K
DP + K-Best            907380      0.0%    0.12s   41K
DP + Beam              907380      0.0%    0.10s   17K
```

**Interpretation**:
- All methods found optimal cost (Gap = 0.0%)
- Warm-start reduced states by 98% (1.2M → 49K)
- Pruning further reduced states by 50-80%
- Speedup: 15× (warm-start) to 50× (beam pruning)

### Load Distribution

```
Greedy:
  Slots used: 5/12
  Distribution: [0:8] [1:7] [2:8] [3:9] [4:8]

DP + Beam:
  Slots used: 6/12
  Distribution: [0:6] [1:5] [2:6] [4:8] [5:7] [8:8]
```

**Interpretation**:
- Greedy used first 5 slots (early scheduling)
- DP used 6 slots including slot 8 (strategic postponement)
- DP balanced loads better (max 8 vs 9)

---

## Design Decisions

### Why look-ahead in Greedy?

Without look-ahead, greedy would compute:
```python
cost = carbon[t] * duration[s] * block_size * current_emission_factor
```

This ignores rebound effects! A "cheap" green slot could become expensive after assignment.

With look-ahead:
```python
new_load = current_load[t] + block_size
cost = carbon[t] * duration[s] * block_size * get_emission_factor(new_load, tiers)
```

Greedy autocorrects and avoids overload naturally.

### Why loads in DP state?

**Without loads** (only track error):
```python
D[error] = min_cost  # WRONG with capacity tiers!
```

**Problem**: Two paths with same error but different loads:
- Path A: `loads = [10, 0, 0]`
- Path B: `loads = [5, 5, 0]`

Next block goes to slot 0:
- Path A: load 10→15, emission factor 4.0×, high cost
- Path B: load 5→10, emission factor 2.0×, lower cost

Same error, different future costs → **must track loads in state**.

### Why relaxed pruning?

Aggressive pruning (K=10) risks eliminating states that lead to better solutions:
1. State looks expensive now (high cost)
2. But enables cheap future assignments (strategic positioning)
3. Pruned too early → suboptimal solution

Relaxed pruning (K=200-5000):
- Keeps more diverse exploration
- Higher success rate finding optimal
- Still 100-500× faster than full DP

---

## Troubleshooting

**Problem**: DP returns `inf` cost

**Cause**: Pruning eliminated all feasible states.

**Solution**:
- Increase `--pruning-k` (e.g., 500 → 5000)
- Check upper bound is not too tight
- Verify error threshold allows feasible solutions

---

**Problem**: Greedy always matches DP

**Cause**: Problem structure is simple (greedy look-ahead sufficient).

**Solution**: Create harder scenarios:
- Tight error budget (threshold = 3%)
- Long deadlines (last 80% of slots)
- Non-monotonic carbon pattern (traps greedy)

---

**Problem**: DP too slow

**Cause**: State space explosion with many slots/blocks.

**Solution**:
- Use warm-start (mandatory for > 40 requests)
- Apply beam pruning with K=100-200
- Reduce slots or blocks if possible

---

## References

- Original CarbonShift: `../carbonshift.py`
- DP without capacity tiers: `../carbonshiftDP.py`
- Loads in state proof: `WHY_LOADS_IN_STATE.md`
- Greedy vs DP analysis: `GREEDY_VS_DP_EXPLANATION.md`

---

## Future Work

- [ ] Incremental DP (reoptimize as requests arrive)
- [ ] Multi-service with communication costs
- [ ] Probabilistic carbon forecasts
- [ ] Tier transition smoothing (avoid cliff effects)
