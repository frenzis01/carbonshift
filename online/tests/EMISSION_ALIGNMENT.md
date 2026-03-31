# Emission Calculation Alignment

**Date**: 2026-03-30  
**Status**: ✅ Complete

## Problem

Initially, the online schedulers and the optimal solver (carbonshift.py) used different emission calculation formulas, making comparison meaningless:

- **carbonshift.py**: `emission = carbon[t] * duration / 3600 * 0.05`
- **Online schedulers**: `emission = carbon[t] * duration`

This caused massive numerical differences:
- Optimal solution: ~2 gCO2
- Online schedulers: ~77,400 gCO2
- Difference: **38,700x** (meaningless for comparison!)

## Solution

All schedulers now use the **same formula** from carbonshift.py:

```python
emission = carbon[gCO2/kWh] * duration[seconds] / 3600 * 0.05kW
```

Where:
- `carbon[t]` = carbon intensity at time slot t (gCO2/kWh)
- `duration` = execution time in seconds
- `/3600` = converts seconds to hours
- `0.05` = typical server power consumption (kW) = 50 Watts

**Result**: `gCO2/kWh * hours * kW = gCO2`

## Impact

Now the comparison is meaningful:
- Optimal solution: ~2 gCO2
- GreedyCarbonLookahead: ~3 gCO2 (**+50%** vs optimal)
- ProbabilisticSlack: ~7 gCO2 (**+250%** vs optimal)

These gaps are meaningful and allow proper evaluation of online scheduler performance!

## Files Modified

### Core Schedulers
- `online/heuristics.py` (line 141-144)
  - Updated GreedyCarbonLookahead carbon cost calculation

### Test Files
- `online/tests/test_stress.py` (line 124)
- `online/tests/test_optimal_comparison.py` (lines 170, 203, 232)
- `online/tests/test_heuristics.py` (lines 280, 378, 393)
- `online/tests/test_scheduler_comparison.py` (line 57-58)

### Documentation
- `online/tests/README.md`
- `online/tests/MIGRATION_SUMMARY.txt`

## Verification

All tests pass with aligned calculations:

```bash
cd online/tests
python test_heuristics.py        # ✅ Unit tests pass
python test_scheduler_comparison.py  # ✅ Comparison shows reasonable gaps
python test_optimal_comparison.py    # ✅ Online vs optimal comparison meaningful
```

## Technical Details

### Why This Formula?

The formula models realistic server power consumption:

1. **Duration normalization**: `/3600` converts seconds to hours
2. **Power consumption**: `0.05 kW` = 50W typical server draw
3. **Carbon intensity**: `gCO2/kWh` standard unit for grid carbon
4. **Result**: Total CO2 emissions in grams

### Example Calculation

```python
carbon = 150      # gCO2/kWh (typical grid intensity)
duration = 120    # seconds (2 minutes)
emission = 150 * 120 / 3600 * 0.05 = 0.25 gCO2
```

This represents the CO2 emitted by running a 50W server for 2 minutes on a grid with 150 gCO2/kWh intensity.

## Conclusion

✅ **All schedulers now use identical emission calculations**  
✅ **Comparisons are meaningful and accurate**  
✅ **Online scheduler optimality gaps are measurable**  

This enables proper evaluation and improvement of online scheduling heuristics against the optimal offline solution.
