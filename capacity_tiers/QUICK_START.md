# Quick Start Guide

## TL;DR - Run the comparison

```bash
cd capacity_tiers/tests
python comparison.py
```

Expected output: **DP beats Greedy by ~26%** in ~15 seconds total.

---

## What is this?

Capacity Tiers extends CarbonShift with **rebound effect modeling**:
- Overloading a "green" slot increases emissions (more servers, longer execution)
- Define capacity tiers: e.g., 0-10 reqs = 1.0×, 11-20 = 2.0×, 21+ = 4.0×
- Schedulers must balance load across slots to minimize total emissions

---

## Three Schedulers

### 1. Greedy (fast baseline)
- For each block, pick cheapest (slot, strategy) pair
- Has **lookahead**: checks emission factor AFTER adding block
- Surprisingly smart but myopic on complex constraints
- Runtime: ~0.02s

### 2. ProbabilisticSlack (online heuristic)
- Uses deadline slack to decide when to postpone
- If deadline is far → use fast/cheap strategy and postpone to green slot
- If deadline is near → use slow/high-quality strategy immediately
- Runtime: ~0.02s

### 3. DP with Warm-Start (optimal)
- Dynamic programming exploring all (error, loads) states
- Warm-start: prune states worse than greedy
- Pruning: K-Best or Beam Search to reduce state space
- Runtime: ~0.4s (warm-start) to ~12s (full DP)

---

## Example Results

```
Method                    Cost       Gap      Time      States
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Greedy                  288,000    35.2%     0.02s           -
ProbabilisticSlack      510,000   139.4%     0.02s           -
🏆DP (no warm-start)    213,000     0.0%    12.44s   2,551,407
🏆DP + Warm-Start       213,000     0.0%     0.44s     271,929
🏆DP + Beam             213,000     0.0%     0.39s      65,186
```

**Key insights**:
- ✅ DP found 26% better solution than Greedy
- ⚡ Warm-start reduced states by 89% (2.5M → 271K)
- ⚡ Beam pruning reduced states by 97% (2.5M → 65K)
- ⚡ Best speedup: 32× faster with beam pruning

---

## Why DP Wins

**Greedy (myopic)**:
```
Block 0: Pick slot 3, Medium strategy → looks good locally
Block 1: Pick slot 4, Medium strategy → still good
...
Block 7: Uh oh, error budget exhausted!
         Must use expensive High strategy
Total: Expensive!
```

**DP (strategic)**:
```
Blocks 0-5: Use cheap Low strategy (high error but within budget)
Blocks 6-7: Use expensive High strategy (to balance error)
Average error: Under threshold ✓
Total: 26% cheaper!
```

DP plans **globally** across all blocks to optimize the trade-off.

---

## Configuration

Edit `tests/comparison.py` to customize:

```python
NUM_REQUESTS = 40       # Number of requests
NUM_BLOCKS = 8          # Batch into N blocks
NUM_SLOTS = 12          # Time slots available
ERROR_THRESHOLD = 3     # Max average error (%)

CAPACITY_TIERS = [
    (5, 1.0),    # 0-5 requests: normal
    (10, 2.0),   # 6-10: 2× penalty
    (15, 4.0),   # 11-15: 4× penalty
    (30, 8.0),   # 16+: 8× penalty (disaster!)
]

KBEST_K = 5000   # K-Best pruning: keep top K states
BEAM_K = 200     # Beam pruning: keep top K per error level
```

**Tips**:
- Increase `NUM_REQUESTS` → harder problem, longer runtime
- Tighten `ERROR_THRESHOLD` → exposes DP advantage
- Increase `KBEST_K`/`BEAM_K` → more exploration, slower

---

## File Structure

```
capacity_tiers/
├── README.md                    # Full documentation
├── IMPLEMENTATION_SUMMARY.md    # Technical summary
├── QUICK_START.md              # This file
├── greedy.py                   # Greedy scheduler
├── probabilistic_slack.py      # ProbSlack heuristic
├── dp_warmstart.py             # DP with warm-start
├── capacity_tiers_example.csv  # Example tier config
└── tests/
    └── comparison.py           # Comprehensive test
```

---

## Common Issues

**Q: DP returns `inf` cost**

A: Pruning eliminated all states. Increase K or check error threshold.

**Q: Greedy matches DP (Gap = 0%)**

A: Problem is too simple. Try:
- Tighter error threshold (e.g., 3% → 2%)
- Longer deadlines (min_deadline = 80% of slots)
- More complex carbon pattern

**Q: Test too slow**

A: Reduce problem size:
- `NUM_REQUESTS = 20` (was 40)
- `NUM_BLOCKS = 5` (was 8)
- `NUM_SLOTS = 10` (was 12)

**Q: ProbabilisticSlack fails**

A: Check `NUM_BLOCKS` configuration. Should work with any value now.

---

## Next Steps

1. **Read full docs**: `README.md` for complete technical details
2. **Run your data**: Modify test to use real carbon intensity traces
3. **Tune parameters**: Adjust capacity tiers to match your infrastructure
4. **Integrate**: Use as library in your application

---

For questions, see `README.md` or check the code comments.
