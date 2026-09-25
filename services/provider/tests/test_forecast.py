"""Direct tests for the pure synthetic model.

Split from `test_source.py` on purpose: the curve shape is a *model* concern and
should be verifiable without constructing an adapter, a clock, or any config.
"""
from __future__ import annotations

from app import forecast as f

DEFAULTS = dict(seed=26, cycle_slots=12, night_max=160.0, day_min=70.0, noise_std=2.0)


# ─── sigmoid / daylight ──────────────────────────────────────────────────────


def test_sigmoid_is_centred_on_its_midpoint():
    assert f.sigmoid(0.5, 0.5, 18.0) == 0.5


def test_sigmoid_is_monotonic_increasing():
    assert f.sigmoid(-1.0, 0.0, 18.0) < f.sigmoid(1.0, 0.0, 18.0)


def test_daylight_factor_is_near_zero_at_the_start_of_the_cycle():
    assert f.daylight_factor(0, 12) < 0.05


def test_daylight_factor_peaks_mid_cycle():
    assert f.daylight_factor(6, 12) > 0.9


def test_daylight_factor_is_within_bounds_for_a_whole_cycle():
    for slot in range(12):
        assert -0.01 <= f.daylight_factor(slot, 12) <= 1.01


def test_daylight_factor_handles_a_degenerate_cycle_length():
    # cycle_slots=0 must not divide by zero.
    assert 0.0 <= f.daylight_factor(3, 0) <= 1.0


def test_daylight_factor_repeats_every_cycle():
    assert f.daylight_factor(2, 12) == f.daylight_factor(14, 12)


# ─── forecast_value ──────────────────────────────────────────────────────────


def test_forecast_value_is_a_pure_function_of_the_slot():
    assert f.forecast_value(42, **DEFAULTS) == f.forecast_value(42, **DEFAULTS)


def test_forecast_value_does_not_depend_on_call_order():
    first = f.forecast_value(100, **DEFAULTS)
    for s in range(0, 50):
        f.forecast_value(s, **DEFAULTS)
    assert f.forecast_value(100, **DEFAULTS) == first


def test_forecast_value_is_stable_across_processes():
    """Guards the seeding scheme.

    `hash()` of a tuple is salted per process (PYTHONHASHSEED), so a
    `hash`-based seed would give a *different* forecast after every restart —
    silently re-pricing already-committed assignments. Running in a subprocess
    with a different hash seed is the only way to actually prove this, so
    assert on a real second process rather than on a literal.
    """
    import subprocess
    import sys

    script = (
        "from app import forecast as f;"
        "print(f.forecast_value(42, seed=26, cycle_slots=12,"
        " night_max=160.0, day_min=70.0, noise_std=2.0))"
    )
    outputs = []
    for hash_seed in ("0", "12345"):
        out = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, check=True,
            env={"PYTHONHASHSEED": hash_seed, "PATH": "/usr/bin:/bin"},
        )
        outputs.append(out.stdout.strip())

    assert outputs[0] == outputs[1], (
        "forecast changed when PYTHONHASHSEED changed — the seeding scheme is "
        "process-dependent and will drift across restarts"
    )
    assert float(outputs[0]) == f.forecast_value(
        42, seed=26, cycle_slots=12, night_max=160.0, day_min=70.0, noise_std=2.0
    )


def test_forecast_value_is_never_below_one():
    for slot in range(200):
        assert f.forecast_value(slot, **DEFAULTS) >= 1.0


def test_forecast_value_stays_inside_the_night_day_envelope():
    """The daylight sigmoid approaches its bounds asymptotically, so the curve
    reaches *near* night_max / day_min rather than exactly hitting them."""
    values = [f.forecast_value(s, **{**DEFAULTS, "noise_std": 0.0}) for s in range(12)]
    assert values[0] < DEFAULTS["night_max"]
    assert min(values) > DEFAULTS["day_min"]
    # ...but the cycle must still span most of the configured range.
    assert (max(values) - min(values)) > 0.9 * (
        DEFAULTS["night_max"] - DEFAULTS["day_min"]
    )


