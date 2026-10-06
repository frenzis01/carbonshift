"""Restore this client's calibrated QoS profiles when its process starts.

Carbonshift intentionally keeps custom profiles in memory. The client
reconstructs its known definitions from the same calibration file used by the
push script and registers them before serving traffic. A conflict is a hard
configuration error; only transient connection/server errors are retried.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from .carbonshift_client import CarbonshiftError, register_qos_profile
from .config import settings
from .qos_profiles import load_configured_profiles

logger = logging.getLogger("client.qos_profile_bootstrap")


def register_configured_qos_profiles(
    register: Callable[[dict[str, Any]], None] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> list[str]:
    """Register local calibrated profiles, retrying only startup transients.

    The retry loop handles normal Compose startup ordering, where the client
    process may start before Carbonshift is ready to receive profile requests.
    It deliberately does not hide permanent 4xx responses such as a profile
    ID conflict or invalid profile definition.
    """
    register_profile = register or register_qos_profile
    sleep_before_retry = sleep or time.sleep
    profiles = load_configured_profiles()
    if not profiles:
        logger.warning(
            "no local QoS profile definitions are configured; "
            "Carbonshift built-in task-kind defaults remain active",
        )
        logger.warning(
            "checked calibration file %s and custom profile file %s",
            settings.qos_profile_stats_path,
            settings.qos_profile_definitions_path,
        )
        return []

    attempts = settings.qos_profile_registration_attempts
    delay_seconds = settings.qos_profile_registration_retry_seconds
    if attempts < 1:
        raise ValueError("CLIENT_QOS_PROFILE_REGISTRATION_ATTEMPTS must be at least 1")
    if delay_seconds < 0:
        raise ValueError("CLIENT_QOS_PROFILE_REGISTRATION_RETRY_SECONDS cannot be negative")

    registered_ids: list[str] = []
    for profile in profiles:
        profile_id = profile["profile_id"]
        for attempt in range(1, attempts + 1):
            try:
                register_profile(profile)
                registered_ids.append(profile_id)
                logger.info("registered QoS profile %s during client startup", profile_id)
                break
            except CarbonshiftError as exc:
                is_transient = exc.status_code is None or exc.status_code in (408, 429) or exc.status_code >= 500
                if not is_transient or attempt == attempts:
                    raise CarbonshiftError(
                        f"could not restore QoS profile {profile_id!r} "
                        f"after {attempt} attempt(s): {exc}",
                        status_code=exc.status_code,
                    ) from exc

                logger.warning(
                    "Carbonshift not ready to register profile %s (attempt %d/%d): %s",
                    profile_id,
                    attempt,
                    attempts,
                    exc,
                )
                sleep_before_retry(delay_seconds)

    return registered_ids
