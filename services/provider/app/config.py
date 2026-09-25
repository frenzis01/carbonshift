"""Runtime settings for the carbon-intensity provider service.

Environment-variable driven through a single `settings` singleton, with
defaults chosen so the module is importable and unit-testable without any
external service (mirrors `client/app/config.py` and
`executor/app/config.py`).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    host: str = os.environ.get("PROVIDER_HOST", "0.0.0.0")
    port: int = int(os.environ.get("PROVIDER_PORT", "9100"))

    # ── role ──────────────────────────────────────────────────────────────
    # "local"  → generate a synthetic forecast in-process (deterministic,
    #            offline, what the scheduler already does internally today).
    # "remote" → poll a real upstream (carbonintensity.org.uk) for forecast
    #            and actual readings. ⚠️ NOT IMPLEMENTED YET — see README.
    #
    # The SAME public contract (INTERFACE.md) is served either way, so
    # carbonshift/client/executor cannot tell which role is behind it.
    role: str = os.environ.get("PROVIDER_ROLE", "local")

    # ── slot discretization ───────────────────────────────────────────────
    # Must match carbonshift's SLOT_DURATION_SECONDS. Note the upstream GB
    # carbon-intensity API is natively half-hourly, so 30 is the only value
    # that maps 1:1 onto the upstream series (see README "Limiti").
    slot_minutes: float = float(os.environ.get("PROVIDER_SLOT_MINUTES", "60"))

    # ── horizon ───────────────────────────────────────────────────────────
    # How many slots ahead of "now" a forecast covers. The user's stated
    # requirement is ~24 slots; at 30 min/slot that is 12h of lookahead.
    forecast_horizon_slots: int = int(os.environ.get("PROVIDER_FORECAST_HORIZON_SLOTS", "24"))

    # ── manual clock / emulation ──────────────────────────────────────────
    # When true, simulated time advances ONLY via POST /v1/admin/advance-slot
    # and this service becomes the single "time master" (see ARCHITECTURE.md
    # §"Chi possiede il tempo"). Never enable in production.
    manual_clock: bool = os.environ.get("PROVIDER_MANUAL_CLOCK", "0") == "1"

    # ── virtual clock origin ──────────────────────────────────────────────
    # Slot index 0 is aligned to a fixed epoch instant shared by every
    # component, so `slot = floor(unix_seconds / slot_duration)` is a pure
    # function of wall-clock time. See ARCHITECTURE.md §"Allineamento slot".
    epoch_iso: str = os.environ.get("PROVIDER_EPOCH", "2020-01-01T00:00:00+00:00")

    # ── local (synthetic) generator ───────────────────────────────────────
    # Defaults mirror the parameters the Rust service passes to
    # `generate_carbon_intensity_forecast` (see bin/service/main.rs).
    generator_seed: int = int(os.environ.get("PROVIDER_GENERATOR_SEED", "26"))
    generator_night_max: float = float(os.environ.get("PROVIDER_GENERATOR_NIGHT_MAX", "160.0"))
    generator_day_min: float = float(os.environ.get("PROVIDER_GENERATOR_DAY_MIN", "70.0"))
    generator_noise_std: float = float(os.environ.get("PROVIDER_GENERATOR_NOISE_STD", "2.0"))
    generator_cycle_slots: int = int(os.environ.get("PROVIDER_GENERATOR_CYCLE_SLOTS", "12"))

    # ── synthetic "actual" (local role only) ──────────────────────────────
    # A `local` provider has no real measurement to report, so it perturbs
    # its own forecast — exactly the stand-in the client uses today, just
    # moved behind the provider's contract so the client stops doing it.
    actual_jitter_std: float = float(os.environ.get("PROVIDER_ACTUAL_JITTER_STD", "0.05"))

    # ── notification fan-out ──────────────────────────────────────────────
    # Comma-separated base URLs of the components to notify on each slot
    # rollover (carbonshift, and optionally the executor). Emitted in a fixed
    # order with retries — see ARCHITECTURE.md §"Ordine".
    carbonshift_url: str = os.environ.get("CARBONSHIFT_URL", "http://localhost:8080")
    executor_url: str = os.environ.get("EXECUTOR_URL", "")
    client_url: str = os.environ.get("CLIENT_URL", "http://localhost:8100")
    self_base_url: str = os.environ.get("PROVIDER_SELF_BASE_URL", "http://localhost:9100")
    notify_timeout_seconds: float = float(os.environ.get("PROVIDER_NOTIFY_TIMEOUT_SECONDS", "30"))
    notify_max_attempts: int = int(os.environ.get("PROVIDER_NOTIFY_MAX_ATTEMPTS", "3"))
    notify_retry_backoff_seconds: float = float(os.environ.get("PROVIDER_NOTIFY_RETRY_BACKOFF_SECONDS", "1.0"))

    # ── realtime auto-advance ─────────────────────────────────────────────
    # When the clock is NOT manual, a background thread advances the slot on
    # its own every `slot_minutes`. With the manual clock the client drives
    # this explicitly instead.
    auto_advance_clock: bool = os.environ.get("PROVIDER_AUTO_ADVANCE_CLOCK", "1") == "1"


settings = Settings()