def test_forecast_curve_is_monotonic_down_then_up_within_a_cycle():
    """Sanity-check the *shape*: carbon intensity should fall through the
    morning and rise again in the evening, not oscillate randomly."""
    values = [f.forecast_value(s, **{**DEFAULTS, "noise_std": 0.0}) for s in range(12)]
    trough = values.index(min(values))
    assert values[: trough + 1] == sorted(values[: trough + 1], reverse=True)
    assert values[trough:] == sorted(values[trough:])


def test_zero_noise_disables_the_random_term_entirely():
    a = f.forecast_value(9, **{**DEFAULTS, "noise_std": 0.0})
    b = f.forecast_value(9, **{**DEFAULTS, "noise_std": 0.0})
    assert a == b


def test_different_seeds_give_different_curves():
    a = [f.forecast_value(s, **DEFAULTS) for s in range(12)]
    b = [f.forecast_value(s, **{**DEFAULTS, "seed": 99}) for s in range(12)]
    assert a != b


def test_adjacent_slots_are_correlated_not_independent():
    """The 0.7/0.3 neighbour blend exists to avoid adjacent slots jumping
    independently; a jump larger than any plausible grid swing would mean the
    blend was lost."""
    values = [f.forecast_value(s, **DEFAULTS) for s in range(40)]
    jumps = [abs(b - a) for a, b in zip(values, values[1:])]
    assert max(jumps) < 100.0


# ─── actual_value ────────────────────────────────────────────────────────────


def test_actual_value_is_deterministic():
    kwargs = dict(forecast=100.0, seed=1, jitter_std=0.05)
    assert f.actual_value(7, **kwargs) == f.actual_value(7, **kwargs)


def test_actual_value_deviates_from_the_forecast():
    value = f.actual_value(7, forecast=100.0, seed=1, jitter_std=0.05)
    assert value != 100.0


def test_actual_value_stays_near_the_forecast_for_a_small_jitter():
    for slot in range(100):
        value = f.actual_value(slot, forecast=100.0, seed=1, jitter_std=0.05)
        assert 80.0 < value < 120.0


def test_actual_value_is_never_negative():
    for slot in range(100):
        assert f.actual_value(slot, forecast=1.0, seed=5, jitter_std=5.0) >= 0.0


def test_actual_uses_a_different_stream_than_the_forecast():
    """Salt avoids the actual accidentally equalling the forecast for every
    slot, which would make the emulation look suspiciously clean."""
    forecast = f.forecast_value(3, **DEFAULTS)
    actual = f.actual_value(3, forecast=forecast, seed=DEFAULTS["seed"], jitter_std=0.05)
    assert actual != forecast


# ─── observed_value: the measurement is an event ─────────────────────────────


def test_observed_value_is_deterministic_for_a_measuring_slot():
    kwargs = dict(forecast=100.0, seed=1, jitter_std=0.05)
    a = f.observed_value(700, 700, **kwargs)
    b = f.observed_value(700, 700, **kwargs)
    assert a == b


def test_observed_value_deviates_from_the_forecast():
    assert f.observed_value(700, 700, forecast=100.0, seed=1, jitter_std=0.05) != 100.0


def test_observed_value_noise_is_keyed_on_the_measuring_slot():
    """The key property: the same target measured from two different moments
    yields two different readings. That is what makes 'the true value of a
    future slot' a question with no answer, rather than one this model just
    happens not to expose."""
    a = f.observed_value(measuring_slot=700, target_slot=701, forecast=100.0, seed=1, jitter_std=0.05)
    b = f.observed_value(measuring_slot=701, target_slot=701, forecast=100.0, seed=1, jitter_std=0.05)
    assert a != b


def test_observed_value_is_never_negative():
    assert f.observed_value(5, 5, forecast=0.5, seed=3, jitter_std=5.0) >= 0.0


def test_observed_value_stays_near_the_forecast_for_a_small_jitter():
    for slot in range(50):
        value = f.observed_value(slot, slot, forecast=100.0, seed=2, jitter_std=0.05)
        assert 80.0 < value < 120.0
