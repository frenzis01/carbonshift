"""Pure synthetic carbon-intensity model — no I/O, no configuration, no clock.

Kept separate from `source.py` (the adapters) so the *shape* of the curve can
be unit-tested on its own, and so a future ML/statistical forecast model has an
obvious home that is not entangled with HTTP or the clock.

The curve mirrors `carbonshift_rs::engine::scheduler::generate_carbon_intensity_forecast`:
a daylight sigmoid (rise at 25% of the cycle, set at 75%, slope 18) inverted
for "night max / day min", plus a small autocorrelated perturbation.

Determinism is a hard requirement, not a nicety. Every value is a pure function
of `(seed, slot)` — see `source.py` for why: the scheduler re-plans against
this forecast, so a slot's value must be identical no matter when or in what
order it is asked for.

**Invariant: the future has no `actual`.** A forecast may cover any slot; an
*actual* exists only for the slot being entered. `observed_value` draws its
noise from the measuring slot, so a target slot has no stable "true" value —
see `source.py::actual` and `ARCHITECTURE.md` §10.
"""
from __future__ import annotations

import math
import random

#: Salt used when deriving a per-slot seed for the emulated "actual" reading.
_ACTUAL_SALT = "actual"


def _seeded_rng(*parts) -> random.Random:
    """A `random.Random` deterministic across processes and Python versions.

    `random.Random` only accepts None/int/float/str/bytes, and `hash()` of a
    tuple is salted per process (PYTHONHASHSEED), so neither a tuple nor
    `hash((seed, slot))` is usable — the latter would give a *different*
    forecast on every restart, silently re-pricing committed assignments.
    Joining the parts into a string is stable and explicit.
    """
    return random.Random("|".join(str(p) for p in parts))


def sigmoid(x: float, x0: float, k: float) -> float:
    """Standard logistic; split out so the transition slope is testable."""
    return 1.0 / (1.0 + math.exp(-k * (x - x0)))


def daylight_factor(slot: int, cycle_slots: int, transition_slope: float = 18.0) -> float:
    """Fraction of "daylight" for `slot` within a `cycle_slots`-long cycle.

    Returns 0.0 well into the night and ~1.0 at peak daylight, so callers can
    interpolate between a night maximum and a day minimum.
    """
    cycle = max(1, cycle_slots)
    x = (slot % cycle) / cycle
    rise = sigmoid(x, 0.25, transition_slope)
    fall = sigmoid(x, 0.75, transition_slope)
    return rise - fall


def forecast_value(
    slot: int,
    *,
    seed: int,
    cycle_slots: int,
    night_max: float,
    day_min: float,
    noise_std: float,
) -> float:
    """Forecast carbon intensity (gCO₂/kWh) for `slot`.

    The noise term blends the slot's own seeded draw with its neighbour's, at
    a fixed 0.7/0.3 weight. That gives a plausible degree of *autocorrelation*
    (adjacent slots do not jump independently) while remaining a pure function
    of the slot — the property a stateful RNG walk cannot offer.
    """
    trend = night_max - (night_max - day_min) * daylight_factor(slot, cycle_slots)

    if noise_std <= 0.0:
        return max(1.0, round(trend, 6))

    here = _seeded_rng(seed, slot).gauss(0.0, noise_std)
    neighbour = _seeded_rng(seed, slot - 1).gauss(0.0, noise_std)
    return max(1.0, round(trend + 0.7 * here + 0.3 * neighbour, 6))


def actual_value(slot: int, *, forecast: float, seed: int, jitter_std: float) -> float:
    """A plausible *measured* reading for `slot`: the forecast plus a small
    seeded deviation. Used only by the synthetic (emulation) role — a real
    deployment takes this from the remote upstream instead."""
    deviation = _seeded_rng(seed, _ACTUAL_SALT, slot).gauss(0.0, jitter_std)
    return max(0.0, round(forecast * (1.0 + deviation), 6))


def observed_value(
    measuring_slot: int,
    target_slot: int,
    *,
    forecast: float,
    seed: int,
    jitter_std: float,
) -> float:
    """The reading taken *at* `measuring_slot` for the slot it is entering.

    This exists to model one physical fact: **a measurement is an event, not a
    property of a slot.** A real meter can only ever tell you about the slot
    you are standing in; it cannot tell you the true intensity of tomorrow
    afternoon. So the noise is drawn per *measurement* (keyed on
    `measuring_slot`) and applied to `target_slot`'s forecast — which means the
    same target slot has NO stable "true" value, exactly as in reality.

    Consequence, and the point of the whole design: an `actual` for a future
    slot is not merely *unknown*, it is **unknowable**, and any code path that
    asks for one is modelling something that cannot physically happen. That is
    why `actual_value` above is only used for slots already reached.
    """
    deviation = _seeded_rng(seed, _ACTUAL_SALT, measuring_slot).gauss(0.0, jitter_std)
    return max(0.0, round(forecast * (1.0 + deviation), 6))
