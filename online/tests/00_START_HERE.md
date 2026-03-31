# 🚀 START HERE - Carbonshift Online Scheduler Tests

## Quick Run

```bash
cd online/tests
python run_all_tests.py
```

This runs all three comprehensive comparison tests.

---

## What's Here

### 📋 Comparison Tests (NEW)

1. **test_scheduler_comparison.py** (~5s)
   - Compares all online schedulers
   - Moved from `online/example.py`
   - 50 requests

2. **test_stress.py** (~60s)
   - High-load testing (100-5000 requests)
   - Multiple arrival patterns
   - Performance profiling

3. **test_optimal_comparison.py** (~120s)
   - Compares vs optimal batch ILP
   - Requires `carbonshift.py` and `ortools`
   - Shows optimality gap

### 🧪 Unit Tests (Existing)

- `test_heuristics.py` - Heuristic scheduler tests
- `test_request_predictor.py` - Request predictor tests  
- `test_rolling_window_ilp.py` - ILP scheduler tests

---

## Quick Tests

```bash
# Individual comparison tests
python test_scheduler_comparison.py  # Fast (~5s)
python test_stress.py               # Load test (~60s)
python test_optimal_comparison.py   # Optimal gap (~120s)

# Unit tests
python -m pytest test_heuristics.py
```

---

## Requirements

If using virtualenv:
```bash
source ../../venv/bin/activate
```

Install dependencies:
```bash
pip install -r ../../requirements.txt
```

---

## Expected Output

Good results show:
- ✅ 30-80% emissions reduction
- ✅ No deadline violations
- ✅ >100 req/sec throughput
- ✅ <20% optimality gap

Example:
```
✓ Best approach: GreedyCarbonLookahead
  Emissions: 209,250 (-77.7% vs baseline)
```

---

## Troubleshooting

### "ModuleNotFoundError: No module named 'ortools'"
Solution:
```bash
source ../../venv/bin/activate
pip install -r ../../requirements.txt
```

### "carbonshift.py not found"
The optimal comparison test requires `carbonshift.py` in the project root.
It will gracefully skip if not found.

---

## Documentation

See `README.md` for detailed documentation.

---

**Run `python run_all_tests.py` to get started!** 🎉
