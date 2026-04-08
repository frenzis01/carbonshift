# Comparison: Discrete vs Contiguous Time Models

## Summary

| Aspect | Discrete Model | Contiguous Model |
|--------|----------------|------------------|
| **Request placement** | Entire request in ONE slot | Can span MULTIPLE slots |
| **Long requests** | ❌ Cannot exceed slot duration | ✅ Can exceed slot duration |
| **Slot utilization** | Lower (fragmentation) | Higher (fills gaps) |
| **Complexity** | Moderate | Higher (must track spanning) |
| **State space** | Smaller | Larger (residual times) |
| **Realistic?** | No | Yes |

---

## Example Comparison

### Scenario

**Setup:**
- 2 requests, each 20 minutes
- 3 slots, each 30 minutes
- Parallelism = 1
- Carbon: [100, 50, 20] (greener later)

---

### Discrete Model

```
Assignment:
  Request 1 → Slot 1 (cost = 50 × 20 = 1000)
  Request 2 → Slot 2 (cost = 20 × 20 = 400)

Total cost: 1400

Slot usage:
  Slot 0: 0/30 min (0%)
  Slot 1: 20/30 min (67%) → 10 min wasted
  Slot 2: 20/30 min (67%) → 10 min wasted

Wasted capacity: 20 minutes (33%)
```

**Limitation**: Each slot has wasted capacity, but we can't use it for other requests.

---

### Contiguous Model

```
Assignment:
  Request 1 → Starts Slot 1
    - Slot 1: 20 min (cost = 50 × 20 = 1000)
  
  Request 2 → Starts Slot 1
    - Slot 1: 10 min (cost = 50 × 10 = 500)
    - Slot 2: 10 min (cost = 20 × 10 = 200)

Total cost: 1700

Slot usage:
  Slot 0: 0/30 min (0%)
  Slot 1: 30/30 min (100%) → fully utilized!
  Slot 2: 10/30 min (33%)

Wasted capacity: 10 minutes (17%)
```

**Benefit**: Better slot utilization (20 min waste → 10 min waste).

**Trade-off**: Higher cost in this case (1700 vs 1400) because Request 2 uses expensive Slot 1 for 10 min. But in other scenarios, it can be cheaper.

---

## When Does Contiguous Model Win?

### Scenario 1: Long Requests

**Request duration: 50 minutes**
**Slot duration: 30 minutes**

