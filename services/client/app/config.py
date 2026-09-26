"""Runtime settings for the client, via environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class Settings:
    carbonshift_url: str = os.environ.get("CARBONSHIFT_URL", "http://localhost:8080")
    carbonshift_api_key: Optional[str] = os.environ.get("CARBONSHIFT_API_KEY") or None
    # Where carbonshift/executor callbacks reach this client — must be
    # resolvable from carbonshift's process (see README "Rete e SSRF").
    self_base_url: str = os.environ.get("CLIENT_SELF_BASE_URL", "http://localhost:8100")
    host: str = os.environ.get("CLIENT_HOST", "0.0.0.0")
    port: int = int(os.environ.get("CLIENT_PORT", "8100"))
    http_timeout_seconds: float = float(os.environ.get("CLIENT_HTTP_TIMEOUT_SECONDS", "10"))
    # Admin advance-slot calls (emulated plan mode) can legitimately take
    # longer than a normal submission: carbonshift flushes a partial batch
    # and waits for its own dispatcher to hand everything off, and the
    # executor then runs a real model inference before replying.
    admin_timeout_seconds: float = float(os.environ.get("CLIENT_ADMIN_TIMEOUT_SECONDS", "60"))
    # A request with no callback after this long is marked "timed_out".
    callback_timeout_seconds: float = float(os.environ.get("CLIENT_CALLBACK_TIMEOUT_SECONDS", "300"))
    metrics_path: str = os.environ.get("CLIENT_METRICS_PATH", "data/metrics.jsonl")
    # Default executor admin base URL for the "emulated" plan mode (only
    # used to call POST /admin/advance-slot directly — normal task traffic
    # always goes through carbonshift, never straight to the executor).
    executor_admin_url: str = os.environ.get("EXECUTOR_ADMIN_URL", "http://localhost:9000")

    # ── carbon-intensity provider ─────────────────────────────────────────
    # The provider owns the clock (see provider/ARCHITECTURE.md §2). The
    # client is a *peer* it notifies, not a driver.
    provider_url: str = os.environ.get("PROVIDER_URL", "http://localhost:9100")
    # Slot origin. MUST match the provider's PROVIDER_EPOCH exactly — the
    # default is part of the contract between the two services, so it is
    # duplicated here deliberately rather than left to chance. A mismatch
    # makes the client's slot boundaries disagree with the provider's, which
    # silently mis-assigns requests to slots.
    provider_epoch_iso: str = os.environ.get("PROVIDER_EPOCH", "2020-01-01T00:00:00+00:00")
    # Slot length. MUST match the provider's PROVIDER_SLOT_MINUTES and
    # carbonshift's SLOT_DURATION_SECONDS.
    provider_slot_minutes: float = float(os.environ.get("PROVIDER_SLOT_MINUTES", "60"))


settings = Settings()
