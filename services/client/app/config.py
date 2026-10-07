"""Runtime settings for the client, via environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
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
    # A request with no callback after this long is marked "timed_out".
    callback_timeout_seconds: float = float(os.environ.get("CLIENT_CALLBACK_TIMEOUT_SECONDS", "300"))
    metrics_path: str = os.environ.get("CLIENT_METRICS_PATH", "data/metrics.jsonl")
    # This calibration snapshot is the local source of truth from which the
    # client reconstructs the same immutable profile definitions on startup.
    qos_profile_stats_path: str = os.environ.get(
        "CLIENT_QOS_PROFILE_STATS_PATH",
        str(Path(__file__).resolve().parent.parent / "model_stats.json"),
    )
    # Additional fully specified profiles let a client restore caller-owned
    # profiles which cannot be reconstructed from this executor's calibrations.
    qos_profile_definitions_path: Optional[str] = (
        os.environ.get("CLIENT_QOS_PROFILE_DEFINITIONS_PATH") or None
    )
    qos_profile_version: str = os.environ.get("CLIENT_QOS_PROFILE_VERSION", "v1")
    qos_profile_threshold_position: float = float(
        os.environ.get("CLIENT_QOS_PROFILE_THRESHOLD_POSITION", "0.75")
    )
    qos_profile_registration_attempts: int = int(
        os.environ.get("CLIENT_QOS_PROFILE_REGISTRATION_ATTEMPTS", "10")
    )
    qos_profile_registration_retry_seconds: float = float(
        os.environ.get("CLIENT_QOS_PROFILE_REGISTRATION_RETRY_SECONDS", "2.0")
    )
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
