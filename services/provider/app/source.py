"""The carbon-intensity *port* and its two adapters.

This module is the heart of the "local vs remote can coexist cleanly" design
question. The rest of the system (carbonshift, client, executor) and the rest
of this service (main.py, clock.py, notifications.py) only ever depend on
:class:`CarbonIntensitySource`. Which concrete implementation is active is a
settings choice, not a wiring change:

    local  → SyntheticCarbonIntensitySource   (deterministic, offline)
    remote → RemoteCarbonIntensitySource      (carbonintensity.org.uk; stub)

Because both live behind one interface, adding the remote one later touches
exactly one new class plus one `build_source()` branch — nothing else
(Open/Closed at the seam that matters).
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from . import forecast as forecast_model
from .slots import as_utc, parse_epoch

# TODO: (remote) resample half-hourly upstream buckets when slot_minutes != 30.
#   Decision needed first: mean, max, or value-at-slot-start? See ARCHITECTURE.md §9.4

@dataclass(frozen=True)
class ForecastPoint:
    """One slot's worth of carbon intensity **forecast**.

    Only `forecast` lives here. There is deliberately no `actual` field: a
    forecast spans many slots, whereas a measurement is a single event that
    can only concern the slot being entered (`ObservedPoint` below). Folding
    them into one row is what made it possible to ask for "the actual of a
    future slot", which is not a thing that exists.
    """

    slot: int
    forecast: float

    def to_dict(self) -> dict:
        return {"slot": self.slot, "forecast": self.forecast}


@dataclass(frozen=True)
class ObservedPoint:
    """A reading that was actually *taken*, at a known moment.

    `slot` is the slot the reading describes, and `observed_at_slot` is the
    slot during which it was taken. They are the same value in every case
    that can physically happen (you measure the slot you are standing in);
    both are carried so that any code which ever tries to model a
    "measurement of the future" fails loudly rather than silently reading a
    plausible-looking number.
    """

    slot: int
    actual: float
    observed_at_slot: int

    def to_dict(self) -> dict:
        return {
            "slot": self.slot,
            "actual": self.actual,
            "observed_at_slot": self.observed_at_slot,
        }


class CarbonIntensitySource(ABC):
    """Port: something that can say how carbon-intense the grid is/will be.

    The contract has *two* clearly separated capabilities, and keeping them
    apart is the whole point:

    * `forecast(from_slot, count)` — spans any range, past or future.
    * `observe(measuring_slot, target_slot)` — a single measurement, and only
      for a slot that has actually been reached.

    Implementations MUST be cheap enough to call once per slot and MUST NOT
    raise for a merely-unknown slot (return `None`/empty instead) — the
    service is on the critical path of every slot rollover.
    """

    #: Short identifier surfaced on /health and /v1/meta, so an operator can
    #: tell which adapter is live (and tests can assert it).
    name: str = "abstract"

    @abstractmethod
    def forecast(self, from_slot: int, count: int) -> list[ForecastPoint]:
        """`count` forecast points starting at `from_slot`, ascending.

        `from_slot` may legitimately be in the past (a caller catching up),
        so implementations should be defined for any slot, not just "now".
        Returned list may be shorter than `count` if the source simply does
        not know (e.g. beyond a 48h upstream horizon).

        Implementations must return `[]` rather than raising when they have
        no data: a missing forecast degrades optimisation, it must not take
        down the clock loop.
        """

    def observe(self, measuring_slot: int, target_slot: Optional[int] = None) -> Optional[ObservedPoint]:
        """Measure the grid intensity, taken during `measuring_slot`.

        `target_slot` defaults to `measuring_slot`. Returning `None` means
        "no reading taken" — the honest answer for a source that is
        unreachable, or for a `target_slot` that has not been reached yet.

        The default implementation refuses a target that is not the slot
        being measured. That is a deliberate guard, not pedantry: it makes
        "measure the future" an explicit, reviewable decision instead of
        something that silently works because a synthetic adapter happened to
        be able to fabricate the number.
        """
        target = measuring_slot if target_slot is None else target_slot
        if target != measuring_slot:
            return None
        return self._observe_current(measuring_slot)

    @abstractmethod
    def _observe_current(self, slot: int) -> Optional[ObservedPoint]:
        """The reading for `slot`, taken in that same slot."""

    def health(self) -> dict:
        """Adapter-specific health payload (surfaced by `GET /health`)."""
        return {"source": self.name, "ok": True}


# ─── local adapter ───────────────────────────────────────────────────────────


class SyntheticCarbonIntensitySource(CarbonIntensitySource):
    """Deterministic synthetic source — the emulation/offline role.

    Deliberately mirrors the shape the Rust service already generates in
    `engine::scheduler::generate_carbon_intensity_forecast` (a daylight
    sigmoid plus autocorrelated noise). Two properties matter:

    * **Deterministic given (seed, slot)**: the generator is re-seeded per
      slot rather than advanced sequentially, so `forecast(s)` returns the
      same value no matter what order/when slots were requested. A stateful
      RNG would make the series depend on call history, and the scheduler
      would then re-plan against a *different* forecast for the same slot
      after a restart — quietly invalidating every committed assignment.
    * **No real dependency**: keeps `docker compose up` and CI working with
      no network and no API key.
    * **It does NOT fabricate a future "actual".** Even though it could, doing
      so would let emulation exercise a code path that cannot exist in
      production (see `observe`). The deviation from the forecast is applied
      only when a slot is actually entered.
    """

    name = "local"

    def __init__(
        self,
        *,
        seed: int,
        slot_minutes: float,
        epoch: datetime,
        night_max: float,
        day_min: float,
        cycle_slots: int,
        noise_std: float,
        actual_jitter_std: float,
    ) -> None:
        self._seed = seed
        self._slot_minutes = slot_minutes
        self._epoch = as_utc(epoch)
        self._night_max = night_max
        self._day_min = day_min
        self._cycle_slots = max(1, cycle_slots)
        self._noise_std = noise_std
        self._actual_jitter_std = actual_jitter_std

    # ── generation ────────────────────────────────────────────────────────

    def _forecast_value(self, slot: int) -> float:
        """Forecast for `slot` — delegates to the pure model in `forecast.py`."""
        return forecast_model.forecast_value(
            slot,
            seed=self._seed,
            cycle_slots=self._cycle_slots,
            night_max=self._night_max,
            day_min=self._day_min,
            noise_std=self._noise_std,
        )

    # ── port implementation ───────────────────────────────────────────────

    def forecast(self, from_slot: int, count: int) -> list[ForecastPoint]:
        if count <= 0:
            return []
        return [
            ForecastPoint(slot=s, forecast=self._forecast_value(s))
            for s in range(from_slot, from_slot + count)
        ]

    def _observe_current(self, slot: int) -> ObservedPoint:
        """The emulated measurement for `slot`: its forecast plus a fresh
        seeded deviation, drawn from the slot being measured.

        Note what is *not* here: any notion of the slot's "true" value. The
        same slot measured from a different measuring slot would give a
        different number (which is why `observe` refuses that call), mirroring
        the fact that a real meter has one reading, not a stored truth.
        """
        actual = forecast_model.observed_value(
            slot,
            slot,
            forecast=self._forecast_value(slot),
            seed=self._seed,
            jitter_std=self._actual_jitter_std,
        )
        return ObservedPoint(slot=slot, actual=actual, observed_at_slot=slot)

    def health(self) -> dict:
        return {
            "source": self.name,
            "ok": True,
            "seed": self._seed,
            "cycle_slots": self._cycle_slots,
            "detail": "synthetic/deterministic — emulation and offline use",
        }


# ─── remote adapter (intentionally a stub) ───────────────────────────────────


class RemoteCarbonIntensitySource(CarbonIntensitySource):
    """Adapter for a real upstream (carbonintensity.org.uk). **Not implemented.**

    This class exists now, and is wired into `build_source()`, purely to prove
    the seam is real: the interface above is sufficient, and nothing else in
    the system needs to change when the body below is filled in.

    Why it is left as a stub rather than half-built:
    * the upstream is natively **half-hourly**, so it only maps 1:1 onto
      carbonshift slots at `slot_minutes == 30` (see README §Limiti);
    * it is **GB-only** and needs an API-terms/attribution decision;
    * it changes the emulation contract (see `observe`).

    A useful property this adapter shares with the synthetic one, and which
    falls out of the corrected contract: the upstream *does* expose
    `intensity.actual`, but only for buckets it has already settled. So a
    remote source can serve `observe(measuring_slot)` for the current slot
    (and retrospectively for past ones), and `None` for anything later —
    which is exactly the same shape as the local adapter, just with real data.

    TODO(remote): perform `GET {base}/intensity/{from}/fw24h` (or fw48h),
    map each half-hour bucket onto `slot_of(...)`, take `intensity.forecast`
    for the forecast window and `intensity.actual` (present only for settled
    buckets) for `_observe_current`.
    """

    name = "remote"

    def __init__(
        self,
        *,
        base_url: str,
        slot_minutes: float,
        epoch: datetime,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._slot_minutes = slot_minutes
        self._epoch = as_utc(epoch)
        self._timeout_seconds = timeout_seconds
        # Cache is honoured so that a single upstream call can serve several
        # slot advances (and so a transient upstream outage does not stall the
        # clock). Not populated while the fetch is a stub.
        self._cache: dict[int, ForecastPoint] = {}
        self._observed: dict[int, ObservedPoint] = {}

    def forecast(self, from_slot: int, count: int) -> list[ForecastPoint]:
        # TODO(remote): replace with a real HTTP fetch (see class docstring).
        # Returning [] rather than raising keeps the clock loop alive during
        # the interim: the service degrades to "no forecast" instead of
        # crashing every slot rollover.
        return [self._cache[s] for s in range(from_slot, from_slot + count) if s in self._cache]

    def _observe_current(self, slot: int) -> Optional[ObservedPoint]:
        # TODO(remote): fetch the settled bucket for `slot` and return its
        # `intensity.actual`. None until implemented — never a fabricated
        # number, which would be the worst possible failure mode for a
        # component whose whole job is to report real grid data.
        return self._observed.get(slot)

    def health(self) -> dict:
        return {
            "source": self.name,
            "ok": False,
            "base_url": self._base_url,
            "detail": "remote adapter not implemented yet (returns no data)",
        }


# ─── factory ─────────────────────────────────────────────────────────────────


def build_source(settings) -> CarbonIntensitySource:
    """Select the adapter from configuration.

    Fails loudly on an unknown role instead of silently defaulting to
    synthetic: a typo'd `PROVIDER_ROLE=romote` must not look like a working
    emulation setup.
    """
    epoch = parse_epoch(settings.epoch_iso)
    role = settings.role.strip().lower()

    if role == "local":
        return SyntheticCarbonIntensitySource(
            seed=settings.generator_seed,
            slot_minutes=settings.slot_minutes,
            epoch=epoch,
            night_max=settings.generator_night_max,
            day_min=settings.generator_day_min,
            cycle_slots=settings.generator_cycle_slots,
            noise_std=settings.generator_noise_std,
            actual_jitter_std=settings.actual_jitter_std,
        )
    if role == "remote":
        return RemoteCarbonIntensitySource(
            base_url=os.environ.get("PROVIDER_REMOTE_BASE_URL", "https://api.carbonintensity.org.uk"),
            slot_minutes=settings.slot_minutes,
            epoch=epoch,
        )
    raise ValueError(f"unknown PROVIDER_ROLE {settings.role!r}: expected 'local' or 'remote'")
