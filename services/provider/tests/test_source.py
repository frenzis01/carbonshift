"""Tests for the source port and its adapters — the seam that lets a local and
a remote provider coexist without touching anything else.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.source import (
    CarbonIntensitySource,
    RemoteCarbonIntensitySource,
    SyntheticCarbonIntensitySource,
    build_source,
)

EPOCH = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _local(**overrides) -> SyntheticCarbonIntensitySource:
    kwargs = dict(
        seed=26,
        slot_minutes=30,
        epoch=EPOCH,
        night_max=160.0,
        day_min=70.0,
        cycle_slots=12,
        noise_std=2.0,
        actual_jitter_std=0.05,
    )
    kwargs.update(overrides)
    return SyntheticCarbonIntensitySource(**kwargs)


# ─── determinism (the property the scheduler depends on) ─────────────────────


def test_local_forecast_is_deterministic_across_instances():
    a = _local().forecast(100, 5)
    b = _local().forecast(100, 5)
    assert [p.to_dict() for p in a] == [p.to_dict() for p in b]


def test_local_forecast_does_not_depend_on_call_history():
    """A slot's value must not depend on which slots were asked for first —
    otherwise a restart would silently re-price already-committed assignments."""
    fresh = _local()
    warm = _local()
    warm.forecast(0, 200)  # advance/warm up in a different order
    assert [p.forecast for p in fresh.forecast(150, 3)] == [
        p.forecast for p in warm.forecast(150, 3)
    ]


def test_local_forecast_is_reproducible_from_the_seed():
    assert [p.forecast for p in _local(seed=1).forecast(0, 4)] != [
        p.forecast for p in _local(seed=2).forecast(0, 4)
    ]


# ─── shape ───────────────────────────────────────────────────────────────────


def test_forecast_returns_requested_count_in_ascending_slot_order():
    points = _local().forecast(7, 4)
    assert [p.slot for p in points] == [7, 8, 9, 10]


def test_forecast_returns_empty_for_non_positive_count():
    assert _local().forecast(3, 0) == []
    assert _local().forecast(3, -1) == []


def test_forecast_values_are_within_a_plausible_range():
    for p in _local().forecast(0, 96):
        assert 0.0 <= p.forecast <= 1000.0


def test_local_varies_between_night_and_day_within_a_cycle():
    # night_max > day_min, so the cycle must not be flat.
    values = [p.forecast for p in _local(noise_std=0.0).forecast(0, 12)]
    assert max(values) - min(values) > 10.0


def test_zero_noise_still_produces_a_smooth_curve():
    values = [p.forecast for p in _local(noise_std=0.0).forecast(0, 6)]
    # Adjacent slots should be close when noise is disabled.
    assert all(abs(b - a) < 60.0 for a, b in zip(values, values[1:]))


# ─── the capability declaration ──────────────────────────────────────────────
#
# The invariant under test here was corrected after review: a measurement is an
# EVENT (taken when you enter a slot), not a property of a slot. So there is no
# "actual for a future slot" to declare support for — instead, the contract
# makes asking for one impossible.


def test_local_forecast_carries_no_actual_at_all():
    """A forecast window is a prediction. It must not smuggle measurements."""
    assert all(not hasattr(p, "actual") for p in _local().forecast(0, 10))


def test_local_can_observe_the_slot_it_is_standing_in():
    reading = _local().observe(500)
    assert reading is not None
    assert reading.slot == 500
    assert reading.observed_at_slot == 500


def test_local_observation_deviates_from_the_forecast():
    """Emulation must produce drift between prediction and measurement."""
    source = _local()
    forecast = source.forecast(500, 1)[0].forecast
    reading = source.observe(500)
    assert reading.actual != forecast
    assert abs(reading.actual - forecast) / forecast < 0.5


def test_local_refuses_to_observe_a_future_slot():
    """The core correction: you cannot measure a slot you have not entered.

    The synthetic adapter *could* fabricate a number here, and that is exactly
    why the guard has to exist — otherwise emulation would exercise a code path
    that cannot occur in production.
    """
    assert _local().observe(measuring_slot=500, target_slot=501) is None


def test_local_refuses_to_observe_a_past_slot_too():
    assert _local().observe(measuring_slot=500, target_slot=499) is None


def test_observation_is_deterministic_for_a_given_slot():
    assert _local().observe(7).actual == _local().observe(7).actual


def test_observations_of_different_slots_differ():
    source = _local()
    assert source.observe(1).actual != source.observe(2).actual


def test_observed_point_serialises_for_the_wire():
    payload = _local().observe(3).to_dict()
    assert set(payload) == {"slot", "actual", "observed_at_slot"}


def test_remote_also_refuses_a_future_target():
    """The guard lives in the port, so both adapters inherit it."""
    remote = RemoteCarbonIntensitySource(base_url="https://example.invalid", slot_minutes=30, epoch=EPOCH)
    assert remote.observe(measuring_slot=500, target_slot=501) is None


def test_remote_returns_none_rather_than_fabricating_a_measurement():
    """A stub must never invent grid data: reporting a plausible number where
    none exists is the worst failure mode for this component."""
    remote = RemoteCarbonIntensitySource(base_url="https://example.invalid", slot_minutes=30, epoch=EPOCH)
    assert remote.observe(500) is None


# ─── the remote stub must not break the clock loop ───────────────────────────


def test_remote_stub_returns_empty_rather_than_raising():
    remote = RemoteCarbonIntensitySource(base_url="https://example.invalid", slot_minutes=30, epoch=EPOCH)
    assert remote.forecast(0, 24) == []
    assert remote.health()["ok"] is False


def test_both_adapters_satisfy_the_port():
    assert issubclass(SyntheticCarbonIntensitySource, CarbonIntensitySource)
    assert issubclass(RemoteCarbonIntensitySource, CarbonIntensitySource)


# ─── factory ─────────────────────────────────────────────────────────────────


def test_build_source_selects_by_role():
    assert build_source(Settings(role="local")).name == "local"
    assert build_source(Settings(role="remote")).name == "remote"


def test_build_source_is_case_and_whitespace_insensitive():
    assert build_source(Settings(role="  LOCAL  ")).name == "local"


def test_build_source_rejects_unknown_role_loudly():
    """A typo must not silently look like a working emulation setup."""
    with pytest.raises(ValueError, match="unknown PROVIDER_ROLE"):
        build_source(Settings(role="romote"))
