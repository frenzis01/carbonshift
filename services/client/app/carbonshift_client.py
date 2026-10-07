"""HTTP helpers for submitting requests and managing Carbonshift profiles."""
from __future__ import annotations

from copy import deepcopy
import logging
import threading
from typing import Any

import requests

from .config import settings

logger = logging.getLogger("client.carbonshift_client")
_known_profile_lock = threading.Lock()
# Definitions are cached locally only after Carbonshift accepts them. If its
# process restarts, this catalog lets submit() restore a known profile once.
_known_profile_definitions: dict[str, dict[str, Any]] = {}


class CarbonshiftError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _post_request(
    body: dict[str, Any],
    profile_id: str | None,
) -> dict[str, Any]:
    """Submit once, restoring a known profile if Carbonshift lost its registry."""
    url = f"{settings.carbonshift_url}/v1/requests"
    try:
        response = requests.post(
            url,
            json=body,
            headers=_api_headers(),
            timeout=settings.http_timeout_seconds,
        )
    except requests.RequestException as exc:
        raise CarbonshiftError(
            f"cannot reach carbonshift at {settings.carbonshift_url}: {exc}"
        ) from exc

    if response.status_code == 404 and profile_id is not None:
        with _known_profile_lock:
            profile_definition = deepcopy(_known_profile_definitions.get(profile_id))
        if profile_definition is not None:
            # A 404 from the request endpoint means this local profile ID is no
            # longer in Carbonshift's in-memory registry. Identical registration
            # is idempotent; a conflicting definition still fails explicitly.
            logger.warning(
                "Carbonshift no longer knows profile %s; restoring it and retrying this request once",
                profile_id,
            )
            register_qos_profile(profile_definition)
            try:
                response = requests.post(
                    url,
                    json=body,
                    headers=_api_headers(),
                    timeout=settings.http_timeout_seconds,
                )
            except requests.RequestException as exc:
                raise CarbonshiftError(
                    f"cannot reach carbonshift at {settings.carbonshift_url}: {exc}"
                ) from exc

    if response.status_code not in (200, 202):
        raise CarbonshiftError(
            f"carbonshift returned {response.status_code}: {response.text}",
            status_code=response.status_code,
        )
    return response.json()


def _api_headers() -> dict[str, str]:
    """Attach the configured caller credential to protected Carbonshift APIs."""
    if settings.carbonshift_api_key:
        return {"X-API-Key": settings.carbonshift_api_key}
    return {}


# Plan submissions include an absolute arrival slot; ordinary batch submissions
# omit it and let Carbonshift derive the arrival slot from its current clock.
def submit(deadline_seconds: float, callback_url: str, payload: dict[str, Any],
           arrival_slot_global: int | None = None, qos_profile_id: str | None = None,
           task_kind: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"deadline_seconds": deadline_seconds, "callback_url": callback_url, "payload": payload}
    if qos_profile_id is not None:
        body["qos_profile_id"] = qos_profile_id
    if task_kind is not None:
        body["task_kind"] = task_kind
    if arrival_slot_global is not None:
        body["arrival_slot_global"] = arrival_slot_global

    return _post_request(body, qos_profile_id)


def register_qos_profile(profile: dict[str, Any]) -> None:
    """Register an immutable, reusable QoS policy using its caller-chosen ID.

    An identical registration is safe to repeat after a Carbonshift restart.
    Reusing an ID with different policy content is reported as an error by
    Carbonshift rather than silently changing the shared budget.
    """
    try:
        resp = requests.post(
            f"{settings.carbonshift_url}/v1/profiles",
            json=profile,
            headers=_api_headers(),
            timeout=settings.http_timeout_seconds,
        )
    except requests.RequestException as exc:
        raise CarbonshiftError(f"cannot reach carbonshift at {settings.carbonshift_url}: {exc}") from exc

    if resp.status_code != 204:
        raise CarbonshiftError(
            f"carbonshift returned {resp.status_code}: {resp.text}",
            status_code=resp.status_code,
        )
    if profile_id := profile.get("profile_id"):
        # Preserve an independent copy: callers may reuse or mutate their
        # input object after registration, but recovery must replay the accepted
        # definition exactly as Carbonshift saw it.
        with _known_profile_lock:
            _known_profile_definitions[str(profile_id)] = deepcopy(profile)


def get_qos_profile(profile_id: str) -> dict[str, Any]:
    """Fetch one active profile by its stable shared identifier."""
    try:
        resp = requests.get(
            f"{settings.carbonshift_url}/v1/profiles/{profile_id}",
            headers=_api_headers(),
            timeout=settings.http_timeout_seconds,
        )
    except requests.RequestException as exc:
        raise CarbonshiftError(f"cannot reach carbonshift at {settings.carbonshift_url}: {exc}") from exc

    if resp.status_code != 200:
        raise CarbonshiftError(f"carbonshift returned {resp.status_code}: {resp.text}")
    return resp.json()


def force_set_global_capacity_tiers(capacity_tiers: list[dict[str, Any]]) -> None:
    """Request an administrative replacement of the shared tier ladder.

    Capacity tiers price shared slot occupancy, so the replacement applies
    globally rather than changing any individual QoS profile.
    """
    try:
        resp = requests.put(
            f"{settings.carbonshift_url}/v1/admin/capacity-tiers",
            json={"capacity_tiers": capacity_tiers},
            headers=_api_headers(),
            timeout=settings.http_timeout_seconds,
        )
    except requests.RequestException as exc:
        raise CarbonshiftError(
            f"cannot reach carbonshift at {settings.carbonshift_url}: {exc}"
        ) from exc

    if resp.status_code != 204:
        raise CarbonshiftError(
            f"carbonshift returned {resp.status_code}: {resp.text}",
            status_code=resp.status_code,
        )


def get_status(request_id: str) -> dict[str, Any]:
    """`GET /v1/requests/{id}` — current status. Used to refresh a
    `TrackedRequest.ack` that was captured while still `pending` (the
    initial submit-time poll timed out before the DP solver committed an
    assignment), so fields like `flavour`/`carbon_cost` aren't stuck `null`
    forever once a result actually arrives (see `tracker.py::on_callback`)."""
    try:
        resp = requests.get(
            f"{settings.carbonshift_url}/v1/requests/{request_id}",
            headers=_api_headers(),
            timeout=settings.http_timeout_seconds,
        )
    except requests.RequestException as exc:
        raise CarbonshiftError(f"cannot reach carbonshift at {settings.carbonshift_url}: {exc}") from exc

    if resp.status_code != 200:
        raise CarbonshiftError(f"carbonshift returned {resp.status_code}: {resp.text}")
    return resp.json()


def get_stats() -> dict[str, Any]:
    """`GET /v1/stats` — request counts by status plus the scheduler's own
    (task-agnostic, by design) global error average/count."""
    try:
        resp = requests.get(
            f"{settings.carbonshift_url}/v1/stats",
            headers=_api_headers(),
            timeout=settings.http_timeout_seconds,
        )
    except requests.RequestException as exc:
        raise CarbonshiftError(f"cannot reach carbonshift at {settings.carbonshift_url}: {exc}") from exc

    if resp.status_code != 200:
        raise CarbonshiftError(f"carbonshift returned {resp.status_code}: {resp.text}")
    return resp.json()