**Discrete**: ❌ INFEASIBLE (request doesn't fit in any slot)

**Contiguous**: ✅ Spans 2 slots
```
Slot 0: 30 min
Slot 1: 20 min
Total: 50 min (feasible!)
```

**Verdict**: Contiguous model is **necessary** for long requests.

---

### Scenario 2: Better Packing

```
Requests: [25 min, 25 min, 25 min]
Slots: 3 × 30 min
Carbon: [100, 50, 20]
```

**Discrete**:
```
Request 1 → Slot 0 (cost = 100 × 25 = 2500, waste 5 min)
Request 2 → Slot 1 (cost = 50 × 25 = 1250, waste 5 min)
Request 3 → Slot 2 (cost = 20 × 25 = 500, waste 5 min)
Total: 4250
Utilization: 75/90 = 83%
```

**Contiguous**:
```
Request 1 → Slot 2 (cost = 20 × 25 = 500)
Request 2 → Slot 2 + Slot 1 (cost = 20 × 5 + 50 × 20 = 100 + 1000 = 1100)
Request 3 → Slot 1 (cost = 50 × 10 = 500, then overflow... wait this is complex)
```

Actually let me recalculate greedy behavior...

**Greedy Contiguous** (better):
```
All 3 requests → Start at Slot 2
  Request 1: Slot 2 (25 min) → cost = 20 × 25 = 500
  Request 2: Slot 2 (5 min) + Slot 1 (20 min) → cost = 20×5 + 50×20 = 1100
  Request 3: Slot 1 (10 min) + Slot 0 (15 min) → cost = 50×10 + 100×15 = 2000
Total: 3600 vs 4250 → SAVINGS OF 650!
```

**Verdict**: Contiguous model can find **cheaper** solutions by better packing.

---

## Complexity Comparison

### State Space Size

**Discrete Model:**
```python
# State: D[block][error, loads_per_slot, times_per_slot]
# But loads and times are discrete (integer number of requests)

States ≈ B × E × (num_load_levels)^Δ × (num_time_levels)^Δ
```

**Contiguous Model:**
```python
# State: D[block][error, residual_time_per_slot]
# Residual times are continuous (0 to slot_capacity)

States ≈ B × E × (slot_capacity)^Δ  ← MUCH LARGER!
```

**Example:**
- Δ = 10 slots
- Slot capacity = 120 minutes (30 min × 4 parallelism)
- Discrete: ~(20 load levels)^10 ≈ 10^13 states
- Contiguous: ~(120 time levels)^10 ≈ 2.8 × 10^20 states ← **EXPLOSION!**

**Solution**: Use discretization (granularity 5-10 min) to reduce state space.

---

## Implementation Complexity

| Task | Discrete | Contiguous |
|------|----------|------------|
| **Check feasibility** | Simple: `time_used ≤ capacity` | Complex: Must simulate spanning |
| **Calculate cost** | Single slot | Multi-slot proportional |
| **Track state** | Load + time per slot | Residual time per slot |
| **DP transition** | O(Δ × S) | O(Δ × S × max_span) |
| **Code complexity** | ~300 LOC | ~400 LOC |

---

## Performance Comparison

### Greedy Heuristics

**Discrete**: ~0.02s (very fast)
**Contiguous**: ~0.03s (slightly slower due to spanning simulation)

**Verdict**: Both are fast enough.

### DP Optimal

**Discrete**: 
- Without pruning: ~3s for 30 requests
- With beam pruning: ~0.06s

**Contiguous**:
- Without pruning: Likely **HOURS** (state explosion)
- With discretization + pruning: ~0.5-2s (estimated)

**Verdict**: Contiguous DP **requires** discretization and aggressive pruning.

---

## When to Use Which?

### Use Discrete Model When:

1. ✅ Requests fit comfortably in single slots
2. ✅ You need fast DP optimal solutions
3. ✅ State space manageable
4. ✅ Fragmentation not a critical issue

### Use Contiguous Model When:

1. ✅ Requests can exceed slot duration
2. ✅ Better slot utilization is critical
3. ✅ Realistic spanning behavior needed
4. ⚠️ Can accept slower/approximate DP (or use heuristics only)

---

## Hybrid Approach (Recommended)

**Idea**: Use discrete model with **smaller slot granularity**.

Instead of:
- Discrete model with 30-min slots → high fragmentation

Use:
- Discrete model with 5-min slots → lower fragmentation
- 30-min window = 6 sub-slots

**Benefits**:
- Simpler implementation (discrete)
- Lower fragmentation (fine granularity)
- Manageable state space
- Approximates contiguous behavior

**Example**:
```
Original: 3 slots × 30 min = [0-30, 30-60, 60-90]
Fine-grained: 18 slots × 5 min = [0-5, 5-10, ..., 85-90]

50-min request: Can now fit in discrete model (uses 10 consecutive 5-min slots)
```

---

## Conclusion

| Model | Pros | Cons | Best For |
|-------|------|------|----------|
| **Discrete** | Fast, simple, DP tractable | Fragmentation, can't handle long requests | Small requests, fast DP needed |
| **Contiguous** | Realistic, better utilization | Complex, DP slow | Long requests, realistic simulation |
| **Discrete (fine-grained)** | Good approximation, tractable | More slots → larger state space | Balance between realism and speed |

**Recommendation for Thesis**:
1. Implement **both** discrete and contiguous
2. Show trade-offs empirically
3. Consider fine-grained discrete as middle ground
4. Use contiguous for **demonstrating concept**, discrete for **large-scale experiments**
