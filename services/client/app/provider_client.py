"""Thin HTTP wrapper around the provider's API (see provider/INTERFACE.md).

Mirrors `carbonshift_client.py`: build the URL, send the request, check the
status, raise a typed error, return JSON. Nothing else.

## Why this module exists

The client now talks to **two** services: carbonshift (submit requests) and
the provider (which owns the clock). Without this module, every call site
would carry its own `requests.post(...)` plus its own idea of the provider's
URL and error handling — the "how do I talk to the provider" knowledge
duplicated and free to drift.

This is the same ports-and-adapters idea as the provider's own
`CarbonIntensitySource`: the client's plan logic should not know *how* the
clock is driven, only that it can ask. This module is the adapter.

Note the direction: this is the client calling *out* to the provider. It is
not the provider's `notifications.py`, which is the provider calling out to
its peers.
"""
from __future__ import annotations

from typing import Any, Optional

import requests

from .config import settings


class ProviderError(RuntimeError):
    """Raised for any failure talking to the provider.

    Deliberately not swallowed anywhere: the provider is the clock, so if it
    is unreachable the run cannot proceed meaningfully. See
    `provider/ARCHITECTURE.md` §4 — a silent fallback here is exactly the
    class of bug the design exists to prevent.
    """


def _base_url() -> str:
    return settings.provider_url.rstrip("/")


def advance_slot(expect_slot: Optional[int] = None, notify_peers: bool = True) -> dict[str, Any]:
    """`POST /v1/advance-slot` — roll the clock one slot and fan out.

    `expect_slot` is the idempotency interlock: the provider returns 409 if it
    is not at that slot, so a retried call cannot double-advance the system.

    A 503 means the clock moved but at least one peer did not acknowledge —
    the run is desynced and unrecoverable without a restart. It is surfaced as
    `ProviderError` rather than retried, because retrying cannot fix it.
    """
    body: dict[str, Any] = {"notify_peers": notify_peers}
    if expect_slot is not None:
        body["expect_slot"] = expect_slot

    try:
        resp = requests.post(f"{_base_url()}/v1/advance-slot", json=body,
                             timeout=settings.admin_timeout_seconds)
    except requests.RequestException as exc:
        raise ProviderError(f"cannot reach provider at {_base_url()}: {exc}") from exc

    if resp.status_code == 409:
        raise ProviderError(f"provider refused the advance (409): {resp.text}")
    if resp.status_code == 503:
        raise ProviderError(
            f"provider is DESYNCED (503): the clock moved but a peer did not "
            f"acknowledge. Restart the stack. {resp.text}"
        )
    if resp.status_code != 200:
        raise ProviderError(f"provider returned {resp.status_code}: {resp.text}")
    return resp.json()


def get_slot() -> dict[str, Any]:
    """`GET /v1/slot` — the provider's current global slot. Cheap; safe to poll."""
    try:
        resp = requests.get(f"{_base_url()}/v1/slot", timeout=settings.http_timeout_seconds)
    except requests.RequestException as exc:
        raise ProviderError(f"cannot reach provider at {_base_url()}: {exc}") from exc
    if resp.status_code != 200:
        raise ProviderError(f"provider returned {resp.status_code}: {resp.text}")
    return resp.json()


def get_forecast(from_slot: Optional[int] = None, count: Optional[int] = None) -> dict[str, Any]:
    """`GET /v1/forecast` — the forecast window (predictions only, no actuals).

    Both arguments are optional: the provider defaults `from_slot` to its
    current slot and `count` to its configured horizon. `requests` omits
    `None` params, so passing them through unconditionally is safe.
    """
    params: dict[str, Any] = {"from_slot": from_slot, "count": count}
    try:
        resp = requests.get(f"{_base_url()}/v1/forecast", params=params,
                            timeout=settings.http_timeout_seconds)
    except requests.RequestException as exc:
        raise ProviderError(f"cannot reach provider at {_base_url()}: {exc}") from exc
    if resp.status_code != 200:
        raise ProviderError(f"provider returned {resp.status_code}: {resp.text}")
    return resp.json()


def get_observed(slot: Optional[int] = None) -> dict[str, Any]:
    """`GET /v1/observed` — the reading taken for a slot, if one was taken.

    `slot=None` yields the current slot's reading. `known: false` is the
    normal answer for a slot that has not been reached: a measurement of a
    future slot does not exist.
    """
    params: dict[str, Any] = {"slot": slot}
    try:
        resp = requests.get(f"{_base_url()}/v1/observed", params=params,
                            timeout=settings.http_timeout_seconds)
    except requests.RequestException as exc:
        raise ProviderError(f"cannot reach provider at {_base_url()}: {exc}") from exc
    if resp.status_code != 200:
        raise ProviderError(f"provider returned {resp.status_code}: {resp.text}")
    return resp.json()


def is_healthy() -> bool:
    """`GET /health` — False when the provider is degraded or desynced."""
    try:
        resp = requests.get(f"{_base_url()}/health", timeout=settings.http_timeout_seconds)
    except requests.RequestException:
        return False
    return resp.status_code == 200
